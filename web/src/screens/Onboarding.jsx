import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import { BackButton, Badge, Button, ChatBubble, EmptyState, IconSparkle, List, ListRow, NavBar, Page, SkeletonCard, useToast } from '../ui/index.js'

/* The onboarding interview: one question at a time, Kairo asks, you answer.
   Answers save as you go (to the vault). Redo or edit anytime. */
export default function Onboarding({ onClose }) {
  const [st, setSt] = useState(null)
  const [i, setI] = useState(0)
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState('ask')   // ask | list
  const [saving, setSaving] = useState(false)
  const toast = useToast()

  const load = useCallback(async () => {
    const s = await api('/api/onboarding')
    setSt(s)
    return s
  }, [])

  useEffect(() => {
    load().then((s) => {
      const first = s.questions.findIndex((q) => !(s.answers[q.id] ?? '').trim())
      if (first === -1) setMode('list')
      else setI(first)
    })
  }, [load])

  const q = st?.questions[i]
  useEffect(() => { if (q) setDraft(st.answers[q.id] ?? '') }, [q, st])

  const save = async (next) => {
    setSaving(true)
    try {
      if ((st.answers[q.id] ?? '') !== draft.trim()) setSt(await api(`/api/onboarding/${q.id}`, { method: 'PUT', body: { value: draft } }))
      if (next >= st.questions.length) setMode('list')
      else setI(Math.max(0, next))
    } catch (e) { toast.show({ text: e.message, duration: 5000 }) } finally { setSaving(false) }
  }

  if (!st) return <Page title="Get to know you" large={false} tabbar={false}><SkeletonCard /></Page>

  if (mode === 'list') {
    return (
      <Page title="Your answers" tabbar={false} left={<BackButton label="Today" onClick={onClose} />}>
        <p className="k-hello">{st.answered} of {st.total} answered. They are in your vault, and Kairo reads them in every chat. Tap one to change it.</p>
        <List>
          {st.questions.map((x, n) => (
            <ListRow key={x.id} title={x.text} wrap chevron
              subtitle={(st.answers[x.id] ?? '').trim() || 'Not answered yet'}
              onClick={() => { setI(n); setMode('ask') }} />
          ))}
        </List>
        <div className="k-pad">
          <Button variant="danger" block onClick={async () => {
            if (!window.confirm('Start over? All answers are removed (your own notes in the vault stay).')) return
            setSt(await api('/api/onboarding/reset', { method: 'POST' })); setI(0); setMode('ask')
          }}>Redo from the start</Button>
        </div>
      </Page>
    )
  }

  const pct = (i / st.questions.length) * 100
  return (
    <div className="k-layer k-onboarding">
      <NavBar title={q.section} left={<BackButton label="Close" onClick={onClose} />}
        right={<Button variant="ghost" size="sm" onClick={() => setMode('list')}>All</Button>} />
      <div className="k-progress"><motion.div className="k-progress-bar" animate={{ scaleX: pct / 100 }} transition={spring.smooth} /></div>
      <div className="k-ob-body">
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.div key={q.id} initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -40 }} transition={spring.smooth}>
            <p className="k-eyebrow">Question {i + 1} of {st.questions.length}</p>
            <div className="k-messages" style={{ padding: 0 }}><ChatBubble role="kairo" text={q.text} /></div>
            {q.hint && <p className="k-muted k-small" style={{ margin: '8px 0 0' }}>{q.hint}</p>}
            <textarea className="k-ob-input" rows={q.kind === 'lines' ? 6 : 4} value={draft} onChange={(e) => setDraft(e.target.value)}
              placeholder={q.kind === 'lines' ? 'One per line' : 'Your answer'} autoFocus />
          </motion.div>
        </AnimatePresence>
      </div>
      <div className="k-ob-actions">
        <Button disabled={i === 0 || saving} onClick={() => save(i - 1)}>Back</Button>
        <Button variant="ghost" disabled={saving} onClick={() => { setDraft(st.answers[q.id] ?? ''); setI(i + 1 >= st.questions.length ? i : i + 1); if (i + 1 >= st.questions.length) setMode('list') }}>Skip</Button>
        <Button variant="primary" loading={saving} onClick={() => save(i + 1)}>{i + 1 === st.questions.length ? 'Finish' : 'Next'}</Button>
      </div>
    </div>
  )
}

export function OnboardingCard({ onOpen }) {
  const [st, setSt] = useState(null)
  useEffect(() => { api('/api/onboarding').then(setSt).catch(() => {}) }, [])
  if (!st) return null
  return (
    <motion.div className="k-card accent" whileTap={{ scale: 0.98 }} transition={spring.smooth} onClick={onOpen} style={{ cursor: 'pointer' }}>
      <p className="k-eyebrow">Get to know you</p>
      <p className="k-card-title">{st.answered === 0 ? 'Start the 20-minute interview' : st.done ? 'Your answers' : 'Continue the interview'}</p>
      <p className="k-card-sub">{st.answered === 0 ? 'Goals, school, apprenticeship, training, people, routines. Everything Kairo does builds on it.' : `${st.answered} of ${st.total} answered · edit anytime`}</p>
      {st.answered > 0 && !st.done && <div className="k-progress" style={{ margin: '12px 0 0' }}><div className="k-progress-bar" style={{ transform: `scaleX(${st.answered / st.total})` }} /></div>}
      {st.done && <div style={{ marginTop: 8 }}><Badge tone="ok" pop={false}>Done</Badge></div>}
    </motion.div>
  )
}
