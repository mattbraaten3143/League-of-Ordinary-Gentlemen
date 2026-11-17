"""OGSN hub refresh tool. Standard library only.

  python3 refresh.py fetch --old league-data.json --out fresh.json [--week N]
      Pulls the league from the Sleeper API, rebuilds every data field, keeps
      `editorial` from the old file, writes fresh.json and prints a JSON diff
      (new trades, adds, renames, recap week change). Exit code 3 = Sleeper
      unreachable. --week defaults to the old file's league.week.

  python3 refresh.py assemble --data fresh.json --template hub_template.txt --out ogsn.html
      Injects the data into the page template.

  python3 refresh.py proj --data fresh.json
      Refreshes only `proj` (Sleeper's projections for the current NFL week, scored with the
      league's settings, plus each player's opponent) in --data. Sleeper calls only. `fetch`
      already does this.

  python3 refresh.py recapdata --week 6 --out facts.json
      Computes every number for week N's recap: each team's starters with points and game
      window, bench points, best possible lineup and points left on the bench, zero-point
      starters, whether the best lineup would have flipped the result, cumulative score by
      game window (kickoff times from nflverse), top performances and the week's transactions.

  python3 refresh.py recap --data fresh.json --html week6.html --week 6 --url <recap artifact url>
      Parses a weekly recap page (storylines, matchups with game-flow data, awards) into
      recapPages so the site shows it as its own page.

  python3 refresh.py image --title "Josh Allen (quarterback)" --slug josh-allen --dir images
      Finds the lead photo of that English Wikipedia article, checks it is on Wikimedia
      Commons under a free license (CC0, public domain, CC BY, CC BY-SA), downloads a
      960px copy to images/<slug>.jpg and prints credit metadata. Use --query "<name> NFL"
      to search instead of giving an exact title. Exits with an error rather than
      returning a non-free or missing image.
"""
import argparse, json, os, sys, urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

LEAGUE = "1308882514567270400"
REVALUE = False  # set by `fetch --revalue`: recompute existing trade analyses with the current value model
API = "https://api.sleeper.app/v1"
# FantasyCalc: documented endpoint only, called once per run (their terms ask for daily caching and attribution).
FC_URL = "https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=1&numTeams=12&ppr=1&tep=none"
# Dynasty Daddy: documented public value endpoints (players only, no picks). Called once per run.
DD_DYNASTY = "https://api.dynasty-daddy.com/api/v1/values/dynasty"
DD_REDRAFT = "https://api.dynasty-daddy.com/api/v1/values/redraft"


# ---- OGSN value model: FantasyCalc as the base, with the league's adjustments on top ----
VALUE_DEFAULTS = {
    "curve": 1.4,          # >1 makes value less linear: stars are worth far more than the sum of mid pieces
    "scale": 10000,        # value at which the curve is neutral (about FantasyCalc's top player)
    "pos": {"QB": 0.75, "RB": 1.08, "WR": 1.05, "TE": 1.0},
    "age": {               # [max age, multiplier] steps, on top of the age effect already in FantasyCalc
        "RB": [[23, 1.08], [25, 1.0], [26, 0.93], [27, 0.85], [99, 0.75]],
        "WR": [[24, 1.06], [27, 1.0], [29, 0.94], [99, 0.85]],
        "TE": [[25, 1.04], [29, 1.0], [99, 0.88]],
        "QB": [[26, 1.03], [32, 1.0], [99, 0.9]],
    },
    "ageStrength": 1.0,    # 0 turns the age curve off, 2 doubles it
    "round": {"1": 1.35, "2": 1.1, "3": 1.0},
    "futureWeight": {"2": 0.6, "3": 0.35},  # how much projected strength moves picks 2 and 3 seasons out
    "yearDiscount": {"2": 0.78, "3": 0.62},  # a pick 2 or 3 seasons out vs. the same slot next year
    "package": 0.75,       # package adjustment: extra pieces count less the smaller they are than the deal's best asset (0 = off)
    "playerAdj": {},       # commissioner adjustments: {sleeper id: {"name": ..., "mult": ...}}
    "classPremium": {},    # one-off premium on a single slot of one draft class: {"2027": {"1": 1.5}} = the 2027 1.01
}


def vm_package(sides, m):
    """Package-adjusted value for each side of a deal. Each side's best asset counts in full; every additional
    piece is multiplied by (its value / the best asset in the whole deal) ** package, so two dimes are not a quarter."""
    a_ = m.get("package", 0) or 0
    vmax = max([v for vals in sides for v in vals] or [0])
    out = []
    for vals in sides:
        vs = sorted(vals, reverse=True)
        out.append(round(sum(v if i == 0 or not a_ or not vmax else v * (v / vmax) ** a_ for i, v in enumerate(vs))))
    return out


def vm_curve(v, m):
    return m["scale"] * (max(v, 0) / m["scale"]) ** m["curve"] if v > 0 else 0.0


def vm_age(pos, age, m):
    steps = m["age"].get(pos)
    if not steps or not age:
        return 1.0
    mult = next((x for a, x in steps if age <= a), steps[-1][1])
    return 1 + (mult - 1) * m.get("ageStrength", 1.0)


def vm_player(fcval, pos, age, m, pid=None):
    adj = ((m.get("playerAdj") or {}).get(str(pid)) or {}).get("mult", 1.0) if pid else 1.0
    return round(vm_curve(fcval, m) * m["pos"].get(pos, 1.0) * vm_age(pos, age, m) * adj)


def vm_slot_value(slot, anchors):
    """FantasyCalc value at an exact draft slot, interpolated (in log space) through the Early/Mid/Late
    anchors at slots 2.5, 6.5 and 10.5 and extended past them on the same slope."""
    import math
    (x1, e), (x2, mi), (x3, la) = (2.5, anchors["Early"]), (6.5, anchors["Mid"]), (10.5, anchors["Late"])
    if min(e, mi, la) <= 0:
        return anchors.get("generic", 0)
    a, b = ((x1, e), (x2, mi)) if slot <= 6.5 else ((x2, mi), (x3, la))
    return math.exp(math.log(a[1]) + (slot - a[0]) * (math.log(b[1]) - math.log(a[1])) / (b[0] - a[0]))


_CACHE = {}


