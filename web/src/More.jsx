import { useEffect, useState } from 'react'

export default function More() {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    fetch('/health')
      .then((r) => r.json())
      .then((b) => setHealth(b.detail ?? b))
      .catch(() => setHealth({ status: 'unreachable' }))
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
      <p className="placeholder">Activity log, jobs, usage, quiet mode and the kill switch arrive with Milestones B and C.</p>
    </div>
  )
}
