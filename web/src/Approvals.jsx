import { useCallback, useEffect, useState } from 'react'
import { api, parseTs } from './api.js'

const TIER = { 3: 'Sends', 4: 'Deletes' }

function Field({ name, value, onChange }) {
  if (name === 'body') return <textarea rows={6} value={value} onChange={(e) => onChange(e.target.value)} />
  if (name === 'response') {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="accepted">Accept</option><option value="tentative">Maybe</option><option value="declined">Decline</option>
      </select>
    )
  }
  if (name === 'to' || name === 'cc') {
    return <input value={(value || []).join(', ')} onChange={(e) => onChange(e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} />
  }
  return <input value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
}

function Card({ card, onDone }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(() => Object.fromEntries(card.editable.map((k) => [k, card.args[k]])))
  const { _display, ...shown } = card.args

  const act = async (kind) => {
    setError(null)
    if (kind === 'approve' && card.confirm_twice &&
        !window.confirm(`Really? This can't be undone:\n\n${card.summary}`)) return
    setBusy(true)
    try {
      if (kind === 'reject') await api(`/api/approvals/${card.id}/reject`, { method: 'POST' })
      else await api(`/api/approvals/${card.id}/approve`, { method: 'POST', body: { confirm: card.confirm_twice, ...(editing ? { args: draft } : {}) } })
      onDone()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <article className={`card tier${card.tier}`}>
      <header><span className="num">#{card.id}</span> <span className="badge">{TIER[card.tier] ?? `Tier ${card.tier}`}</span></header>
      <p className="summary">{card.summary}</p>
      {card.reason && <p className="muted small">Kairo's reason: {card.reason}</p>}
      <details><summary>Exactly what will happen</summary><pre>{JSON.stringify(shown, null, 2)}</pre></details>
      {editing && (
        <div className="edit">
          {card.editable.map((k) => (
            <label key={k}>{k}<Field name={k} value={draft[k]} onChange={(v) => setDraft({ ...draft, [k]: v })} /></label>
          ))}
        </div>
      )}
      {error && <p className="bad small">{error}</p>}
      <div className="actions">
        <button className="approve" disabled={busy} onClick={() => act('approve')}>{editing ? 'Approve edit' : 'Approve'}</button>
        <button disabled={busy} onClick={() => act('reject')}>Reject</button>
        {card.editable.length > 0 && <button disabled={busy} onClick={() => setEditing(!editing)}>{editing ? 'Cancel edit' : 'Edit'}</button>}
      </div>
      <p className="muted small">Expires {parseTs(card.expires_at)?.toLocaleString()}</p>
    </article>
  )
}

export default function Approvals({ onCount }) {
  const [cards, setCards] = useState(null)
  const [history, setHistory] = useState([])
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const open = await api('/api/approvals?status=open')
      setCards(open)
      onCount?.(open.length)
      setHistory((await api('/api/approvals?status=all&limit=15')).filter((c) => c.status !== 'open'))
      setError(null)
    } catch (e) { setError(e.message) }
  }, [onCount])

  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => { clearInterval(t); window.removeEventListener('focus', onFocus) }
  }, [load])

  return (
    <div className="approvals">
      {error && <div className="banner">{error}</div>}
      {cards === null && <p className="placeholder">Loading…</p>}
      {cards?.length === 0 && <p className="placeholder">Nothing waiting. Kairo asks here before it sends, replies or deletes anything.</p>}
      {cards?.map((c) => <Card key={c.id} card={c} onDone={load} />)}
      {history.length > 0 && (
        <details className="history"><summary>Recent decisions</summary>
          <ul>{history.map((c) => <li key={c.id}><b>#{c.id}</b> {c.status}: <span className="muted">{c.summary}</span></li>)}</ul>
        </details>
      )}
    </div>
  )
}
