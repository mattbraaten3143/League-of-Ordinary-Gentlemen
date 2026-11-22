"""League news items from the daily refresh. No AI: facts only, straight from the data.

    python3 scripts/news.py OLD.json FRESH.json

Compares yesterday's data (OLD) with today's (FRESH) and adds items to FRESH's
editorial.news, the "Latest" feed on the site:
  - every new trade: who got what, then each team's record, place and first-round picks
  - waiver / free-agent adds with a FAAB bid of $10 or more (one item per day)
  - team renames
Then drops items older than 8 days and keeps at most 8. Prints how many it added.
Same rules the artifact's daily refresh task used, so the feed reads the same.
"""
import json, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KEEP_DAYS, KEEP_MAX, MIN_BID = 8, 8, 10


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def plain(label):  # "Ollie Gordon II (RB)" -> "Ollie Gordon II"; "2027 Rd 3 (PhilthyPhil)" stays as is
    head, _, tail = label.rpartition(" (")
    return head if head and tail.rstrip(")") in ("QB", "RB", "WR", "TE", "K", "DEF", "") else label


def join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else "nothing"


def wire_key(x):
    return f"{x['ts']}|{x['type']}|{','.join(map(str, x['teams']))}"


def make_items(old, fresh, today):
    T = fresh["teams"]
    name = lambda i: T[str(i)]["name"]
    rec = lambda i: f"{T[str(i)]['w']}-{T[str(i)]['l']}" + (f"-{T[str(i)]['t']}" if T[str(i)].get("t") else "")
    place = lambda i: ordinal([str(s) for s in fresh["standings"]].index(str(i)) + 1)
    firsts = lambda i: (fresh.get("capital", {}).get(str(i)) or {}).get("firsts", 0)

    seen = {wire_key(x) for x in old.get("wire", [])}
    new = sorted((x for x in fresh.get("wire", []) if wire_key(x) not in seen), key=lambda x: x["ts"])
    items = []

    for x in (x for x in new if x["type"] == "trade"):
        sides = x["sides"]
        got = " ".join(f"{name(s['id'])} get {join(plain(g) for g in s['gets'])}." for s in sides)
        ctx = " ".join(f"{name(s['id'])} are {rec(s['id'])}, {place(s['id'])}, with "
                       f"{firsts(s['id'])} first-round pick{'s' if firsts(s['id']) != 1 else ''}." for s in sides)
        h = f"{name(sides[0]['id'])} and {join(name(s['id']) for s in sides[1:])} make a trade"
        items.append({"date": today, "tag": "Trade", "teams": [s["id"] for s in sides], "h": h, "body": f"{got} {ctx}"})

    bids = [x for x in new if x["type"] in ("waiver", "free_agent") and x.get("adds") and (x.get("faab") or 0) >= MIN_BID]
    if bids:
        bids.sort(key=lambda x: -x["faab"])
        top = bids[0]
        h = f"{name(top['teams'][0])} spends ${top['faab']} on {plain(top['adds'][0])}"
        lines = []
        for x in bids:
            s = f"{name(x['teams'][0])} paid ${x['faab']} for {join(plain(a) for a in x['adds'])}"
            if x.get("drops"):
                s += f" and dropped {join(plain(d) for d in x['drops'])}"
            lines.append(s + ".")
        teams = list(dict.fromkeys(x["teams"][0] for x in bids))
        ctx = " ".join(f"{name(i)} are {rec(i)}, {place(i)}." for i in teams)
        items.append({"date": today, "tag": "Waivers", "teams": teams, "h": h, "body": " ".join(lines) + " " + ctx})

    for k, t in T.items():
        o = old.get("teams", {}).get(k)
        if o and o["name"] != t["name"]:
            items.append({"date": today, "tag": "Rename", "teams": [t["id"]],
                          "h": f"{o['name']} is now {t['name']}",
                          "body": f"The team formerly known as {o['name']} now goes by {t['name']}. Same manager, @{t['user']}."})
    return items


def main(old_path, fresh_path):
    old, fresh = json.load(open(old_path)), json.load(open(fresh_path))
    # refresh.py rebuilds a fixed set of fields; keep anything else the site stores (League Wire, injury snapshot)
    for k, v in old.items():
        fresh.setdefault(k, v)
    now = datetime.now(ZoneInfo("America/Chicago"))
    today = now.strftime("%Y-%m-%d")
    ed = fresh.setdefault("editorial", {})
    news = ed.get("news", [])
    added = make_items(old, fresh, today)
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    news = sorted([n for n in added + news if n.get("date", "") >= cutoff], key=lambda n: n["date"], reverse=True)[:KEEP_MAX]
    if added or news != ed.get("news", []):
        ed["news"] = news
    json.dump(fresh, open(fresh_path, "w"), separators=(",", ":"), ensure_ascii=False)
    print(json.dumps({"newsAdded": [i["h"] for i in added], "newsKept": len(news)}))


if __name__ == "__main__":
    main(*sys.argv[1:3])