def get(path):
    if path in _CACHE:
        return _CACHE[path]
    req = urllib.request.Request(path if path.startswith("http") else API + path, headers={"User-Agent": "ogsn-refresh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        _CACHE[path] = json.load(r)
    return _CACHE[path]


def weekly_projections(state, scoring, rosters, players):
    """Sleeper's projections for every rostered player in the current NFL week, scored with the
    league's own scoring settings, with opponent, home/away and game day. Teams with no game are on bye."""
    season, wk = state["season"], state.get("week")
    if state.get("season_type") != "regular" or not wk:
        return None
    pos = "&".join(f"position%5B%5D={p}" for p in ("QB", "RB", "WR", "TE"))
    rows = get(f"https://api.sleeper.app/projections/nfl/{season}/{wk}?season_type=regular&{pos}")
    games = [g for g in get(f"https://api.sleeper.app/schedule/nfl/regular/{season}") if g.get("week") == wk]
    opp = {}
    for g in games:
        day = datetime.strptime(g["date"], "%Y-%m-%d").strftime("%a") if g.get("date") else ""
        opp[g["home"]] = (g["away"], 1, day)
        opp[g["away"]] = (g["home"], 0, day)
    pts = {}
    for x in rows:
        st = x.get("stats") or {}
        if x.get("opponent") and st:
            pts[x["player_id"]] = round(sum(v * scoring[k] for k, v in st.items() if k in scoring and isinstance(v, (int, float))), 2)
    out = {}
    for r in rosters:
        for pid in r.get("players") or []:
            tm = (players.get(pid) or {}).get("team")
            if not tm:
                continue
            if tm not in opp:
                out[pid] = [None, "BYE", 0, ""]
            else:
                o, home, day = opp[tm]
                out[pid] = [pts.get(pid), o, home, day]
    return {"week": wk, "asOf": datetime.now(ZoneInfo("America/Chicago")).date().isoformat(), "players": out}


def season_projections(season, scoring, rosters, players, weeks):
    """Sleeper's projections for every rostered player for each of `weeks`, scored with the league's
    settings. A player whose team has no game that week (bye) gets None."""
    pos = "&".join(f"position%5B%5D={p}" for p in ("QB", "RB", "WR", "TE"))
    rostered = {pid for r in rosters for pid in (r.get("players") or [])}
    out = {pid: [] for pid in rostered}
    for wk in weeks:
        rows = get(f"https://api.sleeper.app/projections/nfl/{season}/{wk}?season_type=regular&{pos}")
        pts = {}
        for x in rows:
            st = x.get("stats") or {}
            if x.get("opponent") and st and x["player_id"] in rostered:
                pts[x["player_id"]] = round(sum(v * scoring[k] for k, v in st.items() if k in scoring and isinstance(v, (int, float))), 2)
        for pid in rostered:
            out[pid].append(pts.get(pid))
    return {"weeks": list(weeks), "players": out}


def projected_lineups(rosters, players, ps, slots):
    """Best legal lineup by projected points for each team and week: {roster_id: [points per week]}.
    Injured-reserve players and players on bye count zero."""
    fl = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}
    out = {}
    for r in rosters:
        res = set(r.get("reserve") or [])
        roster = [q for q in (r.get("players") or []) if q not in res]
        weekly = []
        for k in range(len(ps["weeks"])):
            pool = sorted([q for q in roster if (ps["players"].get(q) or [None] * (k + 1))[k]], key=lambda q: -ps["players"][q][k])
            used, v = set(), 0.0
            for slot in sorted(slots, key=lambda x: x in fl):
                ok = fl.get(slot, {slot})
                q = next((q for q in pool if q not in used and (players.get(q) or {}).get("position") in ok), None)
                if q:
                    used.add(q); v += ps["players"][q][k]
            weekly.append(round(v, 2))
        out[r["roster_id"]] = weekly
    return out


def build(week, old):
    L = f"/league/{LEAGUE}"
    state, league, users, rosters, picks = (get(p) for p in ("/state/nfl", L, L + "/users", L + "/rosters", L + "/traded_picks"))
    players = get("/players/nfl")

    def plab(pid):
        p = players.get(pid, {})
        name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or pid
        return f"{name} ({p.get('position') or ''})"

    umap = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        u, s, m = umap.get(r["owner_id"], {}), r["settings"], r.get("metadata") or {}
        teams[str(r["roster_id"])] = {
            "id": r["roster_id"],
            "name": ((u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Team {r['roster_id']}").strip(),
            "user": u.get("display_name", ""), "w": s["wins"], "l": s["losses"], "t": s["ties"],
            "pf": round(s["fpts"] + s.get("fpts_decimal", 0) / 100, 2),
            "pa": round(s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100, 2),
            "streak": m.get("streak", ""), "form": m.get("record", ""), "allplay": [0, 0],
            "maxpf": round(s.get("ppts", 0) + s.get("ppts_decimal", 0) / 100, 2),
        }

    weeks, ppts = [], defaultdict(list)
    for w in range(1, week + 1):
        m = get(f"{L}/matchups/{w}") or []
        for x in m:
            for pid, v in (x.get("players_points") or {}).items():
                ppts[pid].append((w, v, pid in (x.get("starters") or [])))
        g = defaultdict(list)
        for x in m:
            g[x["matchup_id"]].append({"id": x["roster_id"], "pts": round(x["points"], 2)})
        weeks.append({"week": w, "games": [sorted(v, key=lambda t: -t["pts"]) for v in g.values() if len(v) == 2]})
        for a in m:
            for b in m:
                if a["roster_id"] != b["roster_id"] and a["points"] != b["points"]:
                    teams[str(a["roster_id"])]["allplay"][0 if a["points"] > b["points"] else 1] += 1
    standings = [t["id"] for t in sorted(teams.values(), key=lambda t: (-t["w"], -t["pf"]))]

    games = [(wk["week"], g) for wk in weeks for g in wk["games"]]
    flat = [{"week": w, "id": x["id"], "pts": x["pts"]} for w, g in games for x in g]
    margin = lambda wg: wg[1][0]["pts"] - wg[1][1]["pts"]
    close, blow = min(games, key=margin), max(games, key=margin)
    records = {"high": max(flat, key=lambda r: r["pts"]), "low": min(flat, key=lambda r: r["pts"]),
               "closest": {"week": close[0], "game": close[1]}, "blowout": {"week": blow[0], "game": blow[1]}}

    rounds = league["settings"].get("draft_rounds", 3)
    years = [str(int(state["season"]) + i) for i in (1, 2, 3)]
    own = {(y, r, int(rid)): int(rid) for y in years for r in range(1, rounds + 1) for rid in teams}
    for p in picks:
        k = (p["season"], p["round"], p["roster_id"])
        if k in own:
            own[k] = p["owner_id"]
    capital = {}
    for rid in teams:
        mine = [k for k, v in own.items() if v == int(rid)]
        capital[rid] = {"firsts": sum(k[1] == 1 for k in mine), "total": len(mine),
                        "byYear": {y: sorted(k[1] for k in mine if k[0] == y) for y in years}}

    wire, raw_trades = [], []
    for w in range(max(1, week - 1), week + 3):
        for x in get(f"{L}/transactions/{w}") or []:
            if x.get("status") != "complete":
                continue
            adds, drops = x.get("adds") or {}, x.get("drops") or {}
            item = {"week": w, "type": x["type"], "ts": x["status_updated"], "teams": x["roster_ids"]}
            if x["type"] == "trade":
                raw_trades.append(x)
                got = defaultdict(list)
                for pid, rid in adds.items():
                    got[rid].append(plab(pid))
                for dp in x.get("draft_picks") or []:
                    got[dp["owner_id"]].append(f"{dp['season']} Rd {dp['round']} ({teams[str(dp['roster_id'])]['name']})")
                item["sides"] = [{"id": rid, "gets": got.get(rid, [])} for rid in x["roster_ids"]]
            else:
                item["adds"] = [plab(p) for p in adds]
                item["drops"] = [plab(p) for p in drops]
                bid = (x.get("settings") or {}).get("waiver_bid")
                if bid is not None:
                    item["faab"] = bid
            wire.append(item)
    wire.sort(key=lambda i: -i["ts"])

    # Remaining regular-season schedule (playoff predictor and draft projection)
    pstart = league["settings"].get("playoff_week_start", 15)
    schedule = []
    for w in range(week + 1, pstart):
        m = get(f"{L}/matchups/{w}") or []
        g = defaultdict(list)
        for x in m:
            if x.get("matchup_id"):
                g[x["matchup_id"]].append(x["roster_id"])
        schedule.append({"week": w, "games": [v for v in g.values() if len(v) == 2]})
    starter_strength = {}
    try:  # rest-of-season value of each team's best legal lineup (Dynasty Daddy), same as the playoff predictor
        rvx = {x["sleeper_id"]: x["trade_value"] for x in get(DD_REDRAFT) if x.get("sleeper_id")}
        sl = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
        fl = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}
        for r in rosters:
            res = set(r.get("reserve") or [])
            pool = sorted([q for q in (r.get("players") or []) if q not in res and rvx.get(q)], key=lambda q: -rvx[q])
            used, v = set(), 0
            for slot in sorted(sl, key=lambda x: x in fl):
                ok = fl.get(slot, {slot})
                q = next((q for q in pool if q not in used and (players.get(q) or {}).get("position") in ok), None)
                if q:
                    used.add(q); v += rvx[q]
            starter_strength[r["roster_id"]] = v
    except Exception:
        starter_strength = {}
    proj_season, proj_lineups = old.get("projSeason"), None
    try:  # Sleeper projections for every remaining week (regular season and playoffs)
        lastwk = pstart + 2
        proj_season = season_projections(state["season"], league.get("scoring_settings") or {}, rosters, players, range(week + 1, lastwk + 1))
    except Exception as e:
        print(f"season projections unavailable: {e}", file=sys.stderr)
    if proj_season:
        slots_ = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
        proj_lineups = projected_lineups(rosters, players, proj_season, slots_)
    draft_proj = project_draft(teams, weeks, schedule, league["settings"].get("playoff_teams", 6), starter=starter_strength,
                               proj=proj_lineups, proj_weeks=proj_season["weeks"] if proj_season else None)

    fc, fc_err = None, None
    try:
        fc = get(FC_URL)
    except Exception as e:
        fc_err = str(e)
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    old_ta = {t["key"]: t for t in old.get("tradeAnalysis", [])}
    trade_analysis, team_value = list(old_ta.values()), old.get("teamValue", {})
    if fc:
        vm = dict(VALUE_DEFAULTS); vm.update((old.get("valueModel") or {}).get("params") or {})
        fcraw = {x["player"].get("sleeperId"): x["value"] for x in fc if x["player"].get("sleeperId")}
        pickv = {x["player"]["name"]: x["value"] for x in fc if x["player"]["position"] == "PICK"}
        fcv = {}
        for x in fc:
            sid = x["player"].get("sleeperId")
            if sid:
                pl_ = players.get(sid) or {}
                fcv[sid] = vm_player(x["value"], pl_.get("position") or x["player"].get("position"), pl_.get("age") or x["player"].get("maybeAge"), vm, sid)
        ordn_ = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
        pick_base = {}
        for y in years:
            pick_base[y] = {}
            for rnd in range(1, rounds + 1):
                nm = f"{y} {ordn_[rnd]}"
                b_ = {"generic": pickv.get(nm, 0)}
                for t_ in ("Early", "Mid", "Late"):
                    if f"{nm} ({t_})" in pickv:
                        b_[t_] = pickv[f"{nm} ({t_})"]
                pick_base[y][str(rnd)] = b_
        # projected strength for picks 2+ seasons out: OGSN value of each roster's players
        strength = {r["roster_id"]: sum(fcv.get(q, 0) for q in (r.get("players") or [])) for r in rosters}
        strength_rank = {rid: i + 1 for i, rid in enumerate(sorted(strength, key=lambda k: -strength[k]))}
        rank = {tid: i + 1 for i, tid in enumerate(standings)}
        nxt = years[0]
        ordn = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}

        def pick_value(season, rnd, orig):
            """OGSN pick value. Next year: expected value over the pick's projected draft slots (league rules).
            Later years: FantasyCalc's general value moved toward the Early/Mid/Late tier the original team's
            current roster strength points to, weighted less the further out. Firsts get the round premium."""
            rm = vm["round"].get(str(rnd), 1.0)
            b_ = pick_base.get(season, {}).get(str(rnd), {})
            anchors = pick_base.get(nxt, {}).get(str(rnd), {})
            if season == nxt and draft_proj.get(str(orig)) and all(k in anchors for k in ("Early", "Mid", "Late")):
                d_ = draft_proj[str(orig)]["dist"]
                cp_ = (vm.get("classPremium") or {}).get(season) or {}
                ev = sum(pp * vm_curve(vm_slot_value(sl + 1, anchors), vm) * (cp_.get(str(sl + 1), 1.0) if rnd == 1 else 1.0)
                         for sl, pp in enumerate(d_))
                slot = draft_proj[str(orig)]["avgSlot"]
                tier = "Early" if slot <= 4.5 else "Mid" if slot <= 8.5 else "Late"
                return round(ev * rm), tier
            yrs_out = str(int(season) - int(state["season"]))
            w_ = vm["futureWeight"].get(yrs_out, 0.0)
            sr = strength_rank.get(orig, 6)
            tier = "Late" if sr <= 4 else "Mid" if sr <= 8 else "Early"
            disc = vm.get("yearDiscount", {}).get(yrs_out)
            if disc is not None and anchors.get("generic") and anchors.get(tier):
                # next year's value at the projected tier (blended with next year's general value by certainty),
                # discounted for time, so a 2027 pick always beats a 2028 pick at the same slot
                base_ = disc * (w_ * anchors[tier] + (1 - w_) * anchors["generic"])
            else:
                base_ = b_.get("generic", 0)
                if anchors.get("generic") and anchors.get(tier):
                    base_ = base_ * (1 + w_ * (anchors[tier] / anchors["generic"] - 1))
            return round(vm_curve(base_, vm) * rm), (tier if w_ else None)

        def prod(pid):
            g = [v for (_, v, st) in ppts.get(pid, []) if v or st]
            return round(sum(g), 2), len(g)

        team_value = {}
        for r in rosters:
            rid = r["roster_id"]
            pv = sum(fcv.get(pid, 0) for pid in (r.get("players") or []))
            kv = sum(pick_value(k[0], k[1], k[2])[0] for k, v in own.items() if v == rid)
            team_value[str(rid)] = {"players": pv, "picks": kv, "total": pv + kv}
        order = sorted(team_value, key=lambda k: -team_value[k]["total"])
        for i, k in enumerate(order):
            team_value[k]["rank"] = i + 1

        def table_through(n):  # records and standings from weeks 1..n
            tb = {int(k): {"w": 0, "l": 0, "t": 0, "pf": 0.0} for k in teams}
            for wk in weeks[:n]:
                for g in wk["games"]:
                    hi, lo = g
                    tb[hi["id"]]["pf"] += hi["pts"]; tb[lo["id"]]["pf"] += lo["pts"]
                    if hi["pts"] == lo["pts"]:
                        tb[hi["id"]]["t"] += 1; tb[lo["id"]]["t"] += 1
                    else:
                        tb[hi["id"]]["w"] += 1; tb[lo["id"]]["l"] += 1
            order = sorted(tb, key=lambda k: (-tb[k]["w"], -tb[k]["pf"]))
            return tb, order

        def ctx(rid, leg):
            n = max(0, min(leg - 1, week))
            tb, order = table_through(n)
            t, sixth = tb[rid], tb[order[5]]
            gb = ((sixth["w"] - t["w"]) + (t["l"] - sixth["l"])) / 2
            rk = order.index(rid) + 1
            firsts = capital[str(rid)]["firsts"]
            if rk <= 4:
                mode = "contender"
            elif (rk >= 9 and firsts >= 4) or gb > 1.5:
                mode = "rebuilding"
            else:
                mode = "playoff bubble"
            return {"rank": rk, "throughWeek": n, "record": f"{t['w']}-{t['l']}" + (f"-{t['t']}" if t["t"] else ""), "pf": round(t["pf"], 2),
                    "gamesBack": max(gb, 0), "mode": mode, "firstsNow": firsts,
                    "valueRank": team_value[str(rid)]["rank"]}

        for x in raw_trades:
            key = f"{x['status_updated']}|{','.join(map(str, x['roster_ids']))}"
            if key in old_ta and not REVALUE:
                continue
            sides = []
            for rid in x["roster_ids"]:
                gets = []
                for pid, to in (x.get("adds") or {}).items():
                    if to == rid:
                        pts, gp = prod(pid)
                        pl = players.get(pid, {})
                        gets.append({"kind": "player", "label": plab(pid), "value": fcv.get(pid, 0),
                                     "age": pl.get("age"), "pts": pts, "games": gp, "ppg": round(pts / gp, 2) if gp else 0})
                for dp in x.get("draft_picks") or []:
                    if dp["owner_id"] == rid:
                        v, tier = pick_value(dp["season"], dp["round"], dp["roster_id"])
                        gets.append({"kind": "pick", "label": f"{dp['season']} Rd {dp['round']} ({teams[str(dp['roster_id'])]['name']})",
                                     "value": v, "tier": tier})
                gets.sort(key=lambda g: -g["value"])
                sides.append({"id": rid, "gets": gets, "value": sum(g["value"] for g in gets),
                              "ppgIn": round(sum(g.get("ppg", 0) for g in gets), 2),
                              "picksIn": sum(g["kind"] == "pick" for g in gets), "ctx": ctx(rid, x["leg"])})
            for s, adj in zip(sides, vm_package([[g["value"] for g in s["gets"]] for s in sides], vm)):
                s["rawValue"], s["value"] = s["value"], adj  # value = package-adjusted; rawValue = plain sum
            tot = sum(s["value"] for s in sides) or 1
            a, b = sorted(sides, key=lambda s: -s["value"])[:2] if len(sides) >= 2 else (sides[0], sides[0])
            margin = a["value"] - b["value"]
            for s in sides:
                given = [o for o in sides if o is not s]
                s["ppgOut"] = round(sum(o["ppgIn"] for o in given), 2)
                s["net"] = s["value"] - sum(o["value"] for o in given)
            entry = {
                "key": key, "ts": x["status_updated"], "week": x["leg"], "asOf": today, "sides": sides,
                "winner": a["id"] if margin / tot >= 0.10 else None, "margin": margin, "pctOfDeal": round(margin / tot * 100, 1),
                "headline": "", "takes": {}, "new": True}
            if key in old_ta:  # re-valued: keep the old write-up for review, flag it
                prev = old_ta[key]
                entry.update({"headline": prev.get("headline", ""), "takes": prev.get("takes", {}), "new": True, "revalued": True,
                              "previous": {"winner": prev.get("winner"), "pctOfDeal": prev.get("pctOfDeal")}})
                trade_analysis = [t_ for t_ in trade_analysis if t_["key"] != key]
            trade_analysis.append(entry)
    trade_analysis.sort(key=lambda t: -t["ts"])
    if fc and proj_season and draft_proj:
        # Draft-order effect: how each side's 2027 picks gain or lose value because the trade changed
        # rosters (and so projected points and Max PF). Compare current rosters with the trade reversed.
        slots_ = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
        P_ = league["settings"].get("playoff_teams", 6)
        base_l = projected_lineups(rosters, players, proj_season, slots_)
        base_d = project_draft(teams, weeks, schedule, P_, N=4000, seed=11, proj=base_l, proj_weeks=proj_season["weeks"])
        raw_by_key = {f"{x['status_updated']}|{','.join(map(str, x['roster_ids']))}": x for x in raw_trades}

        def ev27(orig, rnd, dist):
            anchors = pick_base.get(nxt, {}).get(str(rnd), {})
            if not all(k in anchors for k in ("Early", "Mid", "Late")):
                return 0.0
            cp_ = (vm.get("classPremium") or {}).get(nxt) or {}
            return sum(pp * vm_curve(vm_slot_value(sl + 1, anchors), vm) * (cp_.get(str(sl + 1), 1.0) if rnd == 1 else 1.0)
                       for sl, pp in enumerate(dist)) * vm["round"].get(str(rnd), 1.0)

        for t in trade_analysis:
            x = raw_by_key.get(t["key"])
            if not x:
                continue
            rev = [dict(r, players=list(r.get("players") or [])) for r in rosters]
            byid = {r["roster_id"]: r for r in rev}
            for pid, to in (x.get("adds") or {}).items():
                frm = (x.get("drops") or {}).get(pid)
                if frm and pid in byid[to]["players"]:
                    byid[to]["players"].remove(pid); byid[frm]["players"].append(pid)
            rev_d = project_draft(teams, weeks, schedule, P_, N=4000, seed=11,
                                  proj=projected_lineups(rev, players, proj_season, slots_), proj_weeks=proj_season["weeks"])
            for sd in t["sides"]:
                rid = sd["id"]
                mine = [k for k, v in own.items() if v == rid and k[0] == nxt]
                eff = sum(ev27(k[2], k[1], base_d[str(k[2])]["dist"]) - ev27(k[2], k[1], rev_d[str(k[2])]["dist"]) for k in mine)
                own1 = (nxt, 1, rid) in mine
                sd["draftEffect"] = {"value": round(eff), "asOf": today,
                                     "ownSlot": {"with": base_d[str(rid)]["avgSlot"], "without": rev_d[str(rid)]["avgSlot"]} if own1 else None}
    if fc:  # FantasyCalc value moved by each transaction, for "top moves"
        raw_by_key = {}
        for w in range(max(1, week - 1), week + 3):
            for x in get(f"{L}/transactions/{w}") or []:
                raw_by_key[f"{x['status_updated']}|{x['type']}|{','.join(map(str, x['roster_ids']))}"] = x
        for item in wire:
            x = raw_by_key.get(f"{item['ts']}|{item['type']}|{','.join(map(str, item['teams']))}")
            if not x:
                continue
            v = sum(fcv.get(pid, 0) for pid in (x.get("adds") or {}))
            v += sum(pick_value(dp["season"], dp["round"], dp["roster_id"])[0] for dp in (x.get("draft_picks") or []))
            item["fcValue"] = v

    # Power rankings from Dynasty Daddy values (computed here; not Dynasty Daddy's own tool)
    power, dd_err = old.get("powerRankings"), None
    try:
        ddd, ddr = get(DD_DYNASTY), get(DD_REDRAFT)
    except Exception as e:
        ddd = ddr = None
        dd_err = str(e)
    if ddd and ddr:
        dv = {x["sleeper_id"]: x["trade_value"] for x in ddd if x.get("sleeper_id")}
        rv = {x["sleeper_id"]: x["trade_value"] for x in ddr if x.get("sleeper_id")}
        slots = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
        flex_ok = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}
        pr = {}
        for r in rosters:
            rid = str(r["roster_id"])
            reserve = set(r.get("reserve") or [])
            roster = [pid for pid in (r.get("players") or [])]
            bypos = defaultdict(int)
            for pid in roster:
                bypos[(players.get(pid) or {}).get("position") or "?"] += dv.get(pid, 0)
            pool = sorted([pid for pid in roster if pid not in reserve and rv.get(pid)], key=lambda pid: -rv[pid])
            used, starters = set(), []
            for slot in sorted(slots, key=lambda s: s in flex_ok):  # fixed slots first, then flex
                ok = flex_ok.get(slot, {slot})
                pick = next((pid for pid in pool if pid not in used and (players.get(pid) or {}).get("position") in ok), None)
                if pick:
                    used.add(pick)
                    pl = players.get(pick) or {}
                    starters.append({"slot": slot, "name": pl.get("full_name") or pick, "pos": pl.get("position"), "value": rv[pick]})
            dyn = sum(dv.get(pid, 0) for pid in roster)
            picks_v = team_value.get(rid, {}).get("picks", 0)
            pr[rid] = {"players": dyn, "picks": picks_v, "total": dyn + picks_v,
                       "byPos": {k: bypos.get(k, 0) for k in ("QB", "RB", "WR", "TE")},
                       "starterValue": sum(x["value"] for x in starters), "starters": starters}
        for key, rk in (("total", "valueRank"), ("starterValue", "contenderRank")):
            for i, k in enumerate(sorted(pr, key=lambda k: -pr[k][key])):
                pr[k][rk] = i + 1
        for k, t in pr.items():
            c, v = t["contenderRank"], t["valueRank"]
            t["tier"] = ("Contender, built to last" if c <= 4 and v <= 6 else "Win-now contender" if c <= 4
                         else "In the mix" if c <= 8 else "Rebuild with assets" if v <= 6 else "Long rebuild")
        prev = (old.get("powerRankings") or {})
        if prev.get("week") is not None and prev.get("week") != week:
            prev_ranks = {k: {"valueRank": t["valueRank"], "contenderRank": t["contenderRank"]} for k, t in prev.get("teams", {}).items()}
            prev_week = prev.get("week")
        else:
            prev_ranks, prev_week = prev.get("prev", {}), prev.get("prevWeek")
        power = {"asOf": today, "week": week, "teams": pr, "prev": prev_ranks, "prevWeek": prev_week,
                 "pickSource": "FantasyCalc" if fc else None}

    # Old team names and usernames that stories may use, so the site can link them to profiles
    aliases = dict(old.get("aliases", {}))
    for k, t in teams.items():
        if t["user"]:
            aliases[t["user"]] = int(k)
        oldname = (old.get("teams", {}).get(k) or {}).get("name")
        if oldname and oldname != t["name"]:
            aliases[oldname] = int(k)

    # Trade calculator: every rostered player and owned pick with values and production
    assets = old.get("assets", {})
    if fc:
        dvm = locals().get("dv") or {}
        rvm = locals().get("rv") or {}
        assets = {}
        for r in rosters:
            rid = r["roster_id"]
            reserve = set(r.get("reserve") or [])
            taxi = set(r.get("taxi") or [])
            lineup = set(r.get("starters") or [])
            pl = []
            for pid in r.get("players") or []:
                p = players.get(pid) or {}
                pos = p.get("position") or ""
                g = [v for (_, v, st) in ppts.get(pid, []) if v or st]
                pl.append([pid, p.get("full_name") or pid, pos, p.get("age"), p.get("team") or "FA",
                           fcv.get(pid, 0), dvm.get(pid, 0), rvm.get(pid, 0),
                           round(sum(g) / len(g), 2) if g else 0, len(g), 1 if pid in reserve else 0,
                           1 if pid in lineup else 0, 1 if pid in taxi else 0, fcraw.get(pid, 0)])
            pl.sort(key=lambda x: -x[5])
            pk = []
            for (season, rnd, orig), owner in sorted(own.items()):
                if owner == rid:
                    v, tier = pick_value(season, rnd, orig)
                    pk.append([f"{season} Rd {rnd} ({teams[str(orig)]['name']})", season, rnd, orig, v, tier,
                               (draft_proj.get(str(orig)) or {}).get("avgSlot") if season == years[0] else strength_rank.get(orig)])
            assets[str(rid)] = {"players": pl, "picks": pk}

    try:
        proj = weekly_projections(state, league.get("scoring_settings") or {}, rosters, players)
    except Exception as e:  # projections are a nice-to-have; never block the refresh
        print(f"projections unavailable: {e}", file=sys.stderr)
        proj = old.get("proj")

    fresh = {
        "league": {"name": league["name"], "season": state["season"], "week": week,
                   "playoffWeek": league["settings"].get("playoff_week_start", 15),
                   "updated": datetime.now(ZoneInfo("America/Chicago")).date().isoformat()},
        "teams": teams, "standings": standings, "weeks": weeks, "records": records,
        "capital": capital, "capitalYears": years, "wire": wire,
        "editorial": old.get("editorial", {}),
        "tradeAnalysis": trade_analysis, "teamValue": team_value, "powerRankings": power,  # tradeAnalysis keeps every trade, newest first
        "images": old.get("images", {}), "recapPages": old.get("recapPages", {}),
        "office": old.get("office", {}), "awards": old.get("awards"),
        "assets": assets, "assetCols": ["id", "name", "pos", "age", "nfl", "value", "dd", "ros", "ppg", "games", "ir", "lineup", "taxi", "fcRaw"],
        "pickCols": ["label", "season", "round", "orig", "value", "tier", "avgSlot (next year) or orig team strength rank (later years)"],
        "valueModel": ({"params": vm, "pickBase": pick_base, "strengthRank": {str(k): v for k, v in strength_rank.items()}, "asOf": today}
                       if fc else old.get("valueModel")),
        "aliases": aliases,
        "schedule": schedule, "draftProjection": draft_proj, "rules": old.get("rules", {}),
        "playoffs": {"teams": league["settings"].get("playoff_teams", 6), "start": pstart,
                     "roundType": league["settings"].get("playoff_round_type", 0),
                     "seedType": league["settings"].get("playoff_seed_type", 0),
                     "tradeDeadline": league["settings"].get("trade_deadline")},
        "dd": {"asOf": today if dd_err is None else (old.get("dd") or {}).get("asOf"), "error": dd_err},
        "fc": {"asOf": today if fc else old.get("fc", {}).get("asOf"), "error": fc_err,
               "format": "Dynasty, 1QB, 12 teams, full PPR"},
        "nflState": {k: state.get(k) for k in ("week", "display_week", "season_type", "season")},
        "proj": proj, "projSeason": proj_season,
        "projLineups": {str(k): v for k, v in (proj_lineups or {}).items()} or old.get("projLineups"),
    }
    return fresh


