/* OGSN service worker: makes the site installable, opens offline with the last copy,
   and shows push notifications. Pages always come from the network first, so a deploy
   shows up on the next load. */
const CACHE = "ogsn-v1";
const SHELL = ["/", "/icons/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET" || req.mode !== "navigate") return; // only page loads; everything else goes straight to the network
  e.respondWith(fetch(req).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put("/", copy));
    return res;
  }).catch(() => caches.match("/")));
});

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: "OGSN", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "OGSN", {
    body: d.body || "", tag: d.tag, renotify: !!d.tag,
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png",
    data: { url: d.url || "/#live" },
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL((e.notification.data || {}).url || "/#live", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if (new URL(c.url).origin === self.location.origin) { c.navigate(url); return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
