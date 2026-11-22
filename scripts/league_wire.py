"""League Wire: league moves and injury news for rostered players. No AI: Sleeper data only.

    python3 scripts/league_wire.py public/data/league-data.json

Runs in GitHub Actions every couple of hours. Writes into the data file:
  leagueWire = {
    updated: ISO time,
    moves:   the last 7 days of completed trades, waiver claims and free-agent moves
             [{ts, date, type, teams:[roster ids], adds:[[player, roster id]], drops:[[player, roster id]], faab, picks:[str]}]
    days:    injury status changes for rostered players, newest day first, 7 days kept
             [{date, items:[{pid, name, pos, nfl, roster, starter, from, to, body}]}]
    report:  every rostered player with an injury designation right now
             [{pid, name, pos, nfl, roster, starter, status, body}]
  }
  injurySnap = {player id: status} so the next run can tell what changed.
The first run only records the baseline; changes start showing from the second run.
Prints a short summary. Exits 3 if Sleeper is unreachable (the workflow then skips).
"""
import json, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import refresh  # noqa: E402

TZ = ZoneInfo("America/Chicago")
KEEP_DAYS = 7
SEVERITY = {"IR": 0, "Out": 1, "PUP": 2, "Sus": 3, "Doubtful": 4, "Questionable": 5, "COV": 6, "NA": 7, "DNR": 8}


def pname(p, pid):
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid


def main(path):
    d = json.load(open(path))
    L = f"/league/{refresh.LEAGUE}"
    try:
        state = refresh.get("/state/nfl")
        rosters = refresh.get(L + "/rosters")
        players = refresh.get("/players/nfl")
        week = int(state.get("week") or 1)
        tx = []
        for w in sorted({max(1, week - 1), week}):
            tx += refresh.get(f"{L}/transactions/{w}") or []
    except Exception as e:
        print(json.dumps({"leagueWire": f"Sleeper unreachable: {e!r}"})); sys.exit(3)

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    cutoff_ms = (now - timedelta(days=KEEP_DAYS)).timestamp() * 1000

    # ---- injuries ----
    owner, starters = {}, set()
    for r in rosters:
        for pid in (r.get("players") or []):
            owner[pid] = r["roster_id"]
        starters.update(p for p in (r.get("starters") or []) if p and p != "0")
    prev = d.get("injurySnap") or {}
    snap, report, changes = {}, [], []
    for pid, rid in owner.items():
        p = players.get(pid) or {}
        st = p.get("injury_status") or ""
        snap[pid] = st
        base = {"pid": pid, "name": pname(p, pid), "pos": p.get("position") or "", "nfl": p.get("team") or "FA",
                "roster": rid, "starter": pid in starters}
        if st:
            report.append({**base, "status": st, "body": p.get("injury_body_part") or ""})
        if prev and pid in prev and prev[pid] != st:
            changes.append({**base, "from": prev[pid] or "Healthy", "to": st or "Healthy", "body": p.get("injury_body_part") or ""})
    report.sort(key=lambda x: (SEVERITY.get(x["status"], 9), not x["starter"], x["name"]))

    lw = d.get("leagueWire") or {}
    days = [x for x in (lw.get("days") or []) if x.get("date", "") >= (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")]
    if changes:
        day = next((x for x in days if x["date"] == today), None)
        if not day:
            day = {"date": today, "items": []}
            days.insert(0, day)
        # one line per player per day: the latest move replaces an earlier one, keeping where the day started
        for c in changes:
            old = next((i for i in day["items"] if i["pid"] == c["pid"]), None)
            if old:
                c["from"] = old["from"]
                day["items"].remove(old)
            if c["from"] != c["to"]:
                day["items"].append(c)
        day["items"].sort(key=lambda x: (not x["starter"], SEVERITY.get(x["to"], 9), x["name"]))
        days = [x for x in days if x["items"]]

    # ---- moves ----
    def pl(pid):
        p = players.get(pid) or {}
        return f"{pname(p, pid)} ({p.get('position') or ''})"
    moves = []
    for x in tx:
        if x.get("status") != "complete" or (x.get("status_updated") or 0) < cutoff_ms:
            continue
        moves.append({
            "ts": x["status_updated"],
            "date": datetime.fromtimestamp(x["status_updated"] / 1000, TZ).strftime("%Y-%m-%d"),
            "type": x["type"], "teams": x.get("roster_ids") or [],
            "adds": [[pl(pid), rid] for pid, rid in (x.get("adds") or {}).items()],
            "drops": [[pl(pid), rid] for pid, rid in (x.get("drops") or {}).items()],
            "faab": (x.get("settings") or {}).get("waiver_bid"),
            "picks": [f"{p['season']} Rd {p['round']} ({d['teams'][str(p['roster_id'])]['name']}) to {d['teams'][str(p['owner_id'])]['name']}"
                      for p in (x.get("draft_picks") or []) if str(p.get("roster_id")) in d["teams"] and str(p.get("owner_id")) in d["teams"]],
        })
    moves.sort(key=lambda m: -m["ts"])

    new_lw = {"moves": moves, "days": days, "report": report}
    changed = any(new_lw[k] != lw.get(k) for k in new_lw) or snap != prev
    if changed:
        d["leagueWire"] = {"updated": now.isoformat(timespec="minutes"), **new_lw}
        d["injurySnap"] = snap
        json.dump(d, open(path, "w"), separators=(",", ":"), ensure_ascii=False)
    print(json.dumps({"leagueWire": "updated" if changed else "no change", "moves": len(moves),
                      "injuryChangesToday": sum(len(x["items"]) for x in days if x["date"] == today),
                      "injured": len(report), "baseline": not prev}))


if __name__ == "__main__":
    main(sys.argv[1])
