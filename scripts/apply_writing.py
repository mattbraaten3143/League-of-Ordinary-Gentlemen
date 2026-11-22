"""Put the writer's work into the site data. The writer supplies words; every number comes from the data.

    python3 scripts/apply_writing.py recap  --week N --draft recaps/week-N-draft.json
    python3 scripts/apply_writing.py trades --draft recaps/trades-draft.json

recap: the draft is
    {"dek": str,
     "stories": [{"tag": str, "teams": [roster ids], "h": str, "body": str}, ...],   # first = game of the week
     "games":   [{"winner": roster id, "loser": roster id, "h": str, "tag": str, "hot": bool, "t": [paragraphs], "v": one-line verdict}, ...],
     "awards":  [{"a": award name, "who": str, "p": str}, ...],
     "note":    str}
  Team names, records, final scores and game-flow numbers are filled in from
  recaps/week-N-facts.json and the site data, not from the draft. Writes recapPages[N],
  updates the home page editorial (week, dek, stories, awards, recap list) and sets
  league.week = N.

trades: the draft is {trade key: {"headline": str, "takes": {roster id: str}}} for
  tradeAnalysis entries that have no headline yet.

Checks the draft and exits with an error listing every problem instead of writing
anything half-right.
"""
import argparse, json, os, re, sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "public", "data", "league-data.json")
SITE = "https://leagueofordinarygentlemen.vercel.app"


def fail(problems):
    sys.exit("apply_writing: draft not applied:\n- " + "\n- ".join(problems))


def first_sentences(text, n=2):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(parts[:n])


def recap(week, draft_path):
    d = json.load(open(DATA))
    facts = json.load(open(os.path.join(ROOT, "recaps", f"week-{week}-facts.json")))
    dr = json.load(open(draft_path))
    ft = {int(k): v for k, v in facts["teams"].items()}
    pairs = {frozenset(g["teams"]) for g in facts["games"]}
    ids = set(ft)
    probs = []
    for k in ("dek", "stories", "games", "awards", "note"):
        if not dr.get(k):
            probs.append(f"missing {k}")
    if probs:
        fail(probs)
    seen = set()
    for i, g in enumerate(dr["games"]):
        w, l = g.get("winner"), g.get("loser")
        if frozenset((w, l)) not in pairs:
            probs.append(f"game {i + 1}: {w} vs {l} is not a week {week} matchup")
            continue
        if ft[w]["points"] < ft[l]["points"]:
            probs.append(f"game {i + 1}: {ft[w]['name']} did not beat {ft[l]['name']}")
        seen.add(frozenset((w, l)))
        for k in ("h", "tag", "t", "v"):
            if not g.get(k):
                probs.append(f"game {i + 1}: missing {k}")
    if seen != pairs:
        probs.append(f"games cover {len(seen)} of {len(pairs)} matchups")
    for i, s in enumerate(dr["stories"]):
        if not s.get("h") or not s.get("body") or not s.get("tag"):
            probs.append(f"story {i + 1}: needs tag, h and body")
        if not set(s.get("teams") or []) <= ids:
            probs.append(f"story {i + 1}: unknown roster ids {s.get('teams')}")
    if frozenset(dr["stories"][0].get("teams") or []) not in pairs:
        probs.append("story 1 must be the game of the week, with teams = exactly that game's two roster ids")
    if len(dr["awards"]) < 6:
        probs.append(f"only {len(dr['awards'])} awards (need at least 6)")
    for i, a in enumerate(dr["awards"]):
        if not (a.get("a") and a.get("who") and a.get("p")):
            probs.append(f"award {i + 1}: needs a, who and p")
    if any("<" in json.dumps(x) for x in (dr["dek"], dr["stories"], dr["games"], dr["awards"], dr["note"])):
        probs.append("text must be plain, no HTML tags")
    if probs:
        fail(probs)

    T = d["teams"]
    rec = lambda i: f"{T[str(i)]['w']}-{T[str(i)]['l']}" + (f"-{T[str(i)]['t']}" if T[str(i)].get("t") else "")
    side = lambda i: {"n": ft[i]["name"], "r": rec(i), "p": ft[i]["points"], "f": ft[i]["flow"]}
    url = f"{SITE}/#recap-{week}"
    games = [{"a": side(g["winner"]), "b": side(g["loser"]), "h": g["h"], "tag": g["tag"], "hot": bool(g.get("hot")), "t": g["t"], "v": g["v"]}
             for g in dr["games"]]
    d.setdefault("recapPages", {})[str(week)] = {
        "week": week, "url": url, "dek": dr["dek"],
        "stories": [{"h": s["h"], "body": s["body"]} for s in dr["stories"]],
        "games": games, "awards": dr["awards"], "note": dr["note"]}

    ed = d.setdefault("editorial", {})
    prev = [s for s in ed.get("stories", []) if s.get("week") == week - 1][:3]
    ed["week"] = week
    ed["updated"] = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    ed["dek"] = dr["dek"]
    ed["stories"] = [{"week": week, "tag": s["tag"], "teams": s["teams"], "h": s["h"], "body": s["body"]} for s in dr["stories"]] + \
                    [{**s, "tag": f"Week {week - 1}", "body": first_sentences(s["body"])} for s in prev]
    ed["awards"] = dr["awards"][:6]
    ed["recaps"] = [{"week": week, "url": url, "h": dr["stories"][0]["h"]}] + [r for r in ed.get("recaps", []) if r.get("week") != week]
    d["league"]["week"] = week
    json.dump(d, open(DATA, "w"), separators=(",", ":"), ensure_ascii=False)
    print(json.dumps({"recapApplied": week, "games": len(games), "stories": len(dr["stories"]), "awards": len(dr["awards"])}))


def trades(draft_path):
    d = json.load(open(DATA))
    dr = json.load(open(draft_path))
    by = {t["key"]: t for t in d.get("tradeAnalysis", [])}
    probs, done = [], []
    for k, w in dr.items():
        t = by.get(k)
        if not t:
            probs.append(f"{k}: no such trade")
            continue
        sides = {str(s["id"]) for s in t["sides"]}
        if not w.get("headline") or "<" in w["headline"]:
            probs.append(f"{k}: needs a plain-text headline")
        if set(map(str, (w.get("takes") or {}))) != sides:
            probs.append(f"{k}: takes must cover exactly roster ids {sorted(sides)}")
    if probs:
        fail(probs)
    for k, w in dr.items():
        by[k]["headline"] = w["headline"]
        by[k]["takes"] = {str(r): v for r, v in w["takes"].items()}
        done.append(k)
    json.dump(d, open(DATA, "w"), separators=(",", ":"), ensure_ascii=False)
    print(json.dumps({"tradesWritten": done}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["recap", "trades"])
    ap.add_argument("--week", type=int)
    ap.add_argument("--draft", required=True)
    a = ap.parse_args()
    recap(a.week, a.draft) if a.cmd == "recap" else trades(a.draft)