def diff(old, fresh):
    key = lambda x: f"{x['ts']}|{x['type']}|{','.join(map(str, x['teams']))}"
    seen = {key(x) for x in old.get("wire", [])}
    new = [x for x in fresh["wire"] if key(x) not in seen]
    return {
        "trades": [x for x in new if x["type"] == "trade"],
        "adds": [x for x in new if x["type"] != "trade" and x["adds"]],
        "dropsOnly": len([x for x in new if x["type"] != "trade" and not x["adds"]]),
        "renamed": [{"id": t["id"], "from": old["teams"][k]["name"], "to": t["name"]}
                    for k, t in fresh["teams"].items() if k in old.get("teams", {}) and old["teams"][k]["name"] != t["name"]],
        "weekChanged": fresh["league"]["week"] != old["league"]["week"],
        "tradesNeedingWriteup": [t["key"] for t in fresh.get("tradeAnalysis", []) if not t.get("headline")],
        "fantasyCalcError": fresh["fc"]["error"],
        "dynastyDaddyError": fresh["dd"]["error"],
        "nflState": fresh["nflState"],
    }


def draft_order(seeds, maxpf, pf_final, wins_bracket):
    """League rule: non-playoff teams pick 1.01-1.06 in reverse Max Points For; playoff teams by
    consolation/title games (5th-place W/L 1.07/1.08, 3rd-place W/L 1.09/1.10, runner-up 1.11,
    champion 1.12); a points leader who misses the final is moved to 1.10."""
    P = len(wins_bracket["playoff"])
    non = sorted(seeds[P:], key=lambda t: maxpf[t])
    b = wins_bracket
    order = non + [b["fifthW"], b["fifthL"], b["thirdW"], b["thirdL"], b["runnerUp"], b["champ"]]
    leader = max(pf_final, key=pf_final.get)
    if leader not in (b["champ"], b["runnerUp"]):
        order.remove(leader)
        order.insert(9, leader)
    return order


