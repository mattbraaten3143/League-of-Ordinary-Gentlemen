// Game-day notifier. Called about once a minute by the GitHub "Game-day ticker" workflow
// (and harmlessly by anyone else: a lock lets it run at most once every 40 seconds).
// Compares Sleeper's live points with the last run and sends each signed-up device what it asked for:
//   scores: tracked players and the device's own team's starters (every score, or 6+ point jumps)
//   games:  lead changes and final results in the device's own matchup and pinned matchups
//   news:   a new weekly recap or a new trade write-up on the site
// Returns {active}: whether NFL games are on or about to be, so the ticker knows when to stop.
import { storeReady, redis, getJSON, setJSON } from "./_lib/store.js";
import { vapid, allSubs, send } from "./_lib/push.js";

const LEAGUE = "1308882514567270400";
const BIG = 6, LEAD_QUIET_MS = 10 * 60e3;
const ESPN_FIX = { WSH: "WAS", LA: "LAR" };
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });
const get = u => fetch(u, { cache: "no-store" }).then(r => (r.ok ? r.json() : null)).catch(() => null);
const f2 = n => (Math.round(n * 100) / 100).toFixed(2);

function nflGames(sb, now) {
  const g = {}; let active = false;
  for (const e of (sb && sb.events) || []) {
    const c = (e.competitions || [])[0]; if (!c) continue;
    const t = e.status.type, st = t.state, start = Date.parse(e.date);
    if (st === "in" || (st === "pre" && start - now < 20 * 60e3 && start - now > -60 * 60e3)) active = true;
    const label = st === "post" ? "Final" : st === "in" ? (t.name === "STATUS_HALFTIME" ? "Half" : `Q${e.status.period} ${e.status.displayClock}`) : "";
    for (const me of c.competitors || []) {
      const other = (c.competitors || []).find(o => o !== me) || { team: {} };
      g[ESPN_FIX[me.team.abbreviation] || me.team.abbreviation] = { state: st, label, opp: ESPN_FIX[other.team.abbreviation] || other.team.abbreviation };
    }
  }
  return { g, active };
}

export async function GET(request) { return run(request); }
export async function POST(request) { return run(request); }

