/* OGSN live layer.
   Before the page draws, pull the league straight from Sleeper and bring the built-in
   data up to the minute: records, points, standings, every finished week's scores, and
   this week's matchups. Then start the page. On game days the score ticker refreshes
   every minute while the tab is open.
   If Sleeper is slow or down, the page starts with the data from the last daily refresh. */
(function () {
  "use strict";
  var LEAGUE = "1308882514567270400";
  var BOOT_TIMEOUT = 4000;   // ms to wait for Sleeper before drawing the page anyway
  var POLL_MS = 60000;       // ticker refresh during live games
  // On the real site "/sleeper" is a pass-through to Sleeper's API (see vercel.json);
  // opening the page some other way falls back to calling Sleeper directly.
  var BASES = ["/sleeper", "https://api.sleeper.app/v1"];
  var base = null;
  var started = false;

  function getJSON(path) {
    var tries = base ? [base] : BASES.slice();
    function next() {
      if (!tries.length) return Promise.resolve(null);
      var b = tries.shift();
      return fetch(b + path, { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw 0; return r.json(); })
        .then(function (x) { base = b; return x; })
        .catch(next);
    }
    return next();
  }
  var r2 = function (n) { return Math.round((+n || 0) * 100) / 100; };

  function pairs(matchups) { // Sleeper matchup rows -> [[{id,pts},{id,pts}], ...], higher score first
    var g = {};
    (matchups || []).forEach(function (x) {
      if (x.matchup_id == null) return;
      (g[x.matchup_id] = g[x.matchup_id] || []).push({ id: x.roster_id, pts: r2(x.points) });
    });
    return Object.keys(g).map(function (k) { return g[k]; })
      .filter(function (a) { return a.length === 2; })
      .map(function (a) { return a.sort(function (p, q) { return q.pts - p.pts; }); });
  }

  function chicagoDay() { // 0 = Sunday, in the league's home time zone
    try {
      var s = new Date().toLocaleDateString("en-US", { weekday: "short", timeZone: "America/Chicago" });
      return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(s);
    } catch (e) { return new Date().getDay(); }
  }
  function today() {
    try { return new Date().toLocaleDateString("en-CA", { timeZone: "America/Chicago" }); }
    catch (e) { return new Date().toISOString().slice(0, 10); }
  }

  function liveWeek(week, games) {
    var scored = games.some(function (g) { return g[0].pts > 0 || g[1].pts > 0; });
    var day = chicagoDay(), quiet = day === 2 || day === 3; // Tue/Wed: no NFL games
    var state = !scored ? "upcoming" : quiet ? "final" : "live";
    return { week: week, games: games, state: state, label: state === "live" ? "LIVE" : state === "final" ? "FINAL" : "UPCOMING" };
  }

  // Bring the built-in data up to date. Only touches fields that come straight from Sleeper.
  function patch(D, s) {
    var nfl = s.state, rosters = s.rosters, users = s.users;
    if (!nfl || !rosters || !users) return false;
    if (String(nfl.season) !== String(D.league.season) || nfl.season_type !== "regular") return false;
    var cur = +nfl.week;

    var umap = {};
    users.forEach(function (u) { umap[u.user_id] = u; });
    rosters.forEach(function (r) {
      var t = D.teams[r.roster_id]; if (!t) return;
      var st = r.settings || {}, m = r.metadata || {}, u = umap[r.owner_id] || {};
      t.name = (((u.metadata || {}).team_name) || u.display_name || t.name).trim();
      t.user = u.display_name || t.user;
      t.w = st.wins; t.l = st.losses; t.t = st.ties;
      t.pf = r2((st.fpts || 0) + (st.fpts_decimal || 0) / 100);
      t.pa = r2((st.fpts_against || 0) + (st.fpts_against_decimal || 0) / 100);
      if (st.ppts != null) t.maxpf = r2(st.ppts + (st.ppts_decimal || 0) / 100);
      t.streak = m.streak || ""; t.form = m.record || "";
    });

    // Finished weeks the built-in data doesn't have yet
    var have = {}; D.weeks.forEach(function (w) { have[w.week] = 1; });
    Object.keys(s.done).forEach(function (wk) {
      var games = pairs(s.done[wk]);
      if (!have[wk] && games.length) D.weeks.push({ week: +wk, games: games });
    });
    D.weeks.sort(function (a, b) { return a.week - b.week; });

    // All-play record and league records, from every finished week
    Object.keys(D.teams).forEach(function (id) { D.teams[id].allplay = [0, 0]; });
    var flat = [], games = [];
    D.weeks.forEach(function (w) {
      var all = [];
      w.games.forEach(function (g) { all.push(g[0], g[1]); games.push({ week: w.week, game: g }); });
      all.forEach(function (a) {
        flat.push({ week: w.week, id: a.id, pts: a.pts });
        all.forEach(function (b) {
          if (a.id !== b.id && a.pts !== b.pts && D.teams[a.id]) D.teams[a.id].allplay[a.pts > b.pts ? 0 : 1]++;
        });
      });
    });
    if (flat.length) {
      var margin = function (x) { return x.game[0].pts - x.game[1].pts; };
      D.records = {
        high: flat.reduce(function (a, b) { return b.pts > a.pts ? b : a; }),
        low: flat.reduce(function (a, b) { return b.pts < a.pts ? b : a; }),
        closest: games.reduce(function (a, b) { return margin(b) < margin(a) ? b : a; }),
        blowout: games.reduce(function (a, b) { return margin(b) > margin(a) ? b : a; })
      };
    }

    // Same order as refresh.py: wins, then points for
    D.standings = Object.keys(D.teams).map(function (id) { return D.teams[id]; })
      .sort(function (a, b) { return b.w - a.w || b.pf - a.pf; })
      .map(function (t) { return t.id; });

    // Playoff odds simulate only the weeks still to play
    if (D.schedule) D.schedule = D.schedule.filter(function (w) { return w.week >= cur; });

    var lastPlayoffWeek = (D.league.playoffWeek || 15) + 2;
    if (s.current && cur <= lastPlayoffWeek) {
      var lg = pairs(s.current);
      if (lg.length) D.live = liveWeek(cur, lg);
    }
    D.league.updated = today();
    return true;
  }

  function fetchAll() {
    var L = "/league/" + LEAGUE;
    return getJSON("/state/nfl").then(function (state) {
      if (!state) return null;
      return Promise.all([getJSON(L + "/rosters"), getJSON(L + "/users")]).then(function (ru) {
        var data = JSON.parse(document.getElementById("league-data").textContent);
        var have = {}; data.weeks.forEach(function (w) { have[w.week] = 1; });
        var cur = +state.week, need = [];
        for (var w = 1; w < cur; w++) if (!have[w]) need.push(w);
        return Promise.all(need.map(function (w) { return getJSON(L + "/matchups/" + w); }).concat([getJSON(L + "/matchups/" + cur)]))
          .then(function (ms) {
            var done = {};
            need.forEach(function (w, i) { if (ms[i]) done[w] = ms[i]; });
            return { data: data, state: state, rosters: ru[0], users: ru[1], done: done, current: ms[ms.length - 1] };
          });
      });
    });
  }

  function startApp() {
    if (started) return; started = true;
    var app = document.getElementById("ogsn-app");
    var s = document.createElement("script");
    s.textContent = app.textContent;
    document.body.appendChild(s);
    window.__ogsnReady = true;
    document.dispatchEvent(new Event("ogsn:ready"));
  }

  function poll(week) {
    setInterval(function () {
      if (document.hidden) return;
      getJSON("/league/" + LEAGUE + "/matchups/" + week).then(function (m) {
        var g = pairs(m); if (!g.length) return;
        try {
          // D, sel and strip are the page's own globals
          var lw = liveWeek(week, g);
          D.live.games = lw.games; D.live.state = lw.state; D.live.label = lw.label;
          if (String(sel.value) === String(week) && !document.querySelector("#strip:hover")) strip(sel.value);
        } catch (e) { /* page not ready; try again next minute */ }
      });
    }, POLL_MS);
  }

  /* "Claude's take" in the trade calculator. Inside Claude the page uses the viewer's own
     Claude sign-in (window.claude); on this site the same button goes through /api/take,
     which holds the prompt and the API key. Only the calculator's facts are sent. */
  if (!window.claude) window.claude = {
    use: function () {
      return fetch("/api/take", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { return j && j.ok ? take : false; })
        .catch(function () { return false; });
    }
  };
  function take(prompt, opts) {
    opts = opts || {};
    var at = prompt.lastIndexOf("FACTS:"), sides;
    try { sides = JSON.parse(prompt.slice(at + 6)); } catch (e) { return Promise.reject({ code: "error" }); }
    return fetch("/api/take", { method: "POST", headers: { "content-type": "application/json" },
                                body: JSON.stringify({ sides: sides }), signal: opts.signal })
      .then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; })
          .then(function (j) { throw { code: "error", text: r.status === 429 ? j.error : undefined }; });
        var rd = r.body.getReader(), dec = new TextDecoder(), text = "";
        function pump() {
          return rd.read().then(function (x) {
            if (x.done) return text;
            text += dec.decode(x.value, { stream: true });
            if (opts.onText) opts.onText({ text: text });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") throw { code: "cancelled" };
        throw (e && e.code) ? e : { code: "error" };
      });
  }

  var timer = setTimeout(startApp, BOOT_TIMEOUT); // Sleeper too slow: draw with built-in data
  fetchAll().then(function (s) {
    if (started) return;
    if (!s) { clearTimeout(timer); startApp(); return; } // Sleeper unreachable: draw now with built-in data
    if (patch(s.data, s)) {
      document.getElementById("league-data").textContent = JSON.stringify(s.data);
      clearTimeout(timer);
      startApp();
      // Keep the ticker fresh during games, and on game days before kickoff (Thu, Sat, Sun, Mon)
      var gameDay = [0, 1, 4, 6].indexOf(chicagoDay()) >= 0;
      if (s.data.live && (s.data.live.state === "live" || (s.data.live.state === "upcoming" && gameDay))) poll(s.data.live.week);
      return;
    }
    clearTimeout(timer); startApp();
  }).catch(function () { clearTimeout(timer); startApp(); });
})();