def bracket(seeds, play, P=6, start=None):
    w1, w2, w3 = (start, start + 1, start + 2) if start else (None, None, None)
    s = seeds[:P]
    q1w, q1l = play(s[2], s[5], w1); q2w, q2l = play(s[3], s[4], w1)
    fifthW, fifthL = play(q1l, q2l, w2)
    lo, hi = (q1w, q2w) if s.index(q1w) > s.index(q2w) else (q2w, q1w)
    f1, s1l = play(s[0], lo, w2); f2, s2l = play(s[1], hi, w2)
    thirdW, thirdL = play(s1l, s2l, w3)
    champ, runnerUp = play(f1, f2, w3)
    return {"playoff": s, "fifthW": fifthW, "fifthL": fifthL, "thirdW": thirdW, "thirdL": thirdL,
            "champ": champ, "runnerUp": runnerUp}


def project_draft(teams, weeks, schedule, P=6, N=10000, seed=7, starter=None, proj=None, proj_weeks=None):
    """Monte Carlo of the rest of the season with the playoff predictor's model (scoring so far,
    shrunk to the league mean, blended 50/50 with best-lineup strength) -> each team's own
    next-year 1st-round slot under the league's rules, plus a classic record-based order for comparison."""
    import random, math
    rnd = random.Random(seed)
    ids = [int(k) for k in teams]
    sc = {i: [] for i in ids}
    for w in weeks:
        for g in w["games"]:
            for x in g:
                sc[x["id"]].append(x["pts"])
    allp = [v for i in ids for v in sc[i]]
    mean = sum(allp) / max(1, len(allp))
    mu = {i: (sum(sc[i]) + 3 * mean) / (len(sc[i]) + 3) for i in ids}
    if starter and all(i in starter for i in ids):
        sv = [starter[i] for i in ids]; sm = sum(sv) / len(sv)
        pp = [sum(sc[i]) / max(1, len(sc[i])) for i in ids]; pm = sum(pp) / len(pp)
        vr = sum((v - sm) ** 2 for v in sv)
        slope = max(0.0, sum((sv[k] - sm) * (pp[k] - pm) for k in range(len(ids))) / vr) if vr else 0.0
        mu = {i: 0.5 * mu[i] + 0.5 * (mean + slope * (starter[i] - sm)) for i in ids}
    own_mu = {i: (sum(sc[i]) + 3 * mean) / (len(sc[i]) + 3) for i in ids}
    wkmu = None  # per-week means from Sleeper projections of each current roster's best lineup
    if proj and proj_weeks and all(i in proj for i in ids):
        flat = [v for i in ids for v in proj[i] if v]
        k = mean / (sum(flat) / len(flat)) if flat else 1.0  # calibrate projections to the league's scoring level
        wkmu = {i: {w: 0.85 * k * proj[i][j] + 0.15 * own_mu[i] for j, w in enumerate(proj_weeks)} for i in ids}
    res = [v - sum(sc[i]) / len(sc[i]) for i in ids for v in sc[i] if sc[i]]
    sd = max(15.0, math.sqrt(sum(r * r for r in res) / max(1, len(res) - len(ids))))
    games_played = {i: max(1, len(sc[i])) for i in ids}
    gap = {i: (teams[str(i)]["maxpf"] - teams[str(i)]["pf"]) / games_played[i] for i in ids}
    dist = {i: [0] * len(ids) for i in ids}
    classic = {i: [0] * len(ids) for i in ids}
    made = {i: 0 for i in ids}; moved = {i: 0 for i in ids}; byes = {i: 0 for i in ids}; titles = {i: 0 for i in ids}
    def draw(i, w=None):
        m = wkmu[i].get(w, mu[i]) if (wkmu and w is not None) else mu[i]
        return rnd.gauss(m, sd)
    pstart = (schedule[-1]["week"] + 1) if schedule else None
    def play(a, b, w=None):
        return (a, b) if draw(a, w) >= draw(b, w) else (b, a)
    for _ in range(N):
        w = {i: teams[str(i)]["w"] for i in ids}
        pf = {i: teams[str(i)]["pf"] for i in ids}
        mx = {i: teams[str(i)]["maxpf"] for i in ids}
        for wk in schedule:
            for a, b in wk["games"]:
                sa, sb = draw(a, wk["week"]), draw(b, wk["week"])
                pf[a] += sa; pf[b] += sb; mx[a] += sa + gap[a]; mx[b] += sb + gap[b]
                w[a if sa > sb else b] += 1
        seeds = sorted(ids, key=lambda i: (-w[i], -pf[i]))
        br = bracket(seeds, play, P, pstart)
        order = draft_order(seeds, mx, pf, br)
        for slot, t in enumerate(order):
            dist[t][slot] += 1
        # classic: non-playoff teams by worst record (then fewest points), playoff teams by finish, no 1.10 rule
        corder = sorted(seeds[P:], key=lambda i: (w[i], pf[i])) + [br["fifthW"], br["fifthL"], br["thirdW"], br["thirdL"], br["runnerUp"], br["champ"]]
        for slot, t in enumerate(corder):
            classic[t][slot] += 1
        for t in seeds[:P]:
            made[t] += 1
        for t in seeds[:2]:
            byes[t] += 1
        titles[br["champ"]] += 1
        leader = max(pf, key=pf.get)
        if leader not in (br["champ"], br["runnerUp"]):
            moved[leader] += 1
    out = {}
    for i in ids:
        d = [c / N for c in dist[i]]
        c = [x / N for x in classic[i]]
        out[str(i)] = {"avgSlot": round(sum((k + 1) * p for k, p in enumerate(d)), 2), "dist": [round(p, 4) for p in d],
                       "classicAvgSlot": round(sum((k + 1) * p for k, p in enumerate(c)), 2),
                       "playoffs": round(made[i] / N, 4), "bye": round(byes[i] / N, 4), "title": round(titles[i] / N, 4),
                       "movedTo110": round(moved[i] / N, 4)}
    return out