async function run(request) {
  if (!storeReady()) return json({ error: "notifications are not set up yet", active: false }, 503);
  const now = Date.now();
  const sb = await get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard");
  const { g: games, active } = nflGames(sb, now);
  if (!(await redis("SET", "push:lock", String(now), "NX", "EX", "40"))) return json({ skipped: "ran less than 40 seconds ago", active });

  const host = request.headers.get("host");
  const [stateNfl, data] = await Promise.all([get("https://api.sleeper.app/v1/state/nfl"), get(`https://${host}/data/league-data.json`)]);
  if (!stateNfl || !data) return json({ error: "Sleeper or site data unavailable", active });
  const week = +stateNfl.week;
  // Finals and lead changes need ESPN's games for this same week; without them, only scores and news go out
  const espnOk = !!(sb && sb.events && sb.events.length && (!sb.week || +sb.week.number === week));
  const rows = (await get(`https://api.sleeper.app/v1/league/${LEAGUE}/matchups/${week}`)) || [];

  const T = data.teams || {}, tn = id => (T[id] || {}).name || `Team ${id}`;
  const PL = {};
  for (const id of Object.keys(data.assets || {})) for (const p of data.assets[id].players) PL[p[0]] = { name: p[1], nfl: p[4] || "" };
  const pts = {}, owner = {}, starters = {}, byM = {};
  for (const x of rows) {
    starters[x.roster_id] = [];
    (x.starters || []).forEach((pid, i) => { if (pid && pid !== "0") { pts[pid] = (x.starters_points || [])[i] || 0; owner[pid] = x.roster_id; starters[x.roster_id].push(pid); } });
    if (x.matchup_id != null) (byM[x.matchup_id] = byM[x.matchup_id] || []).push({ id: x.roster_id, pts: x.points || 0 });
  }
  const matchups = Object.entries(byM).filter(([, t]) => t.length === 2).map(([mid, t]) => {
    const [a, b] = t, done = [a, b].every(s => starters[s.id].every(pid => { const n = games[(PL[pid] || {}).nfl]; return !n || n.state === "post"; }));
    const toPlay = [a, b].reduce((k, s) => k + starters[s.id].filter(pid => { const n = games[(PL[pid] || {}).nfl]; return n && n.state === "pre"; }).length, 0);
    return { mid, a, b, leader: a.pts === b.pts ? 0 : a.pts > b.pts ? a.id : b.id, done: espnOk && done, toPlay, started: a.pts + b.pts > 0 };
  });
  const recapWeek = Math.max(0, ...Object.keys(data.recapPages || {}).map(Number));
  const trades = (data.tradeAnalysis || []).filter(t => t.headline).map(t => ({ key: t.key, h: t.headline }));

  const prev = await getJSON("push:state");
  const next = { week, pts, leader: Object.fromEntries(matchups.map(m => [m.mid, m.leader])), leadAt: (prev && prev.week === week && prev.leadAt) || {},
                 final: Object.fromEntries(matchups.filter(m => m.done && m.started).map(m => [m.mid, true])),
                 recap: recapWeek, trades: trades.map(t => t.key), at: now };
  if (!prev || prev.week !== week) {           // first run of the week: just remember where things stand
    await setJSON("push:state", next);
    return json({ baseline: true, week, active });
  }

  // What changed since the last run
  const jumps = Object.keys(pts).map(pid => ({ pid, d: pts[pid] - (prev.pts[pid] ?? pts[pid]) })).filter(j => j.d >= 0.1);
  const flips = matchups.filter(m => espnOk && m.started && !m.done && m.leader && prev.leader[m.mid] !== m.leader && prev.leader[m.mid] !== undefined && now - (next.leadAt[m.mid] || 0) > LEAD_QUIET_MS);
  flips.forEach(m => { next.leadAt[m.mid] = now; });
  const finals = matchups.filter(m => m.done && m.started && !(prev.final || {})[m.mid]);
  const newRecap = recapWeek > (prev.recap || 0) ? (data.recapPages[String(recapWeek)] || {}) : null;
  const newTrades = trades.filter(t => !(prev.trades || []).includes(t.key));

  let sent = 0, devices = 0;
  if (jumps.length || flips.length || finals.length || newRecap || newTrades.length) {
    const subs = await allSubs(), keys = await vapid();
    for (const s of subs) {
      const p = s.prefs || {}, out = [];
      if (p.scores !== "off") {
        const mine = new Set([...(p.track || []), ...(p.team ? starters[p.team] || [] : [])]);
        const hits = jumps.filter(j => mine.has(j.pid) && (p.scores === "all" || j.d >= BIG));
        if (hits.length === 1) {
          const j = hits[0], pl = PL[j.pid] || { name: j.pid }, n = games[pl.nfl];
          out.push({ title: `${pl.name} +${f2(j.d)}`, body: `${f2(pts[j.pid])} total${n && n.label ? " · " + n.label + " vs " + n.opp : ""} · ${tn(owner[j.pid])}`, tag: "score-" + j.pid, url: "/#live" });
        } else if (hits.length > 1) {
          out.push({ title: `${hits.length} of your players scored`, body: hits.slice(0, 5).map(j => `${(PL[j.pid] || { name: j.pid }).name} +${f2(j.d)}`).join(", "), tag: "scores", url: "/#live" });
        }
      }
      if (p.games !== false) {
        const watch = m => (p.pins || []).includes(String(m.mid)) || (p.team && (m.a.id === p.team || m.b.id === p.team));
        for (const m of flips.filter(watch)) {
          const lead = m.leader === m.a.id ? m.a : m.b, trail = lead === m.a ? m.b : m.a;
          out.push({ title: `${tn(lead.id)} take the lead`, body: `${f2(lead.pts)} to ${f2(trail.pts)} over ${tn(trail.id)}${m.toPlay ? ` · ${m.toPlay} starters yet to play` : ""}`, tag: "m-" + m.mid, url: "/#live" });
        }
        for (const m of finals.filter(watch)) {
          const w = m.a.pts >= m.b.pts ? m.a : m.b, l = w === m.a ? m.b : m.a;
          out.push({ title: `Final: ${tn(w.id)} ${f2(w.pts)}, ${tn(l.id)} ${f2(l.pts)}`, body: p.team === w.id ? "You win." : p.team === l.id ? "You lose." : `${tn(w.id)} win by ${f2(w.pts - l.pts)}.`, tag: "m-" + m.mid, url: "/#live" });
        }
      }
      if (p.news !== false) {
        if (newRecap) out.push({ title: `Week ${recapWeek} recap is up`, body: ((newRecap.stories || [])[0] || {}).h || newRecap.dek || "", tag: "recap", url: `/#recap-${recapWeek}` });
        for (const t of newTrades.slice(0, 3)) out.push({ title: "New trade", body: t.h, tag: "trade-" + t.key, url: "/#trades" });
      }
      if (out.length) devices++;
      for (const msg of out.slice(0, 4)) if (await send(s, msg, keys)) sent++;
    }
  }
  await setJSON("push:state", next);
  return json({ active, week, jumps: jumps.length, flips: flips.length, finals: finals.length, recap: !!newRecap, trades: newTrades.length, devices, sent });
}
