// OGSN hub data builder. Paste into a javascript_tool call on a https://sleeper.com tab.
//   const fresh = await ogsnBuild(5);        // 5 = last completed week covered by a recap
//   const diff  = ogsnDiff(oldData, fresh);   // what changed since the hub's current data
// ogsnBuild returns every field of league-data.json except `editorial`.
async function ogsnBuild(week, leagueId = "1308882514567270400") {
  const B = "https://api.sleeper.app/v1", L = `${B}/league/${leagueId}`;
  const j = async u => { const r = await fetch(u); return r.ok ? r.json() : null; };
  const [state, league, users, rosters, picks] = await Promise.all(
    [`${B}/state/nfl`, L, `${L}/users`, `${L}/rosters`, `${L}/traded_picks`].map(j));
  const players = window.__ogsnPlayers || (window.__ogsnPlayers = await j(`${B}/players/nfl`));
  const pname = id => { const p = players[id] || {}; return p.full_name || `${p.first_name || ""} ${p.last_name || ""}`.trim() || id; };
  const plab = id => `${pname(id)} (${(players[id] || {}).position || ""})`;
  const r2 = n => Math.round(n * 100) / 100;

  const umap = Object.fromEntries(users.map(u => [u.user_id, u]));
  const teams = {};
  for (const r of rosters) {
    const u = umap[r.owner_id] || {}, s = r.settings, m = r.metadata || {};
    teams[r.roster_id] = {
      id: r.roster_id,
      name: ((u.metadata || {}).team_name || u.display_name || `Team ${r.roster_id}`).trim(),
      user: u.display_name || "", w: s.wins, l: s.losses, t: s.ties,
      pf: r2(s.fpts + (s.fpts_decimal || 0) / 100),
      pa: r2((s.fpts_against || 0) + (s.fpts_against_decimal || 0) / 100),
      streak: m.streak || "", form: m.record || "", allplay: [0, 0],
    };
  }

  const weeks = [];
  for (let w = 1; w <= week; w++) {
    const m = await j(`${L}/matchups/${w}`) || [];
    const g = {};
    for (const x of m) (g[x.matchup_id] = g[x.matchup_id] || []).push({ id: x.roster_id, pts: r2(x.points) });
    weeks.push({ week: w, games: Object.values(g).filter(a => a.length === 2).map(a => a.sort((p, q) => q.pts - p.pts)) });
    for (const a of m) for (const b of m) if (a.roster_id !== b.roster_id && a.points !== b.points)
      teams[a.roster_id].allplay[a.points > b.points ? 0 : 1]++;
  }
  const standings = Object.values(teams).sort((a, b) => b.w - a.w || b.pf - a.pf).map(t => t.id);

  const games = weeks.flatMap(w => w.games.map(g => ({ week: w.week, game: g })));
  const flat = games.flatMap(x => x.game.map(t => ({ week: x.week, id: t.id, pts: t.pts })));
  const margin = x => x.game[0].pts - x.game[1].pts;
  const records = {
    high: flat.reduce((a, b) => b.pts > a.pts ? b : a),
    low: flat.reduce((a, b) => b.pts < a.pts ? b : a),
    closest: games.reduce((a, b) => margin(b) < margin(a) ? b : a),
    blowout: games.reduce((a, b) => margin(b) > margin(a) ? b : a),
  };

  const rounds = league.settings.draft_rounds || 3;
  const years = [1, 2, 3].map(i => String(+state.season + i));
  const own = {};
  for (const y of years) for (let r = 1; r <= rounds; r++) for (const id in teams) own[`${y}|${r}|${id}`] = +id;
  for (const p of picks) { const k = `${p.season}|${p.round}|${p.roster_id}`; if (k in own) own[k] = p.owner_id; }
  const capital = {};
  for (const id in teams) {
    const mine = Object.keys(own).filter(k => own[k] === +id).map(k => k.split("|"));
    capital[id] = {
      firsts: mine.filter(k => k[1] === "1").length, total: mine.length,
      byYear: Object.fromEntries(years.map(y => [y, mine.filter(k => k[0] === y).map(k => +k[1]).sort()])),
    };
  }

  const wire = [];
  for (let w = Math.max(1, week - 1); w <= week + 1; w++) {
    for (const x of await j(`${L}/transactions/${w}`) || []) {
      if (x.status !== "complete") continue;
      const adds = x.adds || {}, drops = x.drops || {};
      const item = { week: w, type: x.type, ts: x.status_updated, teams: x.roster_ids };
      if (x.type === "trade") {
        const got = {};
        for (const [pid, rid] of Object.entries(adds)) (got[rid] = got[rid] || []).push(plab(pid));
        for (const dp of x.draft_picks || []) (got[dp.owner_id] = got[dp.owner_id] || []).push(`${dp.season} Rd ${dp.round} (${teams[dp.roster_id].name})`);
        item.sides = x.roster_ids.map(id => ({ id, gets: got[id] || [] }));
      } else {
        item.adds = Object.keys(adds).map(plab);
        item.drops = Object.keys(drops).map(plab);
        const bid = (x.settings || {}).waiver_bid;
        if (bid != null) item.faab = bid;
      }
      wire.push(item);
    }
  }
  wire.sort((a, b) => b.ts - a.ts);

  return {
    league: { name: league.name, season: state.season, week, playoffWeek: league.settings.playoff_week_start || 15,
              updated: new Date().toLocaleDateString("en-CA", { timeZone: "America/Chicago" }) },
    teams, standings, weeks, records, capital, capitalYears: years, wire,
    nflState: { week: state.week, display_week: state.display_week, season_type: state.season_type },
  };
}

// Compares the hub's current data with a fresh build. Anything listed here is news.
function ogsnDiff(oldData, fresh) {
  const seen = new Set((oldData.wire || []).map(x => x.ts + "|" + x.type + "|" + x.teams.join(",")));
  const fresh_tx = fresh.wire.filter(x => !seen.has(x.ts + "|" + x.type + "|" + x.teams.join(",")));
  return {
    trades: fresh_tx.filter(x => x.type === "trade"),
    adds: fresh_tx.filter(x => x.type !== "trade" && x.adds.length),
    dropsOnly: fresh_tx.filter(x => x.type !== "trade" && !x.adds.length),
    renamed: Object.values(fresh.teams).filter(t => oldData.teams[t.id] && oldData.teams[t.id].name !== t.name)
      .map(t => ({ id: t.id, from: oldData.teams[t.id].name, to: t.name })),
    newWeek: fresh.league.week !== oldData.league.week,
  };
}
