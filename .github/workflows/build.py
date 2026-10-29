#!/usr/bin/env python3
"""Fetch GIS news feeds and build a static site into ./public.

Usage:
    python build.py                       # normal build
    python build.py --feeds other.yaml    # use a different feed list
    PREVIOUS_ITEMS_URL=https://you.github.io/repo/items.json python build.py

How items survive between runs (no database):
    Each build publishes public/items.json. On the next run the script downloads
    that file from the live site (PREVIOUS_ITEMS_URL), merges it with whatever
    the feeds return today, and drops anything older than MAX_AGE_DAYS. Most
    feeds only list their last 10 or so posts, so this is what lets the site
    hold the full MAX_AGE_DAYS window (14 days by default).

GitHub Repo of the Day:
    Each build also picks one GIS repository from the GitHub search API and
    adds it to public/repos.json. That file is carried over between runs the
    same way, so a repo is never featured twice within REPO_HISTORY_DAYS.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ---------------------------------------------------------------- settings
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "public"
TEMPLATE_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))  # keep this many days of items
SUMMARY_CHARS = 200        # summary length before "…"
TIMEOUT_SECONDS = 20       # per feed
MAX_WORKERS = 8            # feeds fetched in parallel
LOCAL_TZ = ZoneInfo(os.environ.get("SITE_TIMEZONE", "America/Edmonton"))
SITE_TITLE = os.environ.get("SITE_TITLE", "GIS News Digest")

CATEGORIES = {
    "esri": "Esri",
    "open-source": "Open Source",
    "industry": "Industry News",
    "github": "GitHub Repo",
}
# Categories a feed in feeds.yaml can use. "github" is filled by the repo picker.
FEED_CATEGORIES = ("esri", "open-source", "industry")

# ---- GitHub Repo of the Day
GITHUB_API = "https://api.github.com/search/repositories"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional; raises the search rate limit
REPO_HISTORY_DAYS = int(os.environ.get("REPO_HISTORY_DAYS", "365"))  # no repeats within this window
REPO_MIN_STARS = int(os.environ.get("REPO_MIN_STARS", "50"))
REPO_ACTIVE_DAYS = 90  # only repos with a push in this many days
# One topic per day, in rotation, so the picks don't all come from one corner of GIS.
REPO_TOPICS = [
    "gis", "geospatial", "qgis", "arcgis", "remote-sensing",
    "gdal", "mapping", "geojson", "spatial-analysis", "openstreetmap",
]

# Some sites reject requests that don't look like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36 GISNewsDigest/1.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$)")

log = logging.getLogger("gis-news")


# ---------------------------------------------------------------- helpers
class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML fragment, skipping script/style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw or "")
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to a crude strip
        text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def make_summary(raw: str, limit: int = SUMMARY_CHARS) -> str:
    text = strip_html(raw)
    # WordPress feeds often end with "The post X appeared first on Y."
    text = re.sub(r"\s*The post .{0,300}? appeared first on .{0,120}?\.?$", "", text)
    text = re.sub(r"\s*\[(…|\.\.\.|&hellip;)\]\s*$", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[int(limit * 0.6):]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def normalize_url(url: str) -> str:
    """Lowercase host, drop tracking params, fragment and trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower().removeprefix("www."), path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                # feedparser always normalizes these tuples to UTC.
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