# ---- Weekly recap facts: everything the recap writer needs, computed, so no number is guessed ----
NFLVERSE_GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
WINDOWS = ["Start", "Thu–Sat", "Sun early", "Sun late", "SNF", "MNF"]


def kickoff_windows(season, week):
    """Map each NFL team to its game window index (1-5) for this week, from nflverse kickoff times (ET)."""
    import csv, io
    rows = csv.DictReader(io.StringIO(wget(NFLVERSE_GAMES).decode()))
    win, games = {}, []
    for g in rows:
        if g["season"] != str(season) or g["week"] != str(week) or g["game_type"] not in ("REG", "WC", "DIV", "CON", "SB"):
            continue
        day, t = g["weekday"], g["gametime"]
        if day in ("Thursday", "Friday", "Saturday"):
            i = 1
        elif day == "Sunday":
            i = 2 if t < "16:00" else 3 if t < "19:00" else 4
        else:
            i = 5
        for tm in (g["away_team"], g["home_team"]):
            win[{"LA": "LAR", "WAS": "WAS"}.get(tm, tm)] = i
        games.append({"away": g["away_team"], "home": g["home_team"], "day": day, "time": t,
                      "location": g.get("location", ""), "score": f"{g['away_score']}-{g['home_score']}", "window": WINDOWS[i]})
    return win, games


