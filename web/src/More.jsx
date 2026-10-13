import { useEffect, useState } from 'react'
import { api, parseTs } from './api.js'

function BootCheck({ check }) {
  if (!check) return <p className="placeholder">No boot check yet. It runs 3 minutes after the PC starts.</p>
  if (check.error) return <p className="placeholder">{check.error}</p>
  const when = new Date(check.time).toLocaleString()
  return (
    <div className="bootcheck">
      <p className={check.all_ok ? 'ok' : 'bad'}>
        {check.all_ok ? 'Everything came back by itself.' : 'Something did not come back.'} <span className="muted">({check.trigger}, {when})</span>
      </p>
      <ul>
        {check.checks.map((c) => (
          <li key={c.name} className={c.ok ? 'ok' : 'bad'}>
            <span aria-hidden="true">{c.ok ? '✓' : '✗'}</span> {c.name}: <span className="muted">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function urlBase64ToUint8Array(base64) {
  const pad = '='.repeat((4 - (base64.length % 4)) % 4)
  const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'))
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)))
}

function KillSwitch({ onChange }) {
  const [paused, setPaused] = useState(null)
  useEffect(() => { api('/api/kill-switch').then((r) => setPaused(r.paused)).catch(() => {}) }, [])
  if (paused === null) return null
  const toggle = async () => {
    if (!paused && !window.confirm('Pause Kairo? It stops what it is doing and does nothing until you resume.')) return
    const r = await api('/api/kill-switch', { method: 'POST', body: { paused: !paused } })
    setPaused(r.paused)
    onChange?.()
  }
  return (
    <div className={paused ? 'kill on' : 'kill'}>
      <p>{paused ? 'Kairo is PAUSED. Nothing runs.' : 'Kairo is running.'}</p>
      <button onClick={toggle}>{paused ? 'Resume' : 'Pause all (kill switch)'}</button>
    </div>
  )
}

function Notifications() {
  const [state, setState] = useState(() => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission))
  const [msg, setMsg] = useState(null)
  const enable = async () => {
    try {
      const perm = await Notification.requestPermission()
      setState(perm)
      if (perm !== 'granted') return
      const reg = await navigator.serviceWorker.ready
      const { publicKey } = await api('/api/push/key')
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(publicKey) })
      const json = sub.toJSON()
      await api('/api/push/subscribe', { method: 'POST', body: { endpoint: json.endpoint, keys: json.keys, device: navigator.userAgent.includes('iPhone') ? 'iphone' : 'browser' } })
      await api('/api/push/test', { method: 'POST' })
      setMsg('Done. A test notification is on its way.')
    } catch (e) { setMsg(`Could not enable: ${e.message}`) }
  }
  if (state === 'unsupported') return <p className="muted small">Notifications: add Kairo to the Home Screen first (Share → Add to Home Screen), then open it from there.</p>
  return (
    <div>
      <button onClick={enable}>{state === 'granted' ? 'Re-connect notifications' : 'Turn on notifications'}</button>
      {msg && <p className="muted small">{msg}</p>}
    </div>
  )
}

function Google() {
  const [st, setSt] = useState(null)
  useEffect(() => { api('/api/setup/google/status').then(setSt).catch(() => {}) }, [])
  if (!st) return null
  const ok = st.read && st.write && !st.signed_out
  const ends = [st.read_may_end, st.write_may_end].filter(Boolean)
  const end = ends.length ? new Date(Math.min(...ends) * 1000) : null
  return (
    <div>
      <p className={ok ? 'small' : 'small bad'}>
        Google: {st.signed_out ? 'Google signed Kairo out. Sign in again below.'
          : ok ? `connected (${st.read})` : 'not connected yet. On the PC: Start menu -> Kairo -> Kairo Logins'}
      </p>
      {ok && end && <p className="small muted">Google Testing mode: this sign-in may end {end.toLocaleDateString()}. You get a reminder the day before.</p>}
      {(st.read || st.write) && (st.phone_client
        ? <p><a className="action" href="/setup/google/start?kind=read">Sign in to Google again</a></p>
        : <p className="small muted">To sign in again from the iPhone, run Kairo Logins on the PC once (Google step).</p>)}
    </div>
  )
}

function Activity({ refresh }) {
  const [rows, setRows] = useState([])
  useEffect(() => { api('/api/activity?limit=100').then(setRows).catch(() => {}) }, [refresh])
  return (
    <ul className="activity">
      {rows.map((r) => (
        <li key={r.id}>
          <span className="muted small">{parseTs(r.ts)?.toLocaleString()}</span><br />
          <b>{r.action}</b> <span className="muted">({r.actor}{r.tier !== null ? `, tier ${r.tier}` : ''})</span>: {r.outcome}
        </li>
      ))}
    </ul>
  )
}

export default function More() {
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    fetch('/health')
      .then((r) => r.json())
      .then((b) => setHealth(b.detail ?? b))
      .catch(() => setHealth({ status: 'unreachable' }))
    fetch('/api/status')
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])

  return (
    <div className="more">
      <h2>System</h2>
      {!health && <p>Checking…</p>}
      {health && (
        <dl>
          <dt>API</dt><dd>{health.status}</dd>
          <dt>Database</dt><dd>{health.db ?? '–'}</dd>
          <dt>Brain</dt><dd>{health.brain ?? '–'}{health.brain_busy ? ' (busy)' : ''}</dd>
        </dl>
      )}
      <h2>Kill switch</h2>
      <KillSwitch onChange={() => setTick((t) => t + 1)} />
      <h2>Notifications</h2>
      <Notifications />
      <h2>Accounts</h2>
      <Google />
      <h2>Last boot check</h2>
      <BootCheck check={status?.boot_check} />
      <h2>Activity</h2>
      <Activity refresh={tick} />
    </div>
  )
}
