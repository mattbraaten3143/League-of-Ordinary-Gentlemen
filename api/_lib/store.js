// Tiny Upstash Redis client over its REST API (no package needed).
// Vercel's Upstash integration sets KV_REST_API_URL / KV_REST_API_TOKEN; plain Upstash uses UPSTASH_REDIS_REST_*.
const URL_ = () => process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL || "";
const TOKEN = () => process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN || "";

export const storeReady = () => !!(URL_() && TOKEN());

export async function redis(...cmd) {
  const r = await fetch(URL_(), { method: "POST", headers: { Authorization: `Bearer ${TOKEN()}`, "content-type": "application/json" }, body: JSON.stringify(cmd) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error("redis: " + (j.error || r.status));
  return j.result;
}

export async function getJSON(key) {
  const v = await redis("GET", key);
  try { return v ? JSON.parse(v) : null; } catch (e) { return null; }
}
export const setJSON = (key, value, ...opts) => redis("SET", key, JSON.stringify(value), ...opts);
