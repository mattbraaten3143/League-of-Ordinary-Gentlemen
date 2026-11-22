/* OGSN Live: every matchup with live scores, projections and NFL game clocks, plus a
   per-device watchlist (tracked players, pinned matchups) and a scoring feed.
   Website only (needs Sleeper and ESPN from the browser). Data:
     Sleeper  /league/{id}/matchups/{week}      starters and live points
     Sleeper  /schedule/nfl/regular/{season}    game status fallback
     ESPN     site.api.espn.com scoreboard       clock, quarter, score (unofficial public feed)
     D.proj   Sleeper's weekly projections scored with league settings (daily refresh)
   Saved on this device only: ogsn-track (player ids), ogsn-pin (matchup ids). */
(function () {
  "use strict";
  var LEAGUE = "1308882514567270400";
  var SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX"];
  var FAST = 30000, SLOW = 300000;
  var S = { week: null, matchups: [], games: {}, prev: {}, feed: [], updated: null, timer: null, open: {}, filterMine: false, firstLoad: true };

  function load(key) { try { return JSON.parse(localStorage.getItem(key) || "[]"); } catch (e) { return []; } }
  function save(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  var track = new Set(load("ogsn-track")), pins = new Set(load("ogsn-pin").map(String));

  function sleeper(path) {
    return fetch("/sleeper" + path, { cache: "no-store" }).then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .catch(function () { return fetch("https://api.sleeper.app/v1" + path, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }); })
      .catch(function () { return null; });
  }
  var ESPN_FIX = { WSH: "WAS", LA: "LAR" };
  function espn() {
    return fetch("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }

  var PL = {}; // pid -> {name, pos, nfl}
  function players() {
    Object.keys(D.assets || {}).forEach(function (id) {
      D.assets[id].players.forEach(function (p) { PL[p[0]] = { name: p[1], pos: p[2], nfl: p[4] || "FA", roster: +id }; });
    });
  }
  function proj(pid) { var p = ((D.proj || {}).players || {})[pid]; return p && p[0] != null && D.proj.week == S.week ? p[0] : null; }

  // NFL games: team -> {state: pre|in|post, label, frac (share of game left), opp, score}
  function games(sched, sb) {
    var g = {};
    (sched || []).filter(function (x) { return x.week == S.week; }).forEach(function (x) {
      var st = x.status === "complete" ? "post" : x.status === "in_game" ? "in" : "pre";
      g[x.home] = { state: st, label: st === "post" ? "Final" : st === "in" ? "Live" : "", frac: st === "post" ? 0 : st === "in" ? 0.5 : 1, opp: x.away };
      g[x.away] = { state: st, label: g[x.home].label, frac: g[x.home].frac, opp: x.home };
    });
    ((sb || {}).events || []).forEach(function (e) {
      var c = (e.competitions || [])[0]; if (!c) return;
      var t = e.status.type, per = e.status.period || 0, clock = e.status.displayClock || "";
      var st = t.state === "post" ? "post" : t.state === "in" ? "in" : "pre";
      var left = 0;
      if (st === "in") { var p = clock.split(":"), sec = (+p[0] || 0) * 60 + (+p[1] || 0); left = Math.max(0, (4 - per) * 900 + sec) / 3600; }
      var label = st === "post" ? "Final" : st === "in" ? (t.name === "STATUS_HALFTIME" ? "Half" : (per > 4 ? "OT " : "Q" + per + " ") + clock)
        : new Date(e.date).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" });
      var cs = c.competitors || [];
      cs.forEach(function (me) {
        var ab = ESPN_FIX[me.team.abbreviation] || me.team.abbreviation, other = cs.find(function (o) { return o !== me; }) || { team: {} };
        g[ab] = { state: st, label: label, frac: st === "post" ? 0 : st === "pre" ? 1 : left, opp: (me.homeAway === "home" ? "vs " : "@ ") + (ESPN_FIX[other.team.abbreviation] || other.team.abbreviation),
                  score: st === "pre" ? "" : me.score + "-" + other.score };
      });
    });
    return g;
  }

  function line(pid, pts) {
    var p = PL[pid] || { name: pid, pos: "", nfl: "" }, g = S.games[p.nfl] || null, pr = proj(pid);
    var est = !g ? pts : g.state === "post" ? pts : g.state === "pre" ? (pr != null ? pr : pts) : pts + (pr != null ? pr * g.frac : 0);
    return { pid: pid, name: p.name, pos: p.pos, nfl: p.nfl, pts: pts, proj: pr, est: est, game: g, roster: p.roster };
  }
  function build(rows) {
    var by = {};
    rows.forEach(function (x) {
      if (x.matchup_id == null) return;
      var st = (x.starters || []).map(function (pid, i) { return pid && pid !== "0" ? line(pid, (x.starters_points || [])[i] || 0) : null; });
      var t = { id: x.roster_id, pts: x.points || 0, starters: st, est: st.reduce(function (a, l) { return a + (l ? l.est : 0); }, 0),
                left: st.filter(function (l) { return l && l.game && l.game.state === "pre"; }).length,
                now: st.filter(function (l) { return l && l.game && l.game.state === "in"; }).length };
      (by[x.matchup_id] = by[x.matchup_id] || []).push(t);
    });
    return Object.keys(by).map(function (k) { return { id: k, teams: by[k] }; }).filter(function (m) { return m.teams.length === 2; });
  }

  function refresh() {
    return Promise.all([sleeper("/league/" + LEAGUE + "/matchups/" + S.week), fetch("https://api.sleeper.app/schedule/nfl/regular/" + D.league.season).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }), espn()])
      .then(function (r) {
        if (!r[0]) return;
        S.games = games(r[1], r[2]);
        var all = {};
        r[0].forEach(function (x) { (x.starters || []).forEach(function (pid, i) { if (pid && pid !== "0") all[pid] = [(x.starters_points || [])[i] || 0, x.roster_id]; }); });
        if (!S.firstLoad) {
          Object.keys(all).forEach(function (pid) {
            var was = S.prev[pid];
            if (was != null && Math.abs(all[pid][0] - was) >= 0.05) {
              var p = PL[pid] || { name: pid }, g = S.games[p.nfl];
              S.feed.unshift({ t: Date.now(), pid: pid, d: all[pid][0] - was, now: all[pid][0], roster: all[pid][1], clock: g ? g.label : "" });
            }
          });
          S.feed = S.feed.slice(0, 60);
        }
        Object.keys(all).forEach(function (pid) { S.prev[pid] = all[pid][0]; });
        S.firstLoad = false;
        S.matchups = build(r[0]);
        S.updated = new Date();
        render();
      });
  }

  function anyLive() { return Object.keys(S.games).some(function (k) { return S.games[k].state === "in"; }); }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { if (!document.hidden && visible()) refresh().then(schedule); else schedule(); }, anyLive() ? FAST : SLOW);
  }
  function visible() { var v = document.querySelector('[data-view="live"]'); return v && !v.hidden; }


  // ---------- app install + notifications ----------
  var A = load("ogsn-alerts"); if (Array.isArray(A)) A = {};
  A = { scores: A.scores || "big", games: A.games !== false, news: A.news !== false };
  var P = { supported: "serviceWorker" in navigator && "PushManager" in window && "Notification" in window, ready: false, key: null, sub: null, msg: "" };
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = function () { return (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true; };
  function b64(s) { var pad = "=".repeat((4 - s.length % 4) % 4), raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(raw, function (c) { return c.charCodeAt(0); }); }
  function prefs() { return { team: ME || 0, track: Array.from(track), pins: Array.from(pins), scores: A.scores, games: A.games, news: A.news }; }
  function api(method, url, body) { return fetch(url, { method: method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); }); }
  function pushInit() {
    if (!P.supported) return Promise.resolve();
    return fetch("/api/push", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }).then(function (j) {
      P.ready = !!j.ready; P.key = j.publicKey || null;
      return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) { P.sub = sub; });
    }).catch(function () {});
  }
  function syncPrefs() { if (P.sub) api("POST", "/api/push", { subscription: P.sub.toJSON(), prefs: prefs() }).catch(function () {}); }
  function alertsOn() {
    P.msg = "Asking your browser…"; render();
    return Notification.requestPermission().then(function (perm) {
      if (perm !== "granted") throw new Error("Notifications were blocked. Allow them for this site in your browser or phone settings, then try again.");
      return navigator.serviceWorker.ready;
    }).then(function (reg) { return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(P.key) }); })
      .then(function (sub) { P.sub = sub; return api("POST", "/api/push", { subscription: sub.toJSON(), prefs: prefs() }); })
      .then(function () { P.msg = "Alerts are on. A test notification is on its way."; render(); return api("POST", "/api/push?test=1", { endpoint: P.sub.endpoint }); })
      .catch(function (e) { P.msg = e.message || "Could not turn on alerts."; render(); });
  }
  function alertsOff() {
    if (!P.sub) return;
    var ep = P.sub.endpoint;
    P.sub.unsubscribe().catch(function () {}).then(function () { return api("DELETE", "/api/push", { endpoint: ep }); })
      .then(function () { P.sub = null; P.msg = "Alerts are off on this device."; render(); }).catch(function () { P.sub = null; render(); });
  }
  function appPanel() {
    var inst = standalone() ? '<p class="lv-ok">Installed. You are using the app.</p>'
      : window.__ogsnInstall ? '<button type="button" class="btn solid" id="lvinstall">Install the app</button>'
      : isIOS ? '<p>On iPhone or iPad: open this site in <b>Safari</b>, tap the <b>Share</b> button (the square with an arrow pointing up), then <b>Add to Home Screen</b>.</p>'
      : '<p>Use your browser\'s <b>Install</b> option (the icon at the right of the address bar, or the menu) to add OGSN as an app.</p>';
    var al;
    if (!P.supported) al = isIOS && !standalone() ? "<p>On iPhone, alerts work in the installed app. Add OGSN to your Home Screen first, open it from there, then turn alerts on.</p>" : "<p>This browser does not support notifications.</p>";
    else if (!P.ready) al = "<p>Alerts are being set up. Check back soon.</p>";
    else if (!P.sub) al = '<button type="button" class="btn solid" id="lvalerts">Turn on alerts</button>';
    else al = '<div class="lv-prefs"><label>Player scores <select id="lvscores"><option value="big"' + (A.scores === "big" ? " selected" : "") + '>Big plays (6+ points)</option><option value="all"' + (A.scores === "all" ? " selected" : "") + '>Every score</option><option value="off"' + (A.scores === "off" ? " selected" : "") + '>Off</option></select></label>' +
      '<label><input type="checkbox" id="lvgames"' + (A.games ? " checked" : "") + "> Lead changes and finals in my matchup and pinned matchups</label>" +
      '<label><input type="checkbox" id="lvnews"' + (A.news ? " checked" : "") + '> League news: new recap, new trades</label>' +
      '<div class="lv-btns"><button type="button" class="tkbtn" id="lvtest">Send a test</button><button type="button" class="tkbtn" id="lvoff">Turn off on this device</button></div></div>' +
      '<p class="lv-note">Score alerts cover the players you track' + (ME ? " and your team's starters" : "") + "." + (ME ? "" : ' Pick your team in <a href="#hub">My Team</a> to also get alerts for your starters and your matchup.') + "</p>";
    return '<section class="box lv-app"><div class="boxhead"><h2>App &amp; alerts</h2><span class="eyebrow">' + (P.sub ? "Alerts on" : "Alerts off") + '</span></div><div class="lv-appb"><div><h3>Get the app</h3>' + inst + '</div><div><h3>Phone alerts</h3>' + al + (P.msg ? '<p class="lv-note">' + esc(P.msg) + "</p>" : "") + "</div></div></section>";
  }

  // ---------- rendering ----------
  var f1 = function (n) { return (Math.round(n * 100) / 100).toFixed(2); };
  function gameTag(l) {
    if (!l.game) return '<span class="lv-g">' + (l.nfl === "FA" ? "FA" : "Bye") + "</span>";
    var g = l.game, cls = g.state === "in" ? "in" : g.state === "post" ? "post" : "";
    return '<span class="lv-g ' + cls + '">' + esc(g.label || "") + (g.opp ? " " + esc(g.opp) : "") + (g.score ? " · " + esc(g.score) : "") + "</span>";
  }
  function star(pid) { var on = track.has(pid); return '<button type="button" class="lv-star' + (on ? " on" : "") + '" data-track="' + esc(pid) + '" aria-pressed="' + on + '" title="' + (on ? "Stop tracking" : "Track this player") + '">' + (on ? "★" : "☆") + "</button>"; }
  function playerRow(l, slot) {
    if (!l) return '<tr class="lv-empty"><td class="lv-slot">' + slot + '</td><td colspan="3">Empty</td></tr>';
    var bump = S.feed.some(function (f) { return f.pid === l.pid && Date.now() - f.t < FAST + 5000; });
    return '<tr' + (l.game && l.game.state === "in" ? ' class="lv-on"' : "") + '><td class="lv-slot">' + slot + '</td><td class="lv-pl">' + star(l.pid) + "<b>" + esc(l.name) + "</b><small>" + esc(l.pos) + " · " + esc(l.nfl) + " " + gameTag(l) + "</small></td>" +
      '<td class="lv-pts' + (bump ? " bump" : "") + '">' + f1(l.pts) + '</td><td class="lv-pj">' + (l.proj != null ? f1(l.proj) : "–") + "</td></tr>";
  }
  function card(m) {
    var a = m.teams[0], b = m.teams[1];
    if (b.pts > a.pts || (b.pts === a.pts && b.est > a.est)) { var t = a; a = b; b = t; }
    var pinned = pins.has(m.id), mine = ME && (a.id === ME || b.id === ME), open = S.open[m.id] || pinned || mine;
    var total = Math.max(a.est + b.est, 1), share = Math.round(a.est / total * 100);
    function side(t, lead) {
      return '<div class="lv-side' + (lead ? " lead" : "") + '">' + teamBtn(t.id, "lv-tn") + '<span class="lv-rec">' + esc(rec(T[t.id])) + '</span><span class="lv-score">' + f1(t.pts) + '</span><span class="lv-est">proj ' + f1(t.est) + '</span><span class="lv-left">' +
        (t.now ? t.now + " playing" : "") + (t.now && t.left ? " · " : "") + (t.left ? t.left + " yet to play" : "") + (!t.now && !t.left ? "done" : "") + "</span></div>";
    }
    var body = open ? '<div class="lv-lineups">' + [a, b].map(function (t) {
      return '<table class="lv-tab lv-lu"><colgroup><col class="c-slot"><col><col class="c-n"><col class="c-n"></colgroup><thead><tr><th colspan="2" class="lv-th">' + esc(tn(t.id)) + '</th><th>Pts</th><th>Proj</th></tr></thead><tbody>' +
        t.starters.map(function (l, i) { return playerRow(l, SLOTS[i] || "BN"); }).join("") + "</tbody></table>";
    }).join("") + "</div>" : "";
    return '<article class="lv-card' + (pinned ? " pinned" : "") + (mine ? " mine" : "") + '" data-m="' + m.id + '"><div class="lv-head">' + side(a, a.pts > b.pts) + '<div class="lv-mid"><button type="button" class="lv-pin' + (pinned ? " on" : "") + '" data-pin="' + m.id + '" aria-pressed="' + pinned + '" title="' + (pinned ? "Unpin" : "Pin this matchup") + '">' + (pinned ? "Pinned" : "Pin") + '</button><span class="lv-wp" title="Share of projected points">' + share + "%</span></div>" + side(b, b.pts > a.pts) + "</div>" +
      '<div class="lv-bar"><i style="width:' + share + '%"></i></div>' + body +
      '<button type="button" class="lv-more" data-open="' + m.id + '">' + (open ? (pinned || mine ? "" : "Hide lineups") : "Show lineups") + "</button></article>";
  }

  function render() {
    var root = document.getElementById("livehub"); if (!root) return;
    var fi = document.getElementById("lvfind"), typed = fi ? fi.value : "", focused = fi && document.activeElement === fi;
    if (!S.matchups.length) { root.innerHTML = '<p class="cempty">No matchups for this week yet.</p>'; return; }
    var lines = [];
    S.matchups.forEach(function (m) { m.teams.forEach(function (t) { t.starters.forEach(function (l) { if (l) lines.push(l); }); }); });
    var byPid = {}; lines.forEach(function (l) { byPid[l.pid] = l; });
    var tracked = Array.from(track).map(function (pid) { return byPid[pid] || (PL[pid] ? Object.assign(line(pid, 0), { benched: true }) : null); }).filter(Boolean);
    var liveN = Object.keys(S.games).filter(function (k) { return S.games[k].state === "in"; }).length / 2;
    var order = S.matchups.slice().sort(function (x, y) {
      var px = (pins.has(x.id) ? 2 : 0) + (x.teams.some(function (t) { return t.id === ME; }) ? 1 : 0), py = (pins.has(y.id) ? 2 : 0) + (y.teams.some(function (t) { return t.id === ME; }) ? 1 : 0);
      return py - px || Math.abs(x.teams[0].est - x.teams[1].est) - Math.abs(y.teams[0].est - y.teams[1].est);
    });
    var feed = S.feed.filter(function (f) { return !S.filterMine || track.has(f.pid) || (ME && f.roster === ME); }).slice(0, 25);
    var opts = Object.keys(PL).filter(function (pid) { return !track.has(pid); }).sort(function (x, y) { return PL[x].name.localeCompare(PL[y].name); })
      .map(function (pid) { return '<option value="' + esc(PL[pid].name + " · " + PL[pid].pos + " · " + tn(PL[pid].roster)) + '" data-pid="' + pid + '"></option>'; }).join("");

    root.innerHTML = appPanel() +
      '<div class="lv-status"><span class="lv-dot' + (liveN ? " on" : "") + '"></span><b>Week ' + S.week + "</b> · " + (liveN ? liveN + " NFL game" + (liveN === 1 ? "" : "s") + " in progress" : "No NFL games in progress") +
      ' · updated ' + S.updated.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" }) + (liveN ? " · refreshes every 30 seconds" : "") +
      ' <button type="button" class="tkbtn" id="lvnow">Refresh now</button></div>' +
      '<div class="lv-top">' +
        '<section class="box lv-watch"><div class="boxhead"><h2>My watchlist</h2><span class="eyebrow">Saved on this device</span></div>' +
          '<div class="lv-add"><input class="cfind" id="lvfind" list="lvlist" placeholder="Track a player: type a name" autocomplete="off"><datalist id="lvlist">' + opts + "</datalist></div>" +
          (tracked.length ? '<table class="lv-tab lv-wl"><tbody>' + tracked.map(function (l) {
            return '<tr' + (l.game && l.game.state === "in" ? ' class="lv-on"' : "") + '><td class="lv-pl">' + star(l.pid) + "<b>" + esc(l.name) + "</b><small>" + esc(l.pos) + " · " + teamBtn(l.roster, "tlink") + (l.benched ? " · not starting" : "") + " " + gameTag(l) + '</small></td><td class="lv-pts">' + (l.benched ? "–" : f1(l.pts)) + '</td><td class="lv-pj">' + (l.proj != null ? f1(l.proj) : "–") + "</td></tr>";
          }).join("") + "</tbody></table>" : '<p class="cempty lv-hint">Tap ☆ next to any player to follow him here. Pin matchups to keep them at the top. ' + (ME ? "" : 'Pick your team in <a href="#hub">My Team</a> and your matchup stays open.') + "</p>") +
        "</section>" +
        '<section class="box lv-feed"><div class="boxhead"><h2>Scoring feed</h2><label class="eyebrow"><input type="checkbox" id="lvmine"' + (S.filterMine ? " checked" : "") + "> Mine only</label></div>" +
          (feed.length ? '<ul class="lv-fl">' + feed.map(function (f) {
            var p = PL[f.pid] || { name: f.pid };
            return '<li><span class="lv-d ' + (f.d > 0 ? "up" : "dn") + '">' + (f.d > 0 ? "+" : "") + f1(f.d) + "</span><span><b>" + esc(p.name) + "</b> " + teamBtn(f.roster, "tlink") + "<small>" + esc(f.clock || "") + " · now " + f1(f.now) + " · " +
              new Date(f.t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) + "</small></span></li>";
          }).join("") + "</ul>" : '<p class="cempty lv-hint">Scoring changes show up here as they happen while this page is open.</p>') +
        "</section></div>" +
      '<div class="lv-grid">' + order.map(card).join("") + "</div>" +
      '<p class="foot">Live points from Sleeper. Game clocks from ESPN\'s public scoreboard. "Proj" is Sleeper\'s weekly projection scored with league settings; a team\'s proj is points so far plus each unfinished player\'s projection for the time left. The % is each side\'s share of projected points, not a win probability.</p>';
    var f2 = document.getElementById("lvfind");
    if (f2 && typed) f2.value = typed;
    if (f2 && focused) f2.focus();
  }

  function onClick(e) {
    var t = e.target.closest("[data-track],[data-pin],[data-open],#lvnow,#lvinstall,#lvalerts,#lvtest,#lvoff");
    if (!t || !t.closest("#livehub")) return;
    if (t.id === "lvnow") { refresh(); return; }
    if (t.id === "lvinstall" && window.__ogsnInstall) { window.__ogsnInstall.prompt(); window.__ogsnInstall.userChoice.then(function () { window.__ogsnInstall = null; render(); }); return; }
    if (t.id === "lvalerts") { alertsOn(); return; }
    if (t.id === "lvoff") { alertsOff(); return; }
    if (t.id === "lvtest") { P.msg = "Test sent."; api("POST", "/api/push?test=1", { endpoint: P.sub.endpoint }).catch(function (er) { P.msg = er.message; render(); }); render(); return; }
    if (t.dataset.track) { var p = t.dataset.track; track.has(p) ? track.delete(p) : track.add(p); save("ogsn-track", Array.from(track)); syncPrefs(); }
    if (t.dataset.pin) { var m = t.dataset.pin; pins.has(m) ? pins.delete(m) : pins.add(m); save("ogsn-pin", Array.from(pins)); syncPrefs(); }
    if (t.dataset.open) S.open[t.dataset.open] = !S.open[t.dataset.open];
    render();
  }
  function onInput(e) {
    if (e.target.id === "lvmine") { S.filterMine = e.target.checked; render(); return; }
    if (e.target.id === "lvscores" || e.target.id === "lvgames" || e.target.id === "lvnews") {
      if (e.type !== "change") return;
      A = { scores: document.getElementById("lvscores").value, games: document.getElementById("lvgames").checked, news: document.getElementById("lvnews").checked };
      save("ogsn-alerts", A); syncPrefs(); P.msg = "Saved."; render(); return;
    }
    if (e.target.id !== "lvfind") return;
    var o = Array.from(document.querySelectorAll("#lvlist option")).find(function (x) { return x.value === e.target.value; });
    if (o) { track.add(o.dataset.pid); save("ogsn-track", Array.from(track)); syncPrefs(); e.target.value = ""; render(); var f = document.getElementById("lvfind"); if (f) f.focus(); }
  }

  function css() {
    var s = document.createElement("style");
    s.textContent = [
      ".nt .lvdot{display:inline-block;width:.45rem;height:.45rem;border-radius:50%;background:#d23a3a;margin-left:.1rem;animation:lvp 1.6s infinite}",
      "@keyframes lvp{50%{opacity:.25}}",
      "#livehub{display:grid;gap:1.5rem}",
      ".lv-status{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;font-family:var(--ui);font-size:.8rem;letter-spacing:.04em;color:var(--muted)}",
      ".lv-status b{color:var(--ink);font-family:var(--display);font-size:1.05rem;letter-spacing:0}",
      ".lv-dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--lose)}.lv-dot.on{background:#d23a3a;animation:lvp 1.6s infinite}",
      ".lv-top{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:1.25rem;align-items:start}",
      ".lv-add{padding:.6rem .8rem;border-bottom:1px solid var(--rule)}",
      ".lv-hint{padding:.8rem}",
      ".lv-tab{width:100%}.lv-tab td,.lv-tab th{font-size:.82rem;padding:.4rem .45rem}",
      ".lv-tab td.lv-pl,.lv-tab th.lv-pl{text-align:left;font-family:var(--body);white-space:normal;width:100%}",
      ".lv-tab th.lv-th{text-align:left;font-family:var(--display);font-size:.95rem;text-transform:none;letter-spacing:0;color:var(--ink);overflow:hidden;text-overflow:ellipsis}",
      ".lv-lu{table-layout:fixed}.lv-lu col.c-slot{width:2.6rem}.lv-lu col.c-n{width:3.4rem}.lv-lu td{white-space:normal}",
      ".lv-wl td.lv-pts,.lv-wl td.lv-pj{width:3.6rem}",
      ".lv-pl b{font-weight:600}.lv-pl small{display:block;font-family:var(--ui);font-size:.7rem;color:var(--muted)}",
      ".lv-slot{font-family:var(--ui);font-size:.66rem;color:var(--muted);letter-spacing:.08em;text-align:left!important}",
      ".lv-pts{font-weight:600;font-family:var(--ui)}.lv-pj{color:var(--muted);font-family:var(--ui)}",
      "tr.lv-on td{background:color-mix(in srgb,var(--win) 7%,transparent)}",
      ".lv-pts.bump{animation:lvb 2.5s}@keyframes lvb{0%{background:var(--flag);color:var(--flag-ink)}100%{background:transparent}}",
      ".lv-g{font-family:var(--ui);font-size:.66rem;color:var(--muted);margin-left:.25rem}.lv-g.in{color:#c03030;font-weight:600}.lv-g.post{color:var(--ink)}",
      ".lv-star{background:none;border:0;cursor:pointer;color:var(--muted);font-size:1rem;padding:0 .35rem 0 0;line-height:1}.lv-star.on{color:var(--flag)}",
      ".lv-fl{list-style:none;margin:0;padding:0;max-height:22rem;overflow:auto}",
      ".lv-fl li{display:grid;grid-template-columns:3.6rem minmax(0,1fr);gap:.6rem;padding:.5rem .8rem;border-bottom:1px solid var(--rule);font-size:.86rem}",
      ".lv-fl small{display:block;font-family:var(--ui);font-size:.68rem;color:var(--muted)}",
      ".lv-d{font-family:var(--ui);font-weight:600}.lv-d.up{color:var(--win)}.lv-d.dn{color:var(--oxblood)}",
      ".lv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(30rem,1fr));gap:1rem;align-items:start}",
      ".lv-card{background:var(--paper);border:1px solid var(--rule);display:grid;min-width:0}",
      ".lv-card.pinned{border-color:var(--flag);box-shadow:inset 0 3px 0 var(--flag)}.lv-card.mine{box-shadow:inset 0 3px 0 var(--win)}",
      ".lv-head{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:.6rem;padding:.8rem .9rem .5rem;align-items:start}",
      ".lv-side{display:grid;gap:.1rem;min-width:0}.lv-side:last-child{text-align:right}",
      ".lv-tn{background:none;border:0;padding:0;cursor:pointer;font-family:var(--display);font-weight:600;font-size:1.05rem;text-align:inherit;color:inherit;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".lv-rec,.lv-est,.lv-left{font-family:var(--ui);font-size:.68rem;color:var(--muted)}",
      ".lv-score{font-family:var(--display);font-size:1.9rem;line-height:1.05;color:var(--muted)}.lv-side.lead .lv-score{color:var(--ink)}",
      ".lv-mid{display:grid;gap:.4rem;justify-items:center;padding-top:.2rem}",
      ".lv-pin{font-family:var(--ui);font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;border:1px solid var(--rule);background:var(--bg);padding:.15rem .45rem;cursor:pointer}.lv-pin.on{background:var(--flag);border-color:var(--flag);color:var(--flag-ink)}",
      ".lv-wp{font-family:var(--ui);font-size:.7rem;color:var(--muted)}",
      ".lv-bar{height:.3rem;background:var(--lose);margin:0 .9rem}.lv-bar i{display:block;height:100%;background:var(--win)}",
      ".lv-lineups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;padding:.6rem .9rem 0}",
      ".lv-more{font-family:var(--ui);font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;background:none;border:0;border-top:1px solid var(--rule);margin-top:.6rem;padding:.5rem;cursor:pointer;color:var(--muted)}.lv-more:empty{display:none}",
      ".lv-appb{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1.25rem;padding:.9rem}",
      ".lv-appb h3{font-size:1.05rem;margin-bottom:.4rem}.lv-appb p{font-size:.9rem}",
      ".lv-prefs{display:grid;gap:.45rem;font-size:.88rem}.lv-prefs select{font:inherit;margin-left:.3rem}",
      ".lv-btns{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.2rem}",
      ".lv-note{font-size:.8rem!important;color:var(--muted);margin-top:.5rem}.lv-ok{color:var(--win);font-weight:600}",
      "@media (max-width:780px){.lv-appb{grid-template-columns:1fr}.lv-top{grid-template-columns:1fr}.lv-grid{grid-template-columns:1fr}.lv-lineups{grid-template-columns:1fr}}"
    ].join("\n");
    document.head.appendChild(s);
  }

  function start() {
    if (typeof D === "undefined" || !document.getElementById("livehub")) return;
    css(); players();
    S.week = (D.live && D.live.week) || (D.nflState && D.nflState.week) || D.league.week;
    if (D.live && D.live.state === "live") { var n = document.querySelector('#nav a[href="#live"]'); if (n) n.insertAdjacentHTML("beforeend", '<i class="lvdot" aria-label="games live"></i>'); }
    document.getElementById("livehub").innerHTML = '<p class="cempty">Loading live scores…</p>';
    document.addEventListener("click", onClick);
    document.addEventListener("change", onInput);
    document.addEventListener("input", onInput);
    document.addEventListener("ogsn:installable", function () { if (S.updated) render(); });
    pushInit().then(function () { if (S.updated) render(); });
    var go = function () { if (visible()) { refresh().then(schedule); } };
    addEventListener("hashchange", function () { setTimeout(go, 0); });
    go();
  }
  if (window.__ogsnReady) start(); else document.addEventListener("ogsn:ready", start);
})();
