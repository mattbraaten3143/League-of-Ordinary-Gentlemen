"""Gather the facts for the weekly recap once a fantasy week is over. No AI.

    python3 scripts/recap_prep.py public/data/league-data.json

Runs in the daily GitHub refresh. When the week before the current NFL week has no
recap yet, it writes:
    recaps/week-N-facts.json   every number the recap needs (refresh.py recapdata)
    recaps/week-N-odds.json    playoff/bye/title odds before and after week N (refresh.py recapodds)
The cloud writer task picks these up, writes the recap, and adds it to the site.
Does nothing when the files already exist, the recap is already on the site, or the
week isn't over. Never fails the refresh: problems are printed and skipped.
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import refresh  # noqa: E402

LAST_WEEK = 17


def with_week(d, week, facts):
    """The daily refresh adds a week's scores to the data only once its recap is posted (league.week moves),
    so the week being recapped is usually missing. Add it from the facts, which have every final score."""
    if any(w["week"] == week for w in d["weeks"]):
        return d
    games = []
    for g in facts.get("games", []):
        a, b = g["teams"]
        ft = facts["teams"]
        pt = lambda i: (ft.get(i) or ft.get(str(i)))["points"]  # int keys in memory, str keys once saved as JSON
        pa, pb = pt(a), pt(b)
        games.append(sorted([{"id": a, "pts": pa}, {"id": b, "pts": pb}], key=lambda x: -x["pts"]))
    return {**d, "weeks": sorted(d["weeks"] + [{"week": week, "games": games}], key=lambda w: w["week"])}


def main(data_path):
    d = json.load(open(data_path))
    st = d.get("nflState") or {}
    if st.get("season_type") != "regular" or str(st.get("season")) != str(d["league"]["season"]):
        print(json.dumps({"recapPrep": "off-season, nothing to do"})); return
    week = int(st["week"]) - 1
    if week < 1 or week > LAST_WEEK:
        print(json.dumps({"recapPrep": f"no recap week ({week})"})); return
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if datetime.now(ZoneInfo("America/Chicago")).weekday() == 0:  # Monday: Monday night football may still be ahead
        print(json.dumps({"recapPrep": "Monday, waiting for Monday night games"})); return
    if str(week) in (d.get("recapPages") or {}):
        print(json.dumps({"recapPrep": f"week {week} recap already on the site"})); return
    os.makedirs(os.path.join(ROOT, "recaps"), exist_ok=True)
    facts_path = os.path.join(ROOT, "recaps", f"week-{week}-facts.json")
    odds_path = os.path.join(ROOT, "recaps", f"week-{week}-odds.json")
    if os.path.exists(facts_path):
        print(json.dumps({"recapPrep": f"week {week} facts already gathered"})); return
    try:
        facts = refresh.recap_facts(week)
    except SystemExit as e:  # "week N has no scores yet"
        print(json.dumps({"recapPrep": f"week {week} not ready: {e}"})); return
    except Exception as e:
        print(json.dumps({"recapPrep": f"week {week} facts failed: {e!r}"})); return
    json.dump(facts, open(facts_path, "w"), indent=1, ensure_ascii=False)
    try:
        json.dump(refresh.recap_odds(with_week(d, week, facts), week, facts), open(odds_path, "w"), indent=1, ensure_ascii=False)
    except BaseException as e:  # odds are a bonus for the writer; never let them sink the refresh
        print(json.dumps({"recapPrep": f"week {week} odds failed (facts kept): {e!r}"}))
    print(json.dumps({"recapPrep": f"week {week} facts written", "files": [os.path.relpath(p, ROOT) for p in (facts_path, odds_path) if os.path.exists(p)]}))


if __name__ == "__main__":
    main(sys.argv[1])