def recap_facts(week):
    L = f"/league/{LEAGUE}"
    state, league, users, rosters = (get(p) for p in ("/state/nfl", L, L + "/users", L + "/rosters"))
    players = get("/players/nfl")
    umap = {u["user_id"]: u for u in users}
    name = {r["roster_id"]: ((umap.get(r["owner_id"], {}).get("metadata") or {}).get("team_name")
                             or umap.get(r["owner_id"], {}).get("display_name") or f"Team {r['roster_id']}").strip() for r in rosters}
    slots = [p for p in league["roster_positions"] if p not in ("BN", "IR", "TAXI")]
    flex_ok = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}
    win, nflgames = kickoff_windows(state["season"], week)
    pinfo = lambda pid: players.get(pid) or {}
    pname = lambda pid: pinfo(pid).get("full_name") or pid
    ppos = lambda pid: pinfo(pid).get("position") or "DEF"
    pteam = lambda pid: pinfo(pid).get("team") or ""
    m = get(f"{L}/matchups/{week}") or []
    if not m or not any(x["points"] for x in m):
        raise SystemExit(f"week {week} has no scores yet")
    try:  # Sleeper's projection for that week, scored with the league's settings (what each manager saw when setting the lineup)
        wkproj = season_projections(state["season"], league.get("scoring_settings") or {}, get(L + "/rosters"), players, [week])
    except Exception:
        wkproj = {}
    def pj(pid):
        v = (((wkproj or {}).get("players") or {}).get(pid) or [None])[0]
        return round(v, 1) if v is not None else None
    def inj(pid):
        pl = pinfo(pid)
        return (pl.get("injury_status") + (f" ({pl['injury_body_part']})" if pl.get("injury_body_part") else "")) if pl.get("injury_status") else None

    def best(x):
        pts = x.get("players_points") or {}
        pool = sorted(pts, key=lambda pid: -pts[pid]); used, lineup = set(), []
        for sl in sorted(slots, key=lambda s: s in flex_ok):
            ok = flex_ok.get(sl, {sl})
            pick = next((pid for pid in pool if pid not in used and (set(pinfo(pid).get("fantasy_positions") or [ppos(pid)]) & ok)), None)
            if pick:
                used.add(pick); lineup.append(pick)
        return round(sum(pts[p] for p in lineup), 2), lineup

    teams = {}
    for x in m:
        pts = x.get("players_points") or {}
        st = [p for p in (x.get("starters") or []) if p and p != "0"]
        flow = [0.0] * 6
        for p in st:
            flow[win.get(pteam(p), 2)] += pts.get(p, 0)
        cum = [round(sum(flow[:i + 1]), 2) for i in range(6)]
        bv, bl = best(x)
        bench = sorted([p for p in pts if p not in st], key=lambda p: -pts[p])
        teams[x["roster_id"]] = {
            "name": name[x["roster_id"]], "matchup": x["matchup_id"], "points": round(x["points"], 2), "flow": cum,
            "starters": [{"name": pname(p), "pos": ppos(p), "nfl": pteam(p), "window": WINDOWS[win.get(pteam(p), 2)], "pts": pts.get(p, 0),
                          "proj": pj(p), "injuryNow": inj(p)} for p in st],
            "bench": [{"name": pname(p), "pos": ppos(p), "nfl": pteam(p), "pts": pts[p], "proj": pj(p), "injuryNow": inj(p)} for p in bench if pts[p] > 0 or pj(p)],
            "zeroStarters": [pname(p) for p in st if not pts.get(p)],
            "bestLineup": bv, "leftOnBench": round(bv - x["points"], 2),
            "bestLineupPlayers": [pname(p) for p in bl],
        }
    games = defaultdict(list)
    for rid, t in teams.items():
        games[t["matchup"]].append(rid)
    out_games = []
    for mid, (a, b) in ((k, v) for k, v in games.items() if len(v) == 2):
        A, B = teams[a], teams[b]
        w, l = (a, b) if A["points"] >= B["points"] else (b, a)
        lead = [round(A["flow"][i] - B["flow"][i], 2) for i in range(6)]
        out_games.append({"winner": w, "loser": l, "margin": round(abs(A["points"] - B["points"]), 2),
                          "flipsWithBestLineup": teams[l]["bestLineup"] > teams[w]["points"],
                          "leadByWindow": {"team": a, "values": lead}, "teams": [a, b]})
    allp = sorted(((p["pts"], p["name"], teams[r]["name"], "starter") for r in teams for p in teams[r]["starters"]), reverse=True)
    bp = sorted(((p["pts"], p["name"], teams[r]["name"], "bench") for r in teams for p in teams[r]["bench"]), reverse=True)
    tx = [x for x in (get(f"{L}/transactions/{week}") or []) + (get(f"{L}/transactions/{week + 1}") or []) if x.get("status") == "complete"]
    txs = []
    for x in sorted(tx, key=lambda x: x["status_updated"]):
        txs.append({"type": x["type"], "teams": [name[r] for r in x["roster_ids"]],
                    "adds": {pname(p): name[r] for p, r in (x.get("adds") or {}).items()},
                    "drops": {pname(p): name[r] for p, r in (x.get("drops") or {}).items()},
                    "picks": [f"{d['season']} Rd {d['round']} ({name[d['roster_id']]}) to {name[d['owner_id']]}" for d in x.get("draft_picks") or []],
                    "faab": (x.get("settings") or {}).get("waiver_bid")})
    # surprises and duds among starters: points against that week's projection
    vs = [(round(p["pts"] - p["proj"], 1), p["name"], teams[r]["name"], p["pts"], p["proj"]) for r in teams for p in teams[r]["starters"] if p.get("proj")]
    vs.sort(reverse=True)
    return {"week": week, "season": state["season"], "playoffWeekStart": league["settings"].get("playoff_week_start"),
            "windows": WINDOWS, "nflGames": nflgames, "teams": teams, "games": out_games,
            "topStarters": allp[:12], "topBench": bp[:8], "transactions": txs,
            "surprises": [{"name": n, "team": t, "pts": a, "proj": b, "overProj": d} for d, n, t, a, b in vs[:8] if d > 0],
            "duds": [{"name": n, "team": t, "pts": a, "proj": b, "underProj": -d} for d, n, t, a, b in vs[::-1][:8] if d < 0],
            "projectionNote": "proj is Sleeper's projection for that week scored with league settings, what managers saw when setting lineups; injuryNow is the player's injury status when this file was built",
            "teamNames": name, "note": "Flow arrays are cumulative starter points after each window: " + ", ".join(WINDOWS)}


