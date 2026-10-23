// What the phone saw, sent to the PC (kairo.log, Run diagnosis): for bugs that only happen on
// the iPhone, like a voice reply nobody hears. Small, batched, never anything personal.

const queue = []
let timer = null

function flush(beacon = false) {
  clearTimeout(timer)
  timer = null
  if (!queue.length) return
  const body = JSON.stringify({ events: queue.splice(0, 20) })
  try {
    if (beacon && navigator.sendBeacon) { navigator.sendBeacon('/api/client-log', new Blob([body], { type: 'application/json' })); return }
    fetch('/api/client-log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {})
  } catch { /* logging must never break the app */ }
  if (queue.length) timer = setTimeout(flush, 500)
}

/* Send everything queued now (tests; a page that is going away uses the beacon in flush). */
export function flushClientLog() {
  while (queue.length) flush()
}

export function clientLog(event, data = {}) {
  queue.push({ t: new Date().toISOString(), event, data })
  if (queue.length > 60) queue.splice(0, queue.length - 60)
  if (!timer) timer = setTimeout(flush, 1500)
}

export function device() {
  const ua = navigator.userAgent || ''
  const ios = /OS (\d+[._]\d+)/.exec(ua)
  const safari = /Version\/(\d+(?:\.\d+)*)/.exec(ua)    // the real Safari version: the OS token above is frozen at 18_7
  return {
    ios: ios ? ios[1].replace('_', '.') : null,
    safari: safari ? safari[1] : null,
    standalone: Boolean(window.navigator.standalone || window.matchMedia?.('(display-mode: standalone)').matches),
    audioSession: 'audioSession' in navigator ? navigator.audioSession.type : 'unsupported',
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => { if (document.hidden) flush(true) })
}
