import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import {
  Badge, Button, Card, EmptyState, IconReview, List, ListRow, Page, SectionTitle, Segmented, Sheet, SkeletonCard,
  Stack, SuccessCheck, useStack, useToast,
} from '../ui/index.js'

const KINDS = [['daily', 'Evening check-in'], ['weekly', 'Weekly review'], ['monthly', 'Monthly review'],
  ['quarterly', 'Quarterly review'], ['half_year', 'Half-year review'], ['yearly', 'Yearly review']]

function period(r) {
  return r.period_start === r.period_end ? r.period_start : `${r.period_start} – ${r.period_end}`
}

function Scale({ id, value, onChange }) {
  return (
    <Segmented id={`scale-${id}`} value={value ?? 0} onChange={onChange}
      options={[1, 2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }))} />
  )
}

export function ReviewForm({ id, onDone }) {
  const [r, setR] = useState(null)
  const [answers, setAnswers] = useState({})
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const toast = useToast()
  useEffect(() => { api(`/api/reviews/${id}`).then((x) => { setR(x); setAnswers(x.answers || {}) }).catch((e) => toast.show({ text: e.message })) }, [id, toast])
  if (!r) return <Page title="Review" large={false}><SkeletonCard /><SkeletonCard /></Page>

  const submit = async () => {
    setBusy(true)
    try {
      await api(`/api/reviews/${id}/complete`, { method: 'POST', body: { answers } })
      setDone(true)
    } catch (e) { toast.show({ text: e.message, duration: 5000 }) } finally { setBusy(false) }
  }
  const closed = r.status !== 'open'
  return (
    <Page title={r.title}>
      <p className="k-hello">{period(r)} · about {r.minutes} min{closed ? ` · ${r.status}` : ''}</p>
      {r.prefill?.facts?.length > 0 && (
        <Card glass>
          <p className="k-eyebrow">What Kairo saw</p>
          {r.prefill.facts.map((f) => <p key={f} className="k-card-sub" style={{ margin: '4px 0' }}>• {f}</p>)}
        </Card>
      )}
      {r.prefill?.last_commitments?.length > 0 && (
        <Card>
          <p className="k-eyebrow">Last time you planned</p>
          {r.prefill.last_commitments.map((c) => <p key={c} className="k-card-sub" style={{ margin: '4px 0' }}>• {c}</p>)}
        </Card>
      )}
      {r.prefill?.goals && Object.keys(r.prefill.goals).length > 0 && (
        <Card>
          <p className="k-eyebrow">Your goals</p>
          {Object.values(r.prefill.goals).flatMap((g) => g.split('\n')).filter(Boolean).slice(0, 10).map((g) => <p key={g} className="k-card-sub" style={{ margin: '4px 0' }}>• {g}</p>)}
        </Card>
      )}
      <div className="k-pad">
        {r.questions.map((q) => (
          <label key={q.id} className="k-field">
            <span style={{ font: 'var(--t-headline)', color: 'var(--text)' }}>{q.text}</span>
            {q.kind === 'scale'
              ? <Scale id={q.id} value={answers[q.id]} onChange={(v) => setAnswers({ ...answers, [q.id]: v })} />
              : <textarea rows={q.kind === 'lines' ? 4 : 3} disabled={closed} value={answers[q.id] ?? ''}
                  placeholder={q.kind === 'lines' ? 'One per line' : ''} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />}
          </label>
        ))}
        {!closed && <Button variant="primary" block loading={busy} onClick={submit}>Save review</Button>}
        {r.vault_path && <p className="k-muted k-small">Saved in your vault: {r.vault_path}</p>}
      </div>
      <Sheet open={done} onClose={() => { setDone(false); onDone?.() }}>
        <div className="k-success">
          <SuccessCheck />
          <p>Saved. Commitments are on your task list.</p>
          <Button onClick={() => { setDone(false); onDone?.() }}>Done</Button>
        </div>
      </Sheet>
    </Page>
  )
}

function Root({ openId }) {
  const stack = useStack()
  const toast = useToast()
  const [view, setView] = useState('open')
  const [rows, setRows] = useState(null)
  const load = useCallback(async () => setRows(await api(`/api/reviews?status=${view === 'open' ? 'open' : 'all'}`)), [view])
  useEffect(() => { load().catch(() => setRows([])) }, [load])
  const { push, pop } = stack
  const open = useCallback((id, title) => push(title, () => <ReviewForm id={id} onDone={() => { pop(); load() }} />), [push, pop, load])
  const opened = useRef(false)   // a push link opens its review once
  useEffect(() => { if (openId && !opened.current) { opened.current = true; open(openId, 'Review') } }, [openId, open])

  const start = async (kind, title) => {
    try { const r = await api('/api/reviews/start', { method: 'POST', body: { kind } }); open(r.id, title) }
    catch (e) { toast.show({ text: e.message }) }
  }
  return (
    <Page title="Reviews" onRefresh={load}>
      <div className="k-pad" style={{ marginBottom: 16 }}>
        <Segmented id="reviews" value={view} onChange={setView} options={[{ value: 'open', label: 'To do' }, { value: 'all', label: 'History' }]} />
      </div>
      {rows === null && <SkeletonCard />}
      {rows?.length === 0 && view === 'open' && (
        <EmptyState icon={<IconReview />} title="Nothing to review now" text="Evening check-in at 21:00, weekly review on Sundays at 18:00. You get a push." />
      )}
      {rows?.length > 0 && (
        <List>
          {rows.map((r) => (
            <ListRow key={r.id} title={r.title} subtitle={period(r)} chevron
              value={<Badge tone={r.status === 'open' ? 'accent' : r.status === 'done' ? 'ok' : 'neutral'} pop={false}>{r.status === 'open' ? `${r.minutes} min` : r.status}</Badge>}
              onClick={() => open(r.id, r.title)} />
          ))}
        </List>
      )}
      <SectionTitle>Start one now</SectionTitle>
      <List>
        {KINDS.map(([k, t]) => <ListRow key={k} title={t} chevron onClick={() => start(k, t)} />)}
      </List>
    </Page>
  )
}

export default function Reviews({ openId }) {
  return <Stack root={<Root openId={openId} />} rootTitle="Reviews" />
}