# ---- Import a weekly recap artifact (the "Ordinary Gentleman Week N" page) into recapPages ----
def parse_recap(html_text, week, url):
    import re, html as H
    txt = lambda frag: re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", "", frag))).strip()
    dek = re.search(r'<p class="dek">(.*?)</p>', html_text, re.S)
    stories = [{"h": txt(h), "body": txt(b)} for h, b in
               re.findall(r'<article class="story">\s*<h3>(.*?)</h3>\s*<p>(.*?)</p>\s*</article>', html_text, re.S)]
    awards = [{"a": txt(a), "who": txt(w), "p": txt(pp)} for a, w, pp in
              re.findall(r'<div class="an">(.*?)</div><div class="who">(.*?)</div><p>(.*?)</p>', html_text, re.S)]
    note = re.search(r'<section id="matchups">.*?<p class="note">(.*?)</p>', html_text, re.S)
    m = re.search(r"const G=(\[.*?\n\]);", html_text, re.S)
    games = []
    if m:
        js = m.group(1)
        js = re.sub(r"`((?:[^`\\]|\\.)*)`", lambda mm: json.dumps(mm.group(1)), js, flags=re.S)  # template strings
        js = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', js)  # bare keys
        js = re.sub(r",\s*([}\]])", r"\1", js)
        for g in json.loads(js):
            games.append({"a": g["a"], "b": g["b"], "h": g["h"], "tag": g.get("tag", ""), "hot": bool(g.get("hot")),
                          "t": [txt(x) for x in g["t"]], "v": txt(g.get("v", ""))})
    if not (stories and games):
        raise SystemExit("recap page did not match the expected layout; import nothing and report it")
    return {"week": week, "url": url, "dek": txt(dek.group(1)) if dek else "", "stories": stories,
            "games": games, "awards": awards, "note": txt(note.group(1)) if note else ""}


# ---- Freely licensed photos (Wikimedia Commons, via the player's English Wikipedia infobox image) ----
WIKI_UA = "OGSN-league-site/1.0 (private fantasy league page; contact via Wikimedia talk)"
FREE = ("cc0", "public domain", "pd", "cc by", "cc-by")


def wget(url):
    import time, urllib.error
    for attempt in range(5):
        time.sleep(1.5 + attempt * 4)  # stay well under Wikimedia's rate limits
        try:
            req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise


def find_image(query, title=None):
    """Return metadata for a free photo of `query` (a Wikipedia article title or search text)."""
    import urllib.parse, re, html as H
    api = "https://en.wikipedia.org/w/api.php?format=json&"
    if not title:
        q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query, "srlimit": 5})
        hits = json.loads(wget(api + q))["query"]["search"]
        if not hits:
            raise SystemExit(f"no Wikipedia article for {query!r}")
        title = hits[0]["title"]
    q = urllib.parse.urlencode({"action": "query", "titles": title, "prop": "pageimages|description", "piprop": "name", "redirects": 1})
    page = next(iter(json.loads(wget(api + q))["query"]["pages"].values()))
    name = page.get("pageimage")
    if not name:
        raise SystemExit(f"article {title!r} has no lead image")
    capi = "https://commons.wikimedia.org/w/api.php?format=json&"
    q = urllib.parse.urlencode({"action": "query", "titles": "File:" + name, "prop": "imageinfo",
                                "iiprop": "url|extmetadata", "iiurlwidth": 960})
    ip = next(iter(json.loads(wget(capi + q))["query"]["pages"].values()))
    if "imageinfo" not in ip:
        raise SystemExit(f"{name} is not on Wikimedia Commons (likely non-free); pick another image")
    ii = ip["imageinfo"][0]; md = ii.get("extmetadata", {})
    val = lambda k: re.sub(r"<[^>]+>", "", H.unescape((md.get(k) or {}).get("value", ""))).strip()
    lic = val("LicenseShortName")
    if not any(lic.lower().startswith(f) for f in FREE):
        raise SystemExit(f"{name}: license {lic!r} is not a free license")
    return {"article": page.get("title"), "description": page.get("description", ""), "file": name,
            "thumb": ii.get("thumburl") or ii["url"], "page": ii.get("descriptionurl"),
            "artist": val("Artist") or "Unknown", "license": lic, "licenseUrl": val("LicenseUrl"),
            "date": val("DateTimeOriginal")[:10]}


def save_image(meta, out_dir, slug):
    import os
    os.makedirs(out_dir, exist_ok=True)
    import shutil, subprocess
    raw = os.path.join(out_dir, slug + ".download")
    open(raw, "wb").write(wget(meta["thumb"]))
    path = os.path.join(out_dir, slug + ".jpg")
    if shutil.which("sips"):  # macOS: convert to a compact JPEG, longest side 960px
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72", "-Z", "960", raw, "--out", path],
                       check=True, capture_output=True)
        os.remove(raw)
    else:
        os.replace(raw, path)
    meta["src"] = f"images/{slug}.jpg"
    meta["localPath"] = path
    return meta


AWARD_KEYS = {"moty": "m", "mvp": "p", "best": "m", "worst": "m", "qdm": "m", "fleece": "m", "panic": "m", "chat": "m", "ghost": "m", "gent": "m"}
# how each award's question is recognized in the form (normalized header contains the key; "exclude" keeps MVP and Group Chat MVP apart)
AWARD_MATCH = {"moty": ("manageroftheyear", None), "mvp": ("mvp", "groupchat"), "best": ("besttotrade", None),
               "worst": ("worsttotrade", None), "qdm": ("questionabledecision", None), "fleece": ("fleece", None),
               "panic": ("panic", None), "chat": ("groupchat", None), "ghost": ("ghost", None), "gent": ("gentleman", None)}


def _norm(x):
    import re
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())


def _form_rows(csv_path):
    """Google Form responses exported as CSV: [(datetime in America/Chicago, {header: value})]."""
    import csv
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            ts = (r.get("Timestamp") or "").strip()
            t = None
            for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M"):
                try:
                    t = datetime.strptime(ts, fmt).replace(tzinfo=ZoneInfo("America/Chicago"))
                    break
                except ValueError:
                    pass
            if t:
                rows.append((t, r))
    return sorted(rows, key=lambda x: x[0])


def tally_awards(csv_path, codes, data):
    """Season totals for the League Awards from the ballot form's responses.
    A ballot counts for the latest NFL week whose last game came before the day it was sent (Tuesday through Monday),
    and only weeks whose voting has closed are counted, so the totals change once a week (Tuesday);
    one ballot per team per week (the latest one sent), identified by the team's private code. Votes for the voter's own
    team, for unknown teams or for players not on a league roster are ignored."""
    season = data["league"].get("season")
    last = defaultdict(str)
    for g in get(f"https://api.sleeper.app/schedule/nfl/regular/{season}") or []:
        last[g["week"]] = max(last[g["week"]], g.get("date") or "")
    def week_of(t):
        d = t.date().isoformat()
        done = [w for w, ld in last.items() if ld and ld < d]
        return max(done) if done else None
    from datetime import date, timedelta
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    def closes(wk):  # a week's ballots close the night the following NFL week ends (Monday)
        nxt = last.get(wk + 1)
        return date.fromisoformat(nxt) if nxt else date.fromisoformat(last[wk]) + timedelta(days=7)
    closed = sorted(w for w in last if closes(w) < today)
    open_wk = week_of(datetime.now(ZoneInfo("America/Chicago")))
    next_update = (closes(open_wk) if open_wk else date.fromisoformat(last[min(last)])) + timedelta(days=1)
    teams = {str(k): v for k, v in data["teams"].items()}
    tname = {}
    for k, t in teams.items():
        tname[_norm(t["name"])] = k
        tname[_norm(t.get("user"))] = k
    for k, al in (data.get("aliases") or {}).items():
        for a in (al if isinstance(al, list) else [al]):
            tname.setdefault(_norm(a), str(k))
    players = {}
    for a in data.get("assets", {}).values():
        for p in a["players"]:
            players[p[0]] = p[1]
    pfull = {_norm(n): i for i, n in players.items()}
    plast = defaultdict(list)
    for i, n in players.items():
        plast[_norm(n.split()[-1])].append(i)
    def player_id(txt):
        n = _norm(txt)
        if not n:
            return None
        if n in pfull:
            return pfull[n]
        hit = [i for k, i in pfull.items() if n in k] or plast.get(n, [])
        words = str(txt).split()
        if not hit and len(words) >= 2:  # nicknames: "Ken Walker" for Kenneth Walker, "Jax Smith-Njigba"
            f0, l0 = _norm(words[0]), _norm(" ".join(words[1:]))
            hit = [i for i, nm in players.items() if _norm(" ".join(nm.split()[1:])) == l0
                   and (_norm(nm.split()[0]).startswith(f0) or f0.startswith(_norm(nm.split()[0])))]
        return hit[0] if len(set(hit)) == 1 else None
    code_team = {_norm(c): str(t) for c, t in codes.items()}
    latest = {}
    for t, r in _form_rows(csv_path):
        code = next((v for h, v in r.items() if h and "code" in _norm(h)), "")
        team, wk = code_team.get(_norm(code)), week_of(t)
        if team and wk and wk in closed:  # only weeks whose voting has closed are counted and shown
            latest[(team, wk)] = r
    counts = {k: defaultdict(int) for k in AWARD_KEYS}
    ballots = defaultdict(int)
    for (team, wk), r in latest.items():
        used = False
        for k, (key, excl) in AWARD_MATCH.items():
            v = next((v for h, v in r.items() if h and key in _norm(h) and not (excl and excl in _norm(h))), "")
            if not v or _norm(v) in ("novote", "none", ""):
                continue
            if AWARD_KEYS[k] == "m":
                vid = tname.get(_norm(v))
                if not vid or vid == team:
                    continue
            else:
                vid = player_id(v)
                if not vid:
                    continue
            counts[k][vid] += 1
            used = True
        if used:
            ballots[f"w{wk}"] += 1
    out = {}  # only the current leader of each award (all of them on a tie); vote counts are never published
    for k, c in counts.items():
        top = max(c.values()) if c else 0
        lead = sorted(i for i, n in c.items() if n == top and top > 0)
        out[k] = [[i, players.get(i, "")] if AWARD_KEYS[k] == "p" else [i] for i in lead]
    return {"asOf": today.isoformat(), "season": season, "throughWeek": max(closed) if closed else None,
            "nextUpdate": next_update.isoformat(), "hasVotes": sum(ballots.values()) > 0, "awards": out}



