// Notification sign-ups.
//   GET    /api/push                      -> {ready, publicKey}
//   POST   /api/push {subscription, prefs} -> saves or updates this device
//   DELETE /api/push {endpoint}            -> removes this device
//   POST   /api/push?test=1 {endpoint}     -> sends this device a test notification
// prefs: {team: roster id|0, track: [player ids], pins: [matchup ids], scores: "all"|"big"|"off", games: bool, news: bool}
import { storeReady, redis } from "./_lib/store.js";
import { vapid, subId, send } from "./_lib/push.js";

const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });
const sameSite = req => { try { return new URL(req.headers.get("origin") || "").host === req.headers.get("host"); } catch (e) { return false; } };

function cleanPrefs(p) {
  p = p || {};
  const ids = (a, n) => (Array.isArray(a) ? a : []).map(String).filter(x => /^[\w-]{1,20}$/.test(x)).slice(0, n);
  return {
    team: Number.isInteger(+p.team) && +p.team > 0 && +p.team < 100 ? +p.team : 0,
    track: ids(p.track, 60), pins: ids(p.pins, 12),
    scores: ["all", "big", "off"].includes(p.scores) ? p.scores : "big",
    games: p.games !== false, news: p.news !== false,
  };
}

export async function GET() {
  if (!storeReady()) return json({ ready: false });
  const k = await vapid();
  return json({ ready: true, publicKey: k.publicKey });
}

export async function POST(request) {
  if (!storeReady()) return json({ error: "notifications are not set up yet" }, 503);
  if (!sameSite(request)) return json({ error: "forbidden" }, 403);
  const raw = await request.text();
  if (raw.length > 8000) return json({ error: "too large" }, 413);
  let b; try { b = JSON.parse(raw); } catch (e) { return json({ error: "bad request" }, 400); }

  if (new URL(request.url).searchParams.get("test") === "1") {
    const id = await subId(String(b.endpoint || ""));
    const row = await redis("HGET", "push:subs", id);
    if (!row) return json({ error: "this device is not signed up" }, 404);
    const ok = await send({ id, ...JSON.parse(row) }, { title: "OGSN notifications are on", body: "You'll hear about the players, matchups and news you picked.", url: "/#live", tag: "test" }, await vapid());
    return json({ sent: ok });
  }

  const s = b.subscription;
  if (!s || typeof s.endpoint !== "string" || !/^https:\/\//.test(s.endpoint) || !s.keys || !s.keys.p256dh || !s.keys.auth) return json({ error: "bad subscription" }, 400);
  const id = await subId(s.endpoint);
  const count = await redis("HLEN", "push:subs");
  if (count >= 300 && !(await redis("HEXISTS", "push:subs", id))) return json({ error: "too many devices" }, 429);
  await redis("HSET", "push:subs", id, JSON.stringify({ subscription: { endpoint: s.endpoint, keys: { p256dh: s.keys.p256dh, auth: s.keys.auth } }, prefs: cleanPrefs(b.prefs), at: Date.now() }));
  return json({ ok: true, id });
}

export async function DELETE(request) {
  if (!storeReady()) return json({ ok: true });
  if (!sameSite(request)) return json({ error: "forbidden" }, 403);
  let b; try { b = JSON.parse(await request.text()); } catch (e) { return json({ error: "bad request" }, 400); }
  await redis("HDEL", "push:subs", await subId(String(b.endpoint || "")));
  return json({ ok: true });
}
