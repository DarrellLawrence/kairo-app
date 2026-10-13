// App updates. Every minute while open, and each time the app comes back to
// the front, ask for a new version. A new version is applied when the app is
// reopened (never in the middle of typing): it reloads into the new one.
import { registerSW } from 'virtual:pwa-register'

export function keepFresh() {
  let ready = false
  const apply = registerSW({
    immediate: true,
    onNeedRefresh() { ready = true },
    onRegisteredSW(_url, reg) {
      if (!reg) return
      const check = () => { if (navigator.onLine !== false) reg.update().catch(() => {}) }
      setInterval(check, 60_000)
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState !== 'visible') return
        if (ready) apply(true)
        else check()
      })
    },
  })
}