def recap_odds(data, week, facts=None):
    """Each team's playoff, bye and title odds and standing just before week `week` was played and just after,
    from the site's simulator. 'Before' takes week `week` back out of the results and plays it again."""
    teams = {k: dict(v) for k, v in data["teams"].items()}
    weeks = [w for w in data["weeks"] if w["week"] <= week]
    sched = [s for s in data.get("schedule", []) if s["week"] > week]
    proj, pw = data.get("projLineups"), (data.get("projSeason") or {}).get("weeks")
    projI = {int(k): v for k, v in proj.items()} if proj else None
    P = (data.get("playoffs") or {}).get("teams", 6)
    def table(tm):
        order = sorted(tm, key=lambda k: (-tm[k]["w"], -tm[k]["pf"]))
        six = tm[order[P - 1]]
        return {k: {"rank": order.index(k) + 1, "record": f"{tm[k]['w']}-{tm[k]['l']}" + (f"-{tm[k]['t']}" if tm[k].get("t") else ""),
                    "gamesBackOfLastPlayoffSpot": max(0.0, ((six["w"] - tm[k]["w"]) + (tm[k]["l"] - six["l"])) / 2)} for k in tm}
    after = project_draft(teams, weeks, sched, P, N=10000, seed=11, proj=projI, proj_weeks=pw)
    tafter = table(teams)
    wk = next((w for w in weeks if w["week"] == week), None)
    if not wk:
        raise SystemExit(f"week {week} results are not in the data")
    bt = {k: dict(v) for k, v in teams.items()}
    fbest = {}
    if facts:
        fbest = {str(k): v.get("bestLineup") for k, v in facts.get("teams", {}).items()}
    for a, b in wk["games"]:
        ta, tb = bt[str(a["id"])], bt[str(b["id"])]
        (ta if a["pts"] > b["pts"] else tb)["w"] -= 1
        (tb if a["pts"] > b["pts"] else ta)["l"] -= 1
        for t, x in ((ta, a), (tb, b)):
            best = fbest.get(str(x["id"])) or x["pts"] * (t["maxpf"] / t["pf"] if t["pf"] else 1)
            t["pf"] -= x["pts"]; t["maxpf"] -= best
    before = project_draft(bt, [w for w in weeks if w["week"] < week],
                           [{"week": week, "games": [[a["id"], b["id"]] for a, b in wk["games"]]}] + sched, P, N=10000, seed=11, proj=projI, proj_weeks=pw)
    tbefore = table(bt)
    pct = lambda x: round(100 * x)
    return {"week": week, "note": "Odds in percent from 10,000 simulated seasons. 'before' replays this week with the same rosters; small swings are noise.",
            "teams": {data["teams"][k]["name"]: {"before": {**tbefore[k], "playoffs": pct(before[k]["playoffs"]), "bye": pct(before[k]["bye"]), "title": pct(before[k]["title"])},
                                                 "after": {**tafter[k], "playoffs": pct(after[k]["playoffs"]), "bye": pct(after[k]["bye"]), "title": pct(after[k]["title"])}}
                      for k in teams}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "assemble", "image", "recap", "recapdata", "recapodds", "proj", "tally"])
    ap.add_argument("--facts")
    ap.add_argument("--csv"); ap.add_argument("--codes")
    ap.add_argument("--html"); ap.add_argument("--url"); ap.add_argument("--revalue", action="store_true")
    ap.add_argument("--query"); ap.add_argument("--title"); ap.add_argument("--slug"); ap.add_argument("--dir")
    ap.add_argument("--old"); ap.add_argument("--out"); ap.add_argument("--week", type=int)
    ap.add_argument("--data"); ap.add_argument("--template")
    a = ap.parse_args()
    if a.cmd == "recapodds":  # playoff, bye and title odds and standing before and after week N (--data fresh.json, optional --facts)
        json.dump(recap_odds(json.load(open(a.data)), a.week, json.load(open(a.facts)) if a.facts else None), open(a.out, "w"), indent=1, ensure_ascii=False)
        print("wrote", a.out)
        return
    if a.cmd == "recapdata":  # facts for writing week N's recap
        json.dump(recap_facts(a.week), open(a.out, "w"), indent=1, ensure_ascii=False)
        print("wrote", a.out)
        return
    if a.cmd == "recap":  # adds/replaces recapPages[week] in --data
        d = json.load(open(a.data))
        d.setdefault("recapPages", {})[str(a.week)] = parse_recap(open(a.html).read(), a.week, a.url)
        json.dump(d, open(a.data, "w"), separators=(",", ":"), ensure_ascii=False)
        r = d["recapPages"][str(a.week)]
        print(f"imported week {a.week}: {len(r['stories'])} stories, {len(r['games'])} games, {len(r['awards'])} awards")
        return
    if a.cmd == "proj":  # replaces proj in --data
        d = json.load(open(a.data))
        L = f"/league/{LEAGUE}"
        d["proj"] = weekly_projections(get("/state/nfl"), get(L).get("scoring_settings") or {}, get(L + "/rosters"), get("/players/nfl"))
        json.dump(d, open(a.data, "w"), separators=(",", ":"), ensure_ascii=False)
        p = d["proj"] or {}
        print(f"projections for week {p.get('week')}: {sum(v[0] is not None for v in p.get('players', {}).values())} projected, "
              f"{sum(v[1] == 'BYE' for v in p.get('players', {}).values())} on bye")
        return
    if a.cmd == "tally":  # League Awards: ballot form responses (--csv) + private team codes (--codes) -> --data["awards"]
        d = json.load(open(a.data))
        cfg = json.load(open(a.codes))
        r = tally_awards(a.csv, cfg.get("codes", cfg), d)
        first = cfg.get("firstPublish")
        if first and r["asOf"] < first:  # before the first publish date the site gets no counts at all
            r = {"asOf": r["asOf"], "season": r["season"], "held": True, "nextUpdate": first, "throughWeek": None, "hasVotes": False, "awards": {}}
        prev = d.get("awards") or {}
        changed = any(prev.get(k) != r.get(k) for k in ("awards", "hasVotes", "throughWeek", "nextUpdate", "held"))
        if changed:
            d["awards"] = r
            json.dump(d, open(a.data, "w"), separators=(",", ":"), ensure_ascii=False)
        print(json.dumps({"awardsChanged": changed, "throughWeek": r["throughWeek"], "nextUpdate": r["nextUpdate"], "hasVotes": r["hasVotes"]}))
        return
    if a.cmd == "image":
        meta = save_image(find_image(a.query, a.title), a.dir or "images", a.slug)
        print(json.dumps(meta, indent=1, ensure_ascii=False))
        return
    if a.cmd == "fetch" and a.revalue:
        global REVALUE
        REVALUE = True
    if a.cmd == "fetch":
        old = json.load(open(a.old))
        try:
            fresh = build(a.week or old["league"]["week"], old)
        except Exception as e:  # network blocked, Sleeper down
            print(json.dumps({"error": f"Sleeper unreachable: {e}"})); sys.exit(3)
        json.dump(fresh, open(a.out, "w"), separators=(",", ":"), ensure_ascii=False)
        print(json.dumps(diff(old, fresh), indent=1, ensure_ascii=False))
    else:
        data = open(a.data).read().replace("</", "<\\/")
        tpl = open(a.template).read()
        assert "__LEAGUE_DATA__" in tpl
        open(a.out, "w").write(tpl.replace("__LEAGUE_DATA__", data))
        print("wrote", a.out)


if __name__ == "__main__":
    main()
