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

## How it runs (no one has to do anything)

| When | What | Where |
|---|---|---|
| Someone opens the site | `live.js` pulls current records, standings and scores from Sleeper; ticker refreshes every minute during games | the visitor's browser |
| Every morning, about 5 AM Central | `refresh.py` pulls Sleeper, FantasyCalc and Dynasty Daddy; `news.py` writes news items for trades, $10+ waiver adds and renames; `recap_prep.py` gathers the facts for last week's recap once the week is over (Tuesday); page rebuilt and committed | GitHub Actions (`.github/workflows/refresh.yml`) |
| Every morning, about 7:43 AM Central | **OGSN Writer** (Claude scheduled task, runs in the cloud, approves itself): writes the weekly recap when facts are waiting, writes headlines and takes for new trades, then mirrors the site to the Claude artifact | Claude cloud |
| Every commit | Vercel redeploys the site | Vercel |

Nothing runs on Matt's computer. If Sleeper is down in the morning, the refresh keeps yesterday's data and tries again the next day.

### The recap pipeline
1. `recap_prep.py` writes `recaps/week-N-facts.json` (every starter, bench, best lineup, game-flow by window, top performances, transactions) and `recaps/week-N-odds.json` (playoff odds before and after).
2. The writer reads those plus the site data and writes `recaps/week-N-draft.json`: the words only.
3. `apply_writing.py recap` checks the draft (every matchup covered, winners right, at least 6 awards, plain text) and fills in names, records, scores and game-flow numbers from the facts, so the numbers on the page never come from the writer. Then `build_site.py` and push.

## Going live (one-time setup)

1. Create a GitHub account and an empty repository (e.g. `ogsn`). Push this folder to it.
2. Sign in to [vercel.com](https://vercel.com) with GitHub, choose **Add New → Project**, pick the repo,
   and deploy with the default settings (`vercel.json` already tells it to serve `public/`).
3. In the GitHub repo: **Settings → Actions → General → Workflow permissions →
   Read and write permissions** (lets the morning refresh commit).
4. Optional: buy a domain and add it under the Vercel project's **Settings → Domains**.

## The Claude artifact

The artifact at claude.ai is now a mirror: the writer republishes it from this repo's data whenever
the data changes. Edit the site here, not there. `scripts/artifact_mirror.json` records the last version mirrored.

## Local preview

```
python3 scripts/build_site.py
python3 -m http.server 8000 -d public
```
Then open http://localhost:8000. Locally there is no `/sleeper` pass-through, so `live.js`
calls Sleeper's API directly.

## Claude's take (trade calculator)

The calculator's "Claude's take" button calls `api/take.js`, a Vercel function that holds the prompt
and uses the `ANTHROPIC_API_KEY` environment variable (set in Vercel, never in this repo). The browser
sends only the calculator's facts. Limits: 8 takes per visitor per 10 minutes and 150 per day per
server instance; the real ceiling is the monthly spend limit in the Anthropic Console. Model:
`claude-haiku-5-5` (override with a `TAKE_MODEL` environment variable). Inside the Claude artifact the
same button uses the viewer's own Claude sign-in instead.

## Roadmap

1. ✅ League hub with live scores and standings
2. Recaps and editorial published straight to the site; AI trade grades on every new trade
3. Daily League Wire (league moves + injuries for rostered players)
4. NFL headlines filtered to rostered players; creator video feeds
5. Installable app (home-screen icon, push notifications for recaps and trades)