# ---------------------------------------------------------------- fetching
def load_feeds(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    feeds = []
    for i, feed in enumerate(data.get("feeds", []), start=1):
        if not isinstance(feed, dict) or not feed.get("url") or not feed.get("name"):
            log.warning("feeds.yaml entry #%d is missing a name or url, skipping", i)
            continue
        if feed.get("enabled", True) is False:
            continue
        category = str(feed.get("category", "industry")).strip().lower()
        if category not in FEED_CATEGORIES:
            log.warning("%s: unknown category %r, using 'industry'", feed["name"], category)
            category = "industry"
        feeds.append({"name": str(feed["name"]), "url": str(feed["url"]), "category": category})
    return feeds


def fetch_feed(feed: dict) -> tuple[dict, list[dict], str | None]:
    """Return (feed, items, error). Never raises."""
    try:
        resp = requests.get(feed["url"], headers=HEADERS, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.Timeout:
        return feed, [], f"timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # network, HTTP status, anything else
        return feed, [], f"{type(exc).__name__}: {exc}"[:300]

    if not parsed.entries:
        reason = parsed.get("bozo_exception") or "no items in feed"
        return feed, [], f"{reason}"[:300]

    now = datetime.now(timezone.utc)
    items = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title") or "")
        if not link or not title:
            continue
        published = entry_datetime(entry)
        raw_summary = entry.get("summary") or ""
        if not raw_summary and entry.get("content"):
            raw_summary = entry["content"][0].get("value", "")
        items.append(
            {
                "title": title,
                "link": link,
                "source": feed["name"],
                "category": feed["category"],
                "summary": make_summary(raw_summary),
                # Undated items get "now"; the merge step keeps their first-seen date.
                "published": min(published or now, now).isoformat(),
                "dated": published is not None,
            }
        )
    return feed, items, None


def fetch_all(feeds: list[dict]) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    status: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for feed, feed_items, error in pool.map(fetch_feed, feeds):
            if error:
                log.warning("SKIPPED %s (%s): %s", feed["name"], feed["url"], error)
            else:
                log.info("ok      %-32s %3d items", feed["name"], len(feed_items))
            items.extend(feed_items)
            status.append({**feed, "ok": error is None, "error": error, "count": len(feed_items)})
    return items, status


def load_previous(url: str | None) -> list[dict]:
    """Download items.json from the live site so older items carry over."""
    if not url:
        return []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 404:
            log.info("No previous items.json yet (first run)")
            return []
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        log.info("Loaded %d items from previous build", len(items))
        return items
    except Exception as exc:
        log.warning("Couldn't load previous items from %s: %s", url, exc)
        return []


# ---------------------------------------------------------------- merging
def merge(fresh: list[dict], previous: list[dict], feed_order: list[str]) -> list[dict]:
    """Combine, de-duplicate and age out items.

    Duplicates are matched on normalized URL, and also on title within the
    same category (catches the same post syndicated by Planet QGIS, etc.).
    When two copies collide, the one from the feed listed earlier in
    feeds.yaml wins, and the earliest known date is kept.
    """
    rank = {name: i for i, name in enumerate(feed_order)}
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    # Fresh items first so their summaries/titles are the current ones.
    candidates = sorted(fresh, key=lambda it: rank.get(it["source"], 999)) + previous

    by_url: dict[str, dict] = {}
    by_title: dict[tuple[str, str], dict] = {}
    kept: list[dict] = []

    for item in candidates:
        try:
            when = datetime.fromisoformat(item["published"])
        except (KeyError, ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue

        url_key = normalize_url(item["link"])
        title_key = (item.get("category", ""), normalize_title(item["title"]))
        existing = by_url.get(url_key) or (by_title.get(title_key) if title_key[1] else None)

        if existing:
            # Keep the earliest date (so undated items don't keep jumping to the top).
            if item["published"] < existing["published"]:
                existing["published"] = item["published"]
            if rank.get(item["source"], 999) < rank.get(existing["source"], 999):
                for key in ("title", "link", "source", "category", "summary"):
                    existing[key] = item[key]
            continue

        record = {k: item.get(k) for k in ("title", "link", "source", "category", "summary", "published")}
        record["id"] = hashlib.sha1(url_key.encode()).hexdigest()[:12]
        kept.append(record)
        by_url[url_key] = record
        if title_key[1]:
            by_title[title_key] = record

    # Drop items whose source was removed from feeds.yaml.
    known = set(feed_order)
    kept = [it for it in kept if it["source"] in known]
    kept.sort(key=lambda it: it["published"], reverse=True)
    return kept


# ---------------------------------------------------------------- repo of the day
def load_previous_repos(url: str | None) -> list[dict]:
    """Download repos.json (past picks) from the live site."""
    if not url:
        return []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 404:
            log.info("No previous repos.json yet (first run)")
            return []
        resp.raise_for_status()
        data = resp.json()
        picks = data.get("picks", []) if isinstance(data, dict) else []
        log.info("Loaded %d past repo picks", len(picks))
        return [p for p in picks if isinstance(p, dict) and p.get("full_name") and p.get("date")]
    except Exception as exc:
        log.warning("Couldn't load previous repo picks from %s: %s", url, exc)
        return []


def search_repos(topic: str) -> list[dict]:
    """Return active, reasonably popular repos for one topic, most stars first."""
    since = (datetime.now(timezone.utc) - timedelta(days=REPO_ACTIVE_DAYS)).date().isoformat()
    query = f"topic:{topic} stars:>={REPO_MIN_STARS} pushed:>={since} archived:false fork:false"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": HEADERS["User-Agent"]}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    resp = requests.get(
        GITHUB_API,
        params={"q": query, "sort": "stars", "order": "desc", "per_page": 100},
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def pick_repo_of_day(previous: list[dict]) -> tuple[list[dict], dict]:
    """Add today's pick (if there isn't one yet) and return (picks, status)."""
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date()
    cutoff = (today - timedelta(days=REPO_HISTORY_DAYS)).isoformat()
    picks = sorted((p for p in previous if p["date"] >= cutoff), key=lambda p: p["date"], reverse=True)
    status = {"name": "GitHub repo search", "url": "https://github.com/topics/gis",
              "category": "github", "ok": True, "error": None, "count": 0}

    if picks and picks[0]["date"] == today.isoformat():
        log.info("ok      Repo of the day already picked: %s", picks[0]["full_name"])
        status["count"] = 1
        return picks, status

    seen = {p["full_name"].lower() for p in picks}
    start = today.toordinal() % len(REPO_TOPICS)
    order = REPO_TOPICS[start:] + REPO_TOPICS[:start]
    errors = []
    for topic in order[:4]:  # today's topic, then a few fallbacks; keeps API calls low
        try:
            candidates = search_repos(topic)
        except Exception as exc:
            errors.append(f"{topic}: {type(exc).__name__}: {exc}"[:200])
            continue
        fresh = [r for r in candidates if r.get("full_name", "").lower() not in seen]
        if not fresh:
            continue
        repo = fresh[0]
        pick = {
            "date": today.isoformat(),
            "picked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "topic": topic,
            "full_name": repo["full_name"],
            "url": repo["html_url"],
            "description": make_summary(repo.get("description") or ""),
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "language": repo.get("language") or "",
            "license": (repo.get("license") or {}).get("spdx_id") or "",
            "pushed": repo.get("pushed_at") or "",
        }
        log.info("ok      Repo of the day: %s (topic %s, %d stars)", pick["full_name"], topic, pick["stars"])
        status["count"] = 1
        return [pick] + picks, status

    status["ok"] = False
    status["error"] = "; ".join(errors) or "no new repos found"
    log.warning("SKIPPED repo of the day: %s", status["error"])
    return picks, status


def repo_items(picks: list[dict]) -> list[dict]:
    """Turn recent picks into cards for the page (last MAX_AGE_DAYS only)."""
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date()
    cutoff = (today - timedelta(days=MAX_AGE_DAYS)).isoformat()
    items = []
    for p in picks:
        if p["date"] < cutoff:
            continue
        items.append({
            "id": "gh-" + hashlib.sha1(p["full_name"].lower().encode()).hexdigest()[:10],
            "title": p["full_name"],
            "link": p["url"],
            "source": "GitHub",
            "category": "github",
            "summary": p.get("description", ""),
            "published": p.get("picked_at") or f"{p['date']}T12:00:00+00:00",
            "repo": p,
            "is_today": p["date"] == today.isoformat(),
        })
    return items


# ---------------------------------------------------------------- rendering
def day_label(local_date, today) -> str:
    if local_date == today:
        return "Today"
    if local_date == today - timedelta(days=1):
        return "Yesterday"
    return local_date.strftime("%A, %B %-d")


def render(items: list[dict], status: list[dict], out_dir: Path, repo_picks: list[dict] | None = None) -> None:
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(LOCAL_TZ)
    today = now_local.date()
    news_items = items
    repo_picks = repo_picks or []
    items = sorted(news_items + repo_items(repo_picks), key=lambda it: it["published"], reverse=True)

    groups: list[dict] = []
    for item in items:
        local = datetime.fromisoformat(item["published"]).astimezone(LOCAL_TZ)
        item["date_display"] = local.strftime("%b %-d, %Y")
        item["date_iso"] = local.date().isoformat()
        label = day_label(local.date(), today)
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append(item)

    counts = {key: sum(1 for it in items if it["category"] == key) for key in CATEGORIES}

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "j2"]))
    html_out = env.get_template("index.html.j2").render(
        site_title=SITE_TITLE,
        groups=groups,
        total=len(items),
        news_total=len(news_items),
        counts=counts,
        categories=CATEGORIES,
        status=sorted(status, key=lambda s: (list(CATEGORIES).index(s["category"]), s["name"].lower())),
        updated_iso=now.isoformat(timespec="seconds"),
        updated_display=now_local.strftime("%b %-d, %Y, %-I:%M %p %Z"),
        max_age_days=MAX_AGE_DAYS,
    )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / "index.html").write_text(html_out, encoding="utf-8")
    for asset in STATIC_DIR.iterdir():
        if asset.is_file():
            shutil.copy2(asset, out_dir / asset.name)

    public_items = [
        {k: it[k] for k in ("id", "title", "link", "source", "category", "summary", "published")}
        for it in news_items
    ]
    (out_dir / "items.json").write_text(
        json.dumps({"updated": now.isoformat(timespec="seconds"), "items": public_items}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (out_dir / "repos.json").write_text(
        json.dumps({"updated": now.isoformat(timespec="seconds"), "picks": repo_picks}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (out_dir / "status.json").write_text(
        json.dumps({"updated": now.isoformat(timespec="seconds"), "feeds": status}, indent=1), encoding="utf-8"
    )
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--feeds", default=str(ROOT / "feeds.yaml"), help="path to feeds.yaml")
    parser.add_argument("--out", default=str(OUT_DIR), help="output folder (default: public)")
    parser.add_argument("--previous", default=os.environ.get("PREVIOUS_ITEMS_URL"),
                        help="URL of the last published items.json")
    parser.add_argument("--previous-repos", default=os.environ.get("PREVIOUS_REPOS_URL"),
                        help="URL of the last published repos.json (default: next to items.json)")
    parser.add_argument("--no-repo", action="store_true", help="skip the GitHub Repo of the Day")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    feeds = load_feeds(Path(args.feeds))
    if not feeds:
        log.error("No feeds found in %s", args.feeds)
        return 1

    fresh, status = fetch_all(feeds)
    previous = load_previous(args.previous)
    items = merge(fresh, previous, [f["name"] for f in feeds])

    repo_picks: list[dict] = []
    if not args.no_repo:
        repos_url = args.previous_repos
        if not repos_url and args.previous:
            repos_url = args.previous.rsplit("/", 1)[0] + "/repos.json"
        repo_picks, repo_status = pick_repo_of_day(load_previous_repos(repos_url))
        status.append(repo_status)

    render(items, status, Path(args.out), repo_picks)

    failed = [s["name"] for s in status if not s["ok"]]
    feeds_ok = sum(1 for s in status if s["ok"] and s["category"] != "github")
    log.info("Built %d items from %d/%d feeds into %s", len(items), feeds_ok, len(feeds), args.out)
    if failed:
        log.warning("Feeds skipped this run: %s", ", ".join(failed))
        # Surface a visible warning in the GitHub Actions log without failing the job.
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=Feeds skipped::{', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
