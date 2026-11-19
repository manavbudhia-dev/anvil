// Minimal service worker so the app can be installed to the home screen.
// It caches nothing: Anvil needs a live connection to your server anyway.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
