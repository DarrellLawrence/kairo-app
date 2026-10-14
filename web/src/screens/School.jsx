import { AnimatePresence, motion, useMotionValue, useTransform, animate } from 'motion/react'
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Badge, Button, Card, EmptyState, IconCalendar, IconPlus, IconSparkle, List, ListRow, Page, SectionTitle, Segmented, Sheet,
  SkeletonCard, useToast,
} from '../ui/index.js'

const dayLabel = (iso) => new Date(iso).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })
const daysTo = (iso) => Math.round((new Date(new Date(iso).toDateString()) - new Date(new Date().toDateString())) / 864e5)
const gradeTone = (x) => (x >= 5 ? 'ok' : x >= 4 ? 'neutral' : 'danger')

function Field({ label, children }) { return <label className="k-field">{label}{children}</label> }

function SubjectInput({ value, onChange, subjects }) {
  return (
    <>
      <input list="k-subjects" value={value} onChange={(e) => onChange(e.target.value)} placeholder="Maths, German, ..." />
      <datalist id="k-subjects">{subjects.map((s) => <option key={s} value={s} />)}</datalist>
    </>
  )
}

function Tests({ d, onAdd, onDelete }) {
  return (
    <>
      {d.events.length === 0 && <EmptyState icon={<IconCalendar />} title="No test dates" text="Add them here, or just capture “Maths test on Friday”." />}
      {d.events.length > 0 && (
        <List>
          {d.events.map((e) => {
            const n = daysTo(e.date)
            return (
              <ListRow key={e.id} title={`${e.subject}: ${e.kind}`} subtitle={`${dayLabel(e.date)}${e.notes ? ` · ${e.notes}` : ''}`} wrap
                value={<Badge tone={n <= 3 ? 'warn' : 'neutral'} pop={false}>{n === 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} d`}</Badge>}
                swipe={{ left: { label: 'Delete', onAction: () => onDelete(e.id) } }} />
            )
          })}
        </List>
      )}
      <div className="k-pad" style={{ marginTop: 12 }}><Button variant="secondary" block icon={<IconPlus />} onClick={onAdd}>Add a date</Button></div>
    </>
  )
}

function Grades({ d, onAdd, onDelete }) {
  const [calc, setCalc] = useState(null)   // subject
  const [target, setTarget] = useState('5')
  const [weight, setWeight] = useState('1')
  const [need, setNeed] = useState(null)
  useEffect(() => {
    if (!calc) return
    api(`/api/school/needed?subject=${encodeURIComponent(calc)}&target=${target || 4}&weight=${weight || 1}`).then((r) => setNeed(r.needed)).catch(() => setNeed(null))
  }, [calc, target, weight])
  const subjects = Object.keys(d.averages).sort()
  return (
    <>
      {subjects.length === 0 && <EmptyState icon={<IconSparkle />} title="No grades yet" text="Add your grades and Kairo keeps the averages." />}
      {subjects.map((s) => {
        const a = d.averages[s]
        return (
          <div key={s}>
            <SectionTitle>{s}</SectionTitle>
            <Card interactive onClick={() => { setCalc(s); setNeed(null) }} style={{ marginBottom: 8 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                <span className="k-stat"><b>{a.average.toFixed(2)}</b></span>
                <Badge tone={gradeTone(a.report)} pop={false}>report {a.report.toFixed(1)}</Badge>
                <span className="k-muted k-small">{a.count} grade{a.count === 1 ? '' : 's'} · tap: what do I need?</span>
              </div>
            </Card>
            <List>
              {d.grades.filter((g) => g.subject === s).map((g) => (
                <ListRow key={g.id} title={g.title || 'Grade'} subtitle={`${dayLabel(g.date)}${g.weight !== 1 ? ` · counts ${g.weight}×` : ''}`}
                  value={<Badge tone={gradeTone(g.grade)} pop={false}>{g.grade}</Badge>}
                  swipe={{ left: { label: 'Delete', onAction: () => onDelete(g.id) } }} />
              ))}
            </List>
          </div>
        )
      })}
      <div className="k-pad" style={{ marginTop: 12 }}><Button variant="secondary" block icon={<IconPlus />} onClick={onAdd}>Add a grade</Button></div>
      <Sheet open={!!calc} onClose={() => setCalc(null)} title={`${calc}: what do I need?`}>
        <div style={{ display: 'flex', gap: 8 }}>
          <Field label="Target average"><input inputMode="decimal" value={target} onChange={(e) => setTarget(e.target.value)} /></Field>
          <Field label="Next test counts"><input inputMode="decimal" value={weight} onChange={(e) => setWeight(e.target.value)} /></Field>
        </div>
        <p className="k-card-title" style={{ textAlign: 'center', marginTop: 8 }}>
          {need === null ? '…' : need > 6 ? `Not reachable with one test (needs ${need})` : need < 1 ? 'Already safe, even with a 1' : `You need at least ${need.toFixed(2)}`}
        </p>
      </Sheet>
    </>
  )
}

function FlipCard({ card, onAnswer }) {
  const [flipped, setFlipped] = useState(false)
  const x = useMotionValue(0)
  const rotate = useTransform(x, [-200, 200], [-12, 12])
  const knewOpacity = useTransform(x, [20, 120], [0, 1])
  const notOpacity = useTransform(x, [-120, -20], [1, 0])
  useEffect(() => { setFlipped(false); x.set(0) }, [card.id, x])
  return (
    <motion.div className="k-flash" style={{ x, rotate }} drag={flipped ? 'x' : false} dragElastic={0.6} dragConstraints={{ left: 0, right: 0 }}
      onDragEnd={(_, i) => {
        if (i.offset.x > 110 || i.velocity.x > 700) onAnswer(true)
        else if (i.offset.x < -110 || i.velocity.x < -700) onAnswer(false)
        else animate(x, 0, spring.bouncy)
      }}
      onClick={() => setFlipped(true)} whileTap={{ scale: 0.98 }}>
      <motion.span className="k-flash-tag ok" style={{ opacity: knewOpacity }}>Knew it</motion.span>
      <motion.span className="k-flash-tag no" style={{ opacity: notOpacity }}>Not yet</motion.span>
      <p className="k-eyebrow">{card.subject}{card.topic ? ` · ${card.topic}` : ''} · box {card.box}</p>
      <p className="k-flash-front">{card.front}</p>
      <AnimatePresence>
        {flipped && <motion.p className="k-flash-back" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={spring.smooth}>{card.back}</motion.p>}
      </AnimatePresence>
      {!flipped && <p className="k-muted k-small" style={{ marginTop: 'auto' }}>Tap to show the answer</p>}
    </motion.div>
  )
}

function Study({ subject, onDone }) {
  const [cards, setCards] = useState(null)
  const [i, setI] = useState(0)
  const [right, setRight] = useState(0)
  useEffect(() => { api(`/api/school/cards/due${subject ? `?subject=${encodeURIComponent(subject)}` : ''}`).then(setCards) }, [subject])
  if (!cards) return <SkeletonCard />
  const card = cards[i]
  const answer = async (knew) => {
    await api(`/api/school/cards/${card.id}/review`, { method: 'POST', body: { knew } })
    if (knew) setRight((r) => r + 1)
    if (!knew) setCards((c) => [...c, card])   // again at the end of this round
    setI((n) => n + 1)
  }
  if (!card) {
    return (
      <EmptyState icon={<IconSparkle />} title="Done for today" text={`${right} right. The next cards come back when they are due.`}
        action={<Button variant="primary" onClick={onDone}>Back</Button>} />
    )
  }
  return (
    <div className="k-pad">
      <p className="k-muted k-small" style={{ textAlign: 'center' }}>{i + 1} / {cards.length}</p>
      <AnimatePresence mode="popLayout">
        <motion.div key={`${card.id}-${i}`} initial={{ opacity: 0, scale: 0.94 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.94 }} transition={spring.smooth}>
          <FlipCard card={card} onAnswer={answer} />
        </motion.div>
      </AnimatePresence>
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <Button variant="secondary" block onClick={() => answer(false)}>Not yet</Button>
        <Button variant="primary" block onClick={() => answer(true)}>Knew it</Button>
      </div>
      <p className="k-muted k-small" style={{ textAlign: 'center', marginTop: 8 }}>Or swipe the card: right = knew it, left = not yet.</p>
    </div>
  )
}

function Learn({ d, onStudy, onMake }) {
  const subjects = d.subjects
  const totalDue = Object.values(d.cards).reduce((n, c) => n + c.due, 0)
  return (
    <>
      {totalDue > 0 && <div className="k-pad" style={{ marginBottom: 12 }}><Button variant="primary" block onClick={() => onStudy(null)}>Study all due cards ({totalDue})</Button></div>}
      {subjects.length === 0 && <EmptyState icon={<IconSparkle />} title="Nothing to learn yet" text="Make flashcards or a study guide from your notes." />}
      {subjects.length > 0 && (
        <List>
          {subjects.map((s) => (
            <ListRow key={s} title={s} subtitle={`${d.cards[s]?.total ?? 0} cards`} chevron={d.cards[s]?.due > 0}
              value={d.cards[s]?.due ? <Badge tone="accent" pop={false}>{d.cards[s].due} due</Badge> : undefined}
              onClick={d.cards[s]?.due ? () => onStudy(s) : undefined} />
          ))}
        </List>
      )}
      <div className="k-pad" style={{ marginTop: 12, display: 'flex', gap: 8 }}>
        <Button variant="secondary" block onClick={() => onMake('cards')}>Make flashcards</Button>
        <Button variant="secondary" block onClick={() => onMake('guide')}>Study guide</Button>
      </div>
      <p className="k-muted k-small k-pad">Kairo uses what you paste plus your vault notes in 40 School/&lt;subject&gt;/. Guides are saved there too.</p>
    </>
  )
}

export default function School() {
  const toast = useToast()
  const [view, setView] = useState('tests')
  const [d, setD] = useState(null)
  const [sheet, setSheet] = useState(null)   // 'event' | 'grade' | 'cards' | 'guide'
  const [f, setF] = useState({})
  const [busy, setBusy] = useState(false)
  const [studying, setStudying] = useState(undefined)
  const [guide, setGuide] = useState(null)

  const load = useCallback(() => api('/api/school').then(setD).catch((e) => toast.show({ text: e.message })), [toast])
  useEffect(() => { load() }, [load])
  const open = (kind) => { setSheet(kind); setF({ subject: d?.subjects[0] || '', kind: 'test', weight: '1', n: '15' }) }
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }))

  const submit = async () => {
    setBusy(true)
    try {
      if (sheet === 'event') await api('/api/school/events', { method: 'POST', body: { subject: f.subject, kind: f.kind, date: new Date(`${f.date}T08:00:00`).toISOString(), notes: f.notes || null } })
      if (sheet === 'grade') await api('/api/school/grades', { method: 'POST', body: { subject: f.subject, grade: Number(f.grade), weight: Number(f.weight || 1), title: f.title || null } })
      if (sheet === 'cards') {
        const cards = await api('/api/school/cards/generate', { method: 'POST', body: { subject: f.subject, topic: f.topic, material: f.material || '', n: Number(f.n || 15) } })
        toast.show({ text: `${cards.length} flashcards ready.` })
      }
      if (sheet === 'guide') setGuide(await api('/api/school/guide', { method: 'POST', body: { subject: f.subject, topic: f.topic, material: f.material || '' } }))
      setSheet(null)
      load()
    } catch (e) { toast.show({ text: e.message, duration: 6000 }) } finally { setBusy(false) }
  }
  const del = async (path) => { await api(path, { method: 'DELETE' }); load() }

  if (studying !== undefined) {
    return (
      <Page title={studying || 'All subjects'} left={<Button variant="ghost" size="sm" onClick={() => { setStudying(undefined); load() }}>Close</Button>}>
        <Study subject={studying} onDone={() => { setStudying(undefined); load() }} />
      </Page>
    )
  }

  const valid = sheet === 'event' ? f.subject && f.date : sheet === 'grade' ? f.subject && Number(f.grade) >= 1 && Number(f.grade) <= 6 : f.subject && f.topic
  return (
    <Page title="School" onRefresh={load}>
      <div className="k-pad" style={{ marginBottom: 12 }}>
        <Segmented id="schoolview" value={view} onChange={setView}
          options={[{ value: 'tests', label: 'Dates' }, { value: 'grades', label: 'Grades' }, { value: 'learn', label: 'Learn' }]} />
      </div>
      {!d && <SkeletonCard />}
      {d && view === 'tests' && <Tests d={d} onAdd={() => open('event')} onDelete={(id) => del(`/api/school/events/${id}`)} />}
      {d && view === 'grades' && <Grades d={d} onAdd={() => open('grade')} onDelete={(id) => del(`/api/school/grades/${id}`)} />}
      {d && view === 'learn' && <Learn d={d} onStudy={setStudying} onMake={open} />}

      <Sheet open={!!sheet} onClose={() => !busy && setSheet(null)}
        title={{ event: 'Add a date', grade: 'Add a grade', cards: 'Make flashcards', guide: 'Study guide' }[sheet] || ''}>
        <Field label="Subject"><SubjectInput value={f.subject || ''} onChange={(v) => setF((x) => ({ ...x, subject: v }))} subjects={d?.subjects || []} /></Field>
        {sheet === 'event' && (
          <>
            <div style={{ display: 'flex', gap: 8 }}>
              <Field label="Date"><input type="date" value={f.date || ''} onChange={set('date')} /></Field>
              <Field label="What"><select value={f.kind} onChange={set('kind')}><option value="test">Test</option><option value="deadline">Deadline</option><option value="lesson">Lesson</option></select></Field>
            </div>
            <Field label="Notes (topics)"><input value={f.notes || ''} onChange={set('notes')} /></Field>
          </>
        )}
        {sheet === 'grade' && (
          <>
            <div style={{ display: 'flex', gap: 8 }}>
              <Field label="Grade (1-6)"><input inputMode="decimal" value={f.grade || ''} onChange={set('grade')} /></Field>
              <Field label="Counts"><input inputMode="decimal" value={f.weight || ''} onChange={set('weight')} /></Field>
            </div>
            <Field label="Name (optional)"><input value={f.title || ''} onChange={set('title')} placeholder="Test 2" /></Field>
          </>
        )}
        {(sheet === 'cards' || sheet === 'guide') && (
          <>
            <Field label="Topic"><input value={f.topic || ''} onChange={set('topic')} placeholder="Geometry" /></Field>
            <Field label="Material (paste notes, optional)"><textarea rows={5} value={f.material || ''} onChange={set('material')} /></Field>
            {sheet === 'cards' && <Field label="How many cards"><input inputMode="numeric" value={f.n || ''} onChange={set('n')} /></Field>}
          </>
        )}
        <Button variant="primary" block loading={busy} disabled={!valid} onClick={submit}>{sheet === 'cards' || sheet === 'guide' ? 'Make it' : 'Add'}</Button>
        {busy && (sheet === 'cards' || sheet === 'guide') && <p className="k-muted k-small" style={{ textAlign: 'center', marginTop: 8 }}>Claude is writing. About a minute.</p>}
      </Sheet>

      <Sheet open={!!guide} onClose={() => setGuide(null)} title={guide?.title || ''}>
        <div className="k-guide">{guide?.markdown}</div>
        <p className="k-muted k-small">Saved in your vault: {guide?.vault_path}</p>
      </Sheet>
    </Page>
  )
}
