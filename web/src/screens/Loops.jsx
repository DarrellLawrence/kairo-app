// Open loops: everything still waiting, each with a ready answer.
// Send = your approval (the backend sends exactly this text). Later never means
// forgotten: it comes back by urgency, a bit more direct each time.
import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import { Badge, Button, EmptyState, IconCheck, IconSparkle, Page, Sheet, SkeletonCard, useToast } from '../ui/index.js'

const URGENCY = { urgent: ['danger', 'Urgent'], normal: ['accent', 'Open'], low: ['neutral', 'Low'] }
const KIND = { reply: 'Waiting for you', waiting: 'Waiting for you', promise: 'Your promise', deadline: 'Deadline', task: 'Task',
  birthday: 'Birthday', people: 'People', money: 'Money', study: 'School', pattern: 'Pattern' }
const GOT_IT = ['money', 'pattern']

const backAt = (iso) => {
  const d = new Date(iso)
  const today = new Date()
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toDateString() === today.toDateString() ? `at ${time}` : `${d.toLocaleDateString([], { weekday: 'long' })} ${time}`
}

function LoopCard({ loop, focus, onChanged }) {
  const toast = useToast()
  const [text, setText] = useState(loop.draft?.text || '')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState('')
  const [later, setLater] = useState(false)
  const ref = useRef(null)
  useEffect(() => { setText(loop.draft?.text || '') }, [loop.draft?.text])
  useEffect(() => { if (focus) ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }) }, [focus])

  const act = async (what, fn) => {
    setBusy(what)
    try { await fn() } catch (e) { toast.show({ text: e.message, duration: 5000 }) } finally { setBusy('') }
  }
  // Send: first the full address (and whether you confirmed it before), then the mail goes (fix P1.6).
  const [shown, setShown] = useState(null)
  const [sure, setSure] = useState(false)
  const preview = () => act('send', async () => {
    setSure(false)
    setShown(await api(`/api/loops/${loop.id}/send`, { method: 'POST', body: { text, preview: true } }))
  })
  const send = () => act('send', async () => {
    if (shown.confirm_twice && !sure) { setSure(true); return }
    const body = { text, to: shown.to, confirm: shown.confirm_twice }
    if (!shown.verified) body.confirm_address = shown.to
    const r = await api(`/api/loops/${loop.id}/send`, { method: 'POST', body })
    setShown(null)
    toast.show({ text: r.card.status === 'done' ? `Sent to ${shown.to}.` : `Not sent: ${r.card.result?.error || r.card.status}`, icon: <IconCheck style={{ color: 'var(--ok)' }} /> })
    onChanged()
  })
  const snooze = (body) => act('later', async () => {
    setLater(false)
    const r = await api(`/api/loops/${loop.id}/later`, { method: 'POST', body })
    toast.show({ text: r.message || `OK. Back ${backAt(r.next_at)}.`, duration: r.message?.includes('But') ? 6000 : 3500 })
    onChanged()
  })
  const close = (reason) => act('close', async () => {
    await api(`/api/loops/${loop.id}/close`, { method: 'POST', body: { reason } })
    toast.show({ text: { done: 'Done. Off the list.', not_for_me: 'Got it. Mails like this won’t show up here again.' }[reason] || 'Dropped. It won’t come back.' })
    onChanged()
  })
  const draft = () => act('draft', async () => { await api(`/api/loops/${loop.id}/draft`, { method: 'POST', body: {} }); onChanged() })

  const [tone, label] = URGENCY[loop.urgency] || URGENCY.normal
  const wantsDraft = (['reply', 'waiting'].includes(loop.kind) && loop.source === 'email') || !!loop.who_email
  return (
    <motion.div ref={ref} layout className="k-loop" initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: 80, transition: { duration: 0.22 } }} transition={spring.smooth}
      style={focus ? { boxShadow: 'var(--glass-edge), 0 0 0 1px var(--accent-line), 0 14px 40px rgba(var(--glow-ember), .18)' } : undefined}>
      <div className="k-approval-head">
        <Badge tone={tone} pop={false}>{label}</Badge>
        <span className="k-approval-who">{KIND[loop.kind] || 'Open'}{loop.who ? ` · ${loop.who}` : ''}</span>
        {loop.waiting_days > 0 && <span className="k-approval-num">{loop.waiting_days} d</span>}
      </div>
      <p className="k-loop-phrase">{loop.phrase}</p>
      {loop.draft?.note && <p className="k-approval-reason">{loop.draft.note}</p>}
      {loop.draft?.ideas?.length > 0 && (
        <ul className="k-ideas selectable">
          {loop.draft.ideas.map((i, n) => (
            <li key={n}><b>{i.idea}</b>{i.for ? ` · for ${i.for}` : ''}{i.price_chf ? ` · CHF ${i.price_chf}` : ''}
              {(i.where || i.why) && <span>{[i.where, i.why].filter(Boolean).join(' · ')}</span>}</li>
          ))}
        </ul>
      )}
      {text && !editing && <p className="k-approval-preview selectable">{text}</p>}
      {editing && (
        <div className="k-field"><textarea rows={5} value={text} autoFocus onChange={(e) => setText(e.target.value)} /></div>
      )}
      {!text && wantsDraft && (
        <Button variant="secondary" size="sm" icon={<IconSparkle />} loading={busy === 'draft'} onClick={draft} style={{ marginBottom: 12 }}>
          Write the answer for me
        </Button>
      )}
      <div className="k-approval-actions">
        {loop.can_send && text ? (
          <>
            <Button variant="primary" breathe loading={busy === 'send' && !shown} disabled={!!busy || !text.trim()} onClick={preview}>Send…</Button>
            <Button variant="secondary" disabled={!!busy} onClick={() => setEditing(!editing)}>{editing ? 'Done editing' : 'Edit'}</Button>
          </>
        ) : (
          <Button variant="primary" disabled={!!busy} onClick={() => close('done')}>{GOT_IT.includes(loop.kind) ? 'Got it' : 'Done'}</Button>
        )}
        <Button variant="secondary" disabled={!!busy} onClick={() => setLater(true)}>Later</Button>
      </div>
      {loop.can_send && text && <button type="button" className="k-loop-link" onClick={() => close('done')}>I did it another way</button>}
      <button type="button" className="k-loop-link" onClick={() => close('dropped')}>Drop it</button>
      {loop.source === 'email' && loop.who_email && (
        <button type="button" className="k-loop-link" onClick={() => close('not_for_me')}>Not a real reply · never from this sender</button>
      )}

      <Sheet open={!!shown} onClose={() => setShown(null)} title={shown?.action === 'gmail.reply' ? 'Reply' : 'Send mail'}>
        {shown && (
          <>
            <p className="k-card-sub">To</p>
            <p className="k-loop-phrase selectable" style={{ wordBreak: 'break-all' }}>{shown.to}</p>
            <p className="k-approval-reason">{shown.verified ? shown.how : 'New address: Kairo noted it, you never confirmed it. Check every letter.'}</p>
            {shown.subject && <p className="k-card-sub">Subject: {shown.subject}</p>}
            <p className="k-approval-preview selectable">{shown.text}</p>
            <Button variant="primary" block loading={busy === 'send'} disabled={!!busy} onClick={send}>
              {sure ? `Yes, really send to ${shown.to}` : shown.verified ? `Send to ${shown.to}` : `Address is right: send to ${shown.to}`}
            </Button>
            <Button variant="secondary" block style={{ marginTop: 8 }} disabled={!!busy} onClick={() => setShown(null)}>Cancel</Button>
          </>
        )}
      </Sheet>

      <Sheet open={later} onClose={() => setLater(false)} title="When should it come back?">
        <Button variant="primary" block onClick={() => snooze({ when: 'later' })}>
          Later ({loop.urgency === 'urgent' ? 'in 30 min' : loop.urgency === 'low' ? 'in 3 days' : 'tomorrow after work'})
        </Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => snooze({ when: 'in_1h' })}>In an hour</Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => snooze({ when: 'tomorrow' })}>Tomorrow morning</Button>
      </Sheet>
    </motion.div>
  )
}

export default function Loops({ focusId }) {
  const [rows, setRows] = useState(null)
  const load = useCallback(async () => { setRows(await api('/api/loops').catch(() => [])) }, [])
  useEffect(() => { load() }, [load])
  return (
    <Page title="Open loops" onRefresh={load}>
      <p className="k-hello">Kairo keeps these until they're done. Send is your OK; Later brings it back.</p>
      {rows === null && <><SkeletonCard /><SkeletonCard /></>}
      {rows?.length === 0 && <EmptyState icon={<IconCheck />} title="Nothing open" text="Nobody is waiting on you. Kairo is watching your mail, promises and deadlines." />}
      <AnimatePresence initial={false}>
        {(rows || []).map((l) => <LoopCard key={l.id} loop={l} focus={l.id === focusId} onChanged={load} />)}
      </AnimatePresence>
    </Page>
  )
}
