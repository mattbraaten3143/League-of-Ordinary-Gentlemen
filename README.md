# Ordinary Gentleman Sports Network

The league site for **The League of Ordinary Gentleman** (Sleeper dynasty, 12 teams), as a real website.
It started as a copy of the Claude artifact, which stays live for the league until this site takes over.

## What's here

```
public/                  everything the website serves
  index.html             the site (built; don't edit by hand)
  live.js                live layer: fresh standings + live scores from Sleeper
  data/league-data.json  the league data the page is built from
  images/                player photos (Wikimedia Commons, credited on the page)
  favicon.svg
scripts/
  hub_template.txt       the page template, same file as the artifact's src/hub_template.txt
  refresh.py             pulls Sleeper, FantasyCalc and Dynasty Daddy; builds league-data.json
  build_site.py          template + data -> public/index.html (adds the live layer)
  builder.js             the artifact's browser-console builder (kept for reference)
.github/workflows/refresh.yml   runs refresh.py + build_site.py every morning and commits
vercel.json              hosting settings, including the /sleeper pass-through to Sleeper's API
```

## How the data stays fresh

- **When someone opens the site:** `live.js` asks Sleeper for the current records, points,
  standings and any finished weeks the built-in data doesn't have yet, then draws the page.
  If Sleeper doesn't answer within 4 seconds, the page draws from the built-in data instead.
- **During games:** the score ticker shows this week's matchups and refreshes every minute.
- **Every morning (about 5 AM Central):** GitHub runs `refresh.py` for new values, trades,
  power rankings and playoff odds, rebuilds the page and commits. Vercel redeploys automatically.
  It can also be run on demand from the repo's **Actions** tab → *Daily data refresh* → *Run workflow*.

## Going live (one-time setup)

1. Create a GitHub account and an empty repository (e.g. `ogsn`). Push this folder to it.
2. Sign in to [vercel.com](https://vercel.com) with GitHub, choose **Add New → Project**, pick the repo,
   and deploy with the default settings (`vercel.json` already tells it to serve `public/`).
3. In the GitHub repo: **Settings → Actions → General → Workflow permissions →
   Read and write permissions** (lets the morning refresh commit).
4. Optional: buy a domain and add it under the Vercel project's **Settings → Domains**.

## Keeping it in step with the artifact (for now)

The weekly recaps and editorial are still written into the artifact by the "OGSN Daily Refresh"
scheduled task (10:00 AM Central). A second scheduled task, "OGSN Website Sync" (11:23 AM Central),
copies the artifact's `data/league-data.json`, `src/hub_template.txt` and any new photos into this
repo, rebuilds, and pushes. `scripts/artifact_sync.json` records the last artifact version synced,
so a day with no artifact change does nothing.

## Local preview

```
python3 scripts/build_site.py
python3 -m http.server 8000 -d public
```
Then open http://localhost:8000. Locally there is no `/sleeper` pass-through, so `live.js`
calls Sleeper's API directly.

## Known differences from the artifact

- The trade calculator's "Claude's take" button only works inside the artifact (it uses the
  viewer's Claude sign-in). On this site it stays hidden until an AI backend with an API key is added.

## Roadmap

1. ✅ League hub with live scores and standings
2. Recaps and editorial published straight to the site; AI trade grades on every new trade
3. Daily League Wire (league moves + injuries for rostered players)
4. NFL headlines filtered to rostered players; creator video feeds
5. Installable app (home-screen icon, push notifications for recaps and trades)
