import { AnimatePresence } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, parseTs } from '../api.js'
import {
  ApprovalCard, Button, EmptyState, Expandable, IconCheck, IconShield, List, ListRow, Page, Segmented, Sheet,
  SkeletonCard, SuccessCheck, useToast,
} from '../ui/index.js'

const UNDO_MS = 4000

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

function Detail({ card, startDrag, close, onApprove, onReject }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(() => Object.fromEntries(card.editable.map((k) => [k, card.args[k]])))
  const { _display, ...shown } = card.args
  return (
    <div className="k-detail-body">
      <div className="k-detail-head" onPointerDown={startDrag}>
        <div className="k-sheet-grab"><span /></div>
        <p className="k-eyebrow">#{card.id} · {card.tier >= 4 ? 'Deletes' : 'Sends'}</p>
        <h2 className="k-card-title selectable">{card.summary}</h2>
      </div>
      <div className="k-pad" style={{ paddingTop: 16 }}>
        {card.reason && <p className="k-approval-reason">Kairo's reason: “{card.reason}”</p>}
        <p className="k-section-title" style={{ margin: '8px 0' }}>Exactly what will happen</p>
        <pre className="k-pre selectable">{JSON.stringify(shown, null, 2)}</pre>
        {editing && (
          <div style={{ marginTop: 16 }}>
            {card.editable.map((k) => (
              <label key={k} className="k-field">{k}<Field name={k} value={draft[k]} onChange={(v) => setDraft({ ...draft, [k]: v })} /></label>
            ))}
          </div>
        )}
        <p className="k-muted k-small">Expires {parseTs(card.expires_at)?.toLocaleString()}</p>
        <div className="k-approval-actions" style={{ marginTop: 16 }}>
          <Button variant="danger" onClick={() => { close(); onReject(card) }}>Reject</Button>
          <Button variant={card.tier >= 4 ? 'danger' : 'primary'} onClick={() => { close(); onApprove(card, editing ? draft : null) }}>
            {editing ? 'Approve edit' : card.tier >= 4 ? 'Delete…' : 'Approve'}
          </Button>
        </div>
        {card.editable.length > 0 && (
          <Button variant="ghost" block onClick={() => setEditing(!editing)} style={{ marginTop: 8 }}>{editing ? 'Cancel edit' : 'Edit before approving'}</Button>
        )}
        <Button variant="secondary" block onClick={close} style={{ marginTop: 8 }}>Close</Button>
      </div>
    </div>
  )
}

export default function Approvals({ onCount }) {
  const [cards, setCards] = useState(null)
  const [history, setHistory] = useState([])
  const [view, setView] = useState('open')
  const [hidden, setHidden] = useState(() => new Set())   // waiting out the Undo window
  const [opened, setOpened] = useState(null)
  const [confirm, setConfirm] = useState(null)             // tier-4 card awaiting the second yes
  const [done, setDone] = useState(null)                   // { text } for the success sheet
  const [error, setError] = useState(null)
  const toast = useToast()
  const pendingRef = useRef(new Set())

  const load = useCallback(async () => {
    try {
      const open = await api('/api/approvals?status=open')
      setCards(open)
      onCount?.(open.filter((c) => !pendingRef.current.has(c.id)).length)
      setHistory((await api('/api/approvals?status=all&limit=30')).filter((c) => c.status !== 'open'))
      setError(null)
    } catch (e) { setError(e.message) }
  }, [onCount])

  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    window.addEventListener('focus', load)
    return () => { clearInterval(t); window.removeEventListener('focus', load) }
  }, [load])

  const hide = (id, on) => {
    if (on) pendingRef.current.add(id); else pendingRef.current.delete(id)
    setHidden(new Set(pendingRef.current))
  }

  const send = async (card, kind, edited) => {
    try {
      if (kind === 'reject') await api(`/api/approvals/${card.id}/reject`, { method: 'POST' })
      else await api(`/api/approvals/${card.id}/approve`, { method: 'POST', body: { confirm: card.confirm_twice, ...(edited ? { args: edited } : {}) } })
      if (kind === 'approve') setDone({ text: card.tier >= 4 ? 'Deleted' : 'Sent' })
    } catch (e) {
      toast.show({ text: `#${card.id}: ${e.message}`, duration: 5000 })
    } finally {
      hide(card.id, false)
      load()
    }
  }

  // Approve and reject wait a few seconds with Undo: a swipe can be a mistake.
  const withUndo = (card, kind, edited) => {
    hide(card.id, true)
    onCount?.((cards ?? []).filter((c) => c.id !== card.id && !pendingRef.current.has(c.id)).length)
    toast.show({
      text: kind === 'approve' ? `Approved #${card.id}` : `Rejected #${card.id}`,
      icon: kind === 'approve' ? <IconCheck style={{ color: 'var(--ok)' }} /> : undefined,
      duration: UNDO_MS,
      action: { label: 'Undo', onAction: () => { hide(card.id, false); load() } },
      onTimeout: () => send(card, kind, edited),
    })
  }

  const approve = (card, edited) => (card.tier >= 4 || card.confirm_twice ? setConfirm({ card, edited }) : withUndo(card, 'approve', edited))
  const reject = (card) => withUndo(card, 'reject')

  const visible = (cards ?? []).filter((c) => !hidden.has(c.id))
  return (
    <Page title="Approvals" onRefresh={load}>
      <div className="k-pad" style={{ marginBottom: 16 }}>
        <Segmented id="approvals" value={view} onChange={setView}
          options={[{ value: 'open', label: `Waiting${visible.length ? ` (${visible.length})` : ''}` }, { value: 'history', label: 'History' }]} />
      </div>
      {error && <div className="k-banner">{error}</div>}

      {view === 'open' && (
        <>
          {cards === null && <><SkeletonCard /><SkeletonCard /></>}
          {cards !== null && visible.length === 0 && (
            <EmptyState icon={<IconShield />} title="Nothing waiting" text="Kairo asks here before it sends, replies or deletes anything." />
          )}
          <AnimatePresence initial={false}>
            {visible.map((c, i) => (
              <Expandable
                key={c.id}
                id={`card-${c.id}`}
                open={opened === c.id}
                onOpen={() => setOpened(c.id)}
                onClose={() => setOpened(null)}
                preview={<ApprovalCard card={c} hint={i === 0} onApprove={() => approve(c)} onReject={() => reject(c)} />}
                detail={(p) => <Detail card={c} {...p} onApprove={approve} onReject={reject} />}
              />
            ))}
          </AnimatePresence>
        </>
      )}

      {view === 'history' && (
        history.length === 0
          ? <EmptyState icon={<IconShield />} title="No decisions yet" text="Approved and rejected actions show up here." />
          : <List>
              {history.map((c) => (
                <ListRow key={c.id} title={c.summary} wrap subtitle={`#${c.id} · ${c.status}${c.decided_at ? ` · ${parseTs(c.decided_at)?.toLocaleString()}` : ''}`} />
              ))}
            </List>
      )}

      <Sheet open={!!confirm} onClose={() => setConfirm(null)} title="Are you sure?">
        {confirm && (
          <>
            <p className="k-small" style={{ textAlign: 'center' }}>This can't be undone:</p>
            <p className="k-card-title" style={{ textAlign: 'center', margin: '8px 0 20px' }}>{confirm.card.summary}</p>
            <Button variant="danger" block onClick={() => { const c = confirm; setConfirm(null); hide(c.card.id, true); send(c.card, 'approve', c.edited) }}>
              Yes, do it
            </Button>
            <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setConfirm(null)}>Cancel</Button>
          </>
        )}
      </Sheet>

      <Sheet open={!!done} onClose={() => setDone(null)}>
        {done && (
          <div className="k-success">
            <SuccessCheck />
            <p>{done.text}</p>
            <Button variant="secondary" onClick={() => setDone(null)}>Done</Button>
          </div>
        )}
      </Sheet>
    </Page>
  )
}
