// Web push: VAPID keys are created on first use and kept in Redis, so nobody has to paste a secret anywhere.
import webpush from "web-push";
import { redis, getJSON, setJSON } from "./store.js";

const SUBJECT = "https://leagueofordinarygentlemen.vercel.app";

export async function vapid() {
  let k = await getJSON("push:vapid");
  if (!k) {
    const fresh = webpush.generateVAPIDKeys();
    // NX: if two requests race, both end up using whichever pair was stored first
    await setJSON("push:vapid", fresh, "NX");
    k = await getJSON("push:vapid");
  }
  return k;
}

export async function subId(endpoint) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(buf)).slice(0, 12).map(b => b.toString(16).padStart(2, "0")).join("");
}

export async function allSubs() {
  const flat = (await redis("HGETALL", "push:subs")) || [];
  const out = [];
  for (let i = 0; i < flat.length; i += 2) { try { out.push({ id: flat[i], ...JSON.parse(flat[i + 1]) }); } catch (e) { /* skip bad row */ } }
  return out;
}

// Send one notification; drops subscriptions the browser has revoked. Returns true when delivered.
export async function send(sub, payload, keys) {
  try {
    await webpush.sendNotification(sub.subscription, JSON.stringify(payload),
      { vapidDetails: { subject: SUBJECT, publicKey: keys.publicKey, privateKey: keys.privateKey }, TTL: 3600, urgency: "high" });
    return true;
  } catch (e) {
    if (e && (e.statusCode === 404 || e.statusCode === 410)) await redis("HDEL", "push:subs", sub.id);
    else console.error("push failed", sub.id, e && e.statusCode, e && e.body);
    return false;
  }
}
