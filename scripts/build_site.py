"""Build the real website's index.html from the artifact's page template.

    python3 scripts/build_site.py

Reads scripts/hub_template.txt (the same template the Claude artifact uses) and
public/data/league-data.json, and writes public/index.html as a full web page that:
  - loads public/live.js, which refreshes scores and standings from Sleeper
    before the page draws, then keeps the score ticker live on game days;
  - otherwise looks and works exactly like the artifact.

The template is left untouched, so it can keep being copied over from the artifact.
Each change below is a small find-and-replace; if the template changes so much that
one no longer matches, the build stops and says which one, instead of shipping a
half-patched page.
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "scripts", "hub_template.txt")
DATA = os.path.join(ROOT, "public", "data", "league-data.json")
OUT = os.path.join(ROOT, "public", "index.html")

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="description" content="The Ordinary Gentleman Sports Network: live scores, standings, trade grades and dynasty tools for The League of Ordinary Gentleman.">
<meta name="theme-color" content="#0F2B23">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
</head>
<body>
"""
TAIL = """
<script src="/live.js"></script>
</body>
</html>
"""

# (label, find, replace). Each must match exactly once.
PATCHES = [
    ("hold the app until live data is in",
     '<script>\nconst D=JSON.parse(document.getElementById("league-data").textContent);',
     '<script type="text/plain" id="ogsn-app">\nconst D=JSON.parse(document.getElementById("league-data").textContent);'),
    ("masthead status shows the live week",
     '$("status").textContent=`${D.league.season} · Week ${W} final`;',
     '$("status").textContent=D.live&&D.live.state==="live"?`${D.league.season} · Week ${D.live.week} live`'
     ':`${D.league.season} · Week ${D.weeks.length?D.weeks[D.weeks.length-1].week:W} final`;'),
    ("week picker lists the live week",
     'sel.innerHTML=D.weeks.map(',
     'sel.innerHTML=(D.live?`<option value="${D.live.week}">Week ${D.live.week} · ${D.live.state==="live"?"Live":"This week"}</option>`:"")+D.weeks.map('),
    ("week picker opens on the newest week",
     'sel.value=W;',
     'sel.value=D.live?D.live.week:(D.weeks.length?D.weeks[D.weeks.length-1].week:W);'),
    ("ticker can show the live week",
     'const w=D.weeks.find(x=>x.week==wk);',
     'const w=D.weeks.find(x=>x.week==wk)||(D.live&&D.live.week==wk?D.live:null);'),
    ("ticker labels live games",
     '<span class="st">FINAL · WK ${wk}',
     '<span class="st">${w.label||"FINAL"} · WK ${wk}'),
    ("home standings box names the newest finished week",
     '$("hstwk").textContent=`After Week ${W}`;',
     '$("hstwk").textContent=`After Week ${D.weeks.length?D.weeks[D.weeks.length-1].week:W}`;'),
    ("standings page names the newest finished week",
     'phead("ph-standings","standings",`After Week ${W}`,',
     'phead("ph-standings","standings",`After Week ${D.weeks.length?D.weeks[D.weeks.length-1].week:W}`,'),
    ("playoff odds name the newest finished week",
     '`After Week ${W} · ${SCHED.length} weeks left`',
     '`After Week ${D.weeks.length?D.weeks[D.weeks.length-1].week:W} · ${SCHED.length} weeks left`'),
    ("ticker starts on the picked week",
     '\nstrip(W);\n',
     '\nstrip(sel.value);\n'),
]


def build():
    tpl = open(TEMPLATE, encoding="utf-8").read()
    for label, find, repl in PATCHES:
        n = tpl.count(find)
        if n != 1:
            sys.exit(f"build_site: patch '{label}' matched {n} times (expected 1). The template changed; update PATCHES.")
        tpl = tpl.replace(find, repl)
    assert "__LEAGUE_DATA__" in tpl, "template is missing __LEAGUE_DATA__"
    data = json.load(open(DATA, encoding="utf-8"))
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = HEAD + tpl.replace("__LEAGUE_DATA__", blob) + TAIL
    open(OUT, "w", encoding="utf-8").write(html)
    print(f"wrote {os.path.relpath(OUT, ROOT)} ({len(html) // 1024} KB, data through week {data['league']['week']})")


if __name__ == "__main__":
    build()
