import { useEffect, useState } from 'react'

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

export default function More() {
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)

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
      <h2>Last boot check</h2>
      <BootCheck check={status?.boot_check} />
      <p className="placeholder">Activity log, jobs, usage, quiet mode and the kill switch arrive with Milestones B and C.</p>
    </div>
  )
}
