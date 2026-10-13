// Web Push handlers, imported into the generated service worker (vite.config.js).
self.addEventListener('push', (event) => {
  let data = { title: 'Kairo', body: '', url: '/' }
  try { data = { ...data, ...event.data.json() } } catch (e) { /* plain text push */ }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, tag: data.title, data: { url: data.url }, icon: '/icon-192.png', badge: '/icon-192.png',
  }))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data && event.notification.data.url) || '/'
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus() } }
    return self.clients.openWindow(url)
  }))
})
