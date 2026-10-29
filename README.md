# GIS News Digest

A small static website that pulls the latest GIS news from Esri, the open-source geospatial world and industry publications into one page. It rebuilds itself every morning at 6:00 am Mountain Time using GitHub Actions and is hosted for free on GitHub Pages. No server, no database.

## What's in here

| File | What it does |
|---|---|
| `feeds.yaml` | The list of news sources. Edit this to add or remove feeds. |
| `build.py` | Downloads the feeds, removes duplicates, keeps the last 14 days, writes the site into `public/`. |
| `templates/index.html.j2` | The page layout. |
| `static/style.css`, `static/app.js`, `static/favicon.svg` | Styling, filter buttons and search. |
| `.github/workflows/build.yml` | The daily schedule and the publish step. |
| `requirements.txt` | Python packages the build needs. |

## Setup (first time, about 10 minutes)

You only need a free GitHub account. Everything below happens in your web browser.

### 1. Create the repository

1. Sign in at [github.com](https://github.com).
2. Click the **+** in the top-right corner, then **New repository**.
3. Give it a name, for example `gis-news`.
4. Choose **Public**. (GitHub Pages is free for public repos. Private repos need a paid plan for Pages.)
5. Leave everything else unticked and click **Create repository**.

### 2. Upload the files

The easiest way without installing anything:

1. Unzip the project on your computer.
2. On the new repository's page, click the **uploading an existing file** link.
3. Drag **the contents** of the `gis-news` folder (not the folder itself) into the browser window. Make sure the hidden `.github` folder comes along. On a Mac press <kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>.</kbd> in Finder to show hidden files; on Windows tick **View → Hidden items** in File Explorer.
4. Click **Commit changes**.
5. Check that you can see a `.github/workflows/build.yml` file in the repo. If it's missing, click **Add file → Create new file**, type `.github/workflows/build.yml` as the name, paste the contents of that file in, and commit.

If you already use git, this works too:

```bash
cd gis-news
git init -b main
git add .
git commit -m "First version"
git remote add origin https://github.com/YOUR-USERNAME/gis-news.git
git push -u origin main
```

### 3. Turn on GitHub Pages

1. In your repository, click **Settings** (top menu).
2. In the left sidebar click **Pages**.
3. Under **Build and deployment → Source**, pick **GitHub Actions**.

That's it. There's no branch to choose because the workflow publishes the site directly.

### 4. Run it for the first time

1. Click the **Actions** tab at the top of the repository.
2. If GitHub shows a message about workflows, click **I understand my workflows, go ahead and enable them**.
3. In the left list click **Build and publish GIS news**.
4. Click the **Run workflow** button on the right, keep the branch as `main`, and click the green **Run workflow**.
5. Wait a minute or two. When the run shows a green tick, click it. The **deploy** box shows your site's link, which looks like `https://YOUR-USERNAME.github.io/gis-news/`.

From now on it runs by itself every morning at 6:00 am Mountain Time. You can press **Run workflow** whenever you want a fresh update.

## Day-to-day

**Add or remove a source.** Open `feeds.yaml` on GitHub, click the pencil icon, edit, and commit. Each feed needs a `name`, `url` and `category` (`esri`, `open-source` or `industry`). To pause a feed without deleting it, add `enabled: false`. The change shows up on the next run.

**See which feeds failed.** Scroll to the bottom of the site and open **Sources**: working feeds have a green dot, skipped ones a red dot with the reason. The Actions run log also shows a yellow warning listing skipped feeds. A broken feed never stops the build.

**Run it on your own computer** (optional):

```bash
python -m pip install -r requirements.txt
python build.py
# then open public/index.html in a browser
```

## GitHub Repo of the Day

The **GitHub Repo** tab shows one open-source GIS repository picked each morning, plus the picks from the last 14 days. Today's pick has an orange border and a "Repo of the day" label, and it also shows up at the top of **All**.

How it's picked: each day uses one topic from a rotating list (`gis`, `geospatial`, `qgis`, `arcgis`, `remote-sensing`, `gdal`, `mapping`, `geojson`, `spatial-analysis`, `openstreetmap`). The build searches GitHub for active repos with that topic (at least 50 stars, a push in the last 90 days, not archived) and takes the most-starred one that hasn't been featured in the past year. Past picks are saved in `repos.json` on the site, the same way `items.json` works for news.

To change the topics or thresholds, edit `REPO_TOPICS`, `REPO_MIN_STARS` and `REPO_ACTIVE_DAYS` near the top of `build.py`. If the GitHub search fails one morning, the news still builds and the Sources list shows a red dot for "GitHub repo search". Running the workflow again the same day keeps that day's pick instead of choosing a new one.

## Good to know

- **The site keeps 14 days of news even though most feeds only list their last ~10 posts.** Each build saves an `items.json` file on the site, and the next build downloads it and adds the new items on top. If you ever want to start fresh, delete and re-create the Pages site, or just let old items age out.
- **GitHub pauses scheduled workflows after 60 days with no commits** to the repository. You'll get an email first. Pressing **Run workflow** or making any small commit (like editing this README) turns the schedule back on.
- **Scheduled runs can start 5 to 30 minutes late** when GitHub is busy. That's normal.
- **Daylight saving time.** GitHub's scheduler only knows UTC, so the workflow has two triggers (12:00 and 13:00 UTC) and a small check that lets through only the one that equals 6:00 am Mountain on that day. The other one finishes in a few seconds without doing anything; that's expected.
- **Esri's main feeds need a parameter.** `esri.com/arcgis-blog/feed/` and `esri.com/about/newsroom/feed/` currently return empty feeds; adding `?post_type=blog` or `?post_type=announcements` is what returns the actual posts. If those ever go empty, that's the first thing to check.
