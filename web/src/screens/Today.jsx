import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { OnboardingCard } from './Onboarding.jsx'
import { CaptureSheet } from './CaptureSheet.jsx'
import { CoachCard } from './CoachCard.jsx'
import { playLastAgain, speak, stopSpeaking, unlockAudio } from '../voice.js'
import { api, parseTs } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Badge, Button, Card, EmptyState, Expandable, Flame3D, GlowChip, IconCalendar, IconCheck, IconChevron, IconInbox, IconMail, IconMic,
  IconPlus, IconCloud, IconPin, IconSparkle, IconTrain, IconWarn, IconX, List, ListRow, Page, SectionTitle, Sheet, Skeleton, SkeletonCard, useToast,
} from '../ui/index.js'

function greeting(d = new Date()) {
  const h = d.getHours()
  return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

const time = (iso) => (iso && iso.length > 10 ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'All day')
const day = (iso) => (iso ? new Date(iso).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }) : '')
const isToday = (iso) => iso && new Date(iso).toDateString() === new Date().toDateString()

function Top3({ items }) {
  return (
    <ol className="k-top3">
      {items.map((t, i) => (
        <motion.li key={t.title} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.smooth, delay: 0.05 * i }}>
          <span className="k-top3-n">{i + 1}</span>
          <span><b>{t.title}</b><span className="k-muted k-small"> · {t.why}</span></span>
        </motion.li>
      ))}
    </ol>
  )
}

function BriefDetail({ b, startDrag, close, onDone }) {
  const today = (b.calendar || []).filter((e) => isToday(e.start) || e.all_day)
  const tomorrow = (b.calendar || []).filter((e) => !today.includes(e))
  return (
    <div className="k-detail-body">
      <div className="k-detail-head" onPointerDown={startDrag}>
        <div className="k-sheet-grab"><span /></div>
        <p className="k-eyebrow">Morning brief · {day(b.date)}{b.by === 'fallback' ? ' · without Claude' : ''}</p>
        <h2 className="k-card-title" style={{ fontSize: 24 }}>{b.headline}</h2>
      </div>
      <div>
        {b.top3?.length > 0 && <><SectionTitle>Top 3</SectionTitle><div className="k-pad"><Top3 items={b.top3} /></div></>}
        {b.open_loops?.length > 0 && (
          <>
            <SectionTitle>Still open</SectionTitle>
            <List>{b.open_loops.map((l) => (
              <ListRow key={l.id} icon={<IconWarn />} title={l.phrase} subtitle={l.offer || undefined} wrap chevron
                onClick={() => { close(); window.location.hash = `#loops/${l.id}` }} />
            ))}</List>
          </>
        )}
        <SectionTitle>Calendar</SectionTitle>
        <List>
          {b.calendar_error && <ListRow icon={<IconWarn />} title="Calendar not available" subtitle={b.calendar_error} wrap />}
          {!b.calendar_error && today.length === 0 && <ListRow icon={<IconCalendar />} title="Nothing in the calendar today" />}
          {today.map((e, i) => <ListRow key={`t${i}`} icon={<IconCalendar />} title={e.summary} subtitle={[time(e.start), e.location].filter(Boolean).join(' · ')} />)}
          {tomorrow.map((e, i) => <ListRow key={`m${i}`} title={e.summary} subtitle={`Tomorrow ${time(e.start)}`} />)}
        </List>
        {b.needs_reply?.length > 0 && (
          <>
            <SectionTitle>Needs a reply</SectionTitle>
            <List>
              {b.needs_reply.map((m) => (
                <ListRow key={m.id} icon={<IconMail />} title={m.subject} subtitle={`${m.from} · ${m.summary || ''}`} wrap
                  onClick={() => window.open(m.link, '_blank')}
                  swipe={{ right: { label: 'Done', onAction: () => onDone(m.id) } }} />
              ))}
            </List>
          </>
        )}
        {(b.overdue?.length > 0 || b.next7?.length > 0 || b.school?.length > 0) && (
          <>
            <SectionTitle>Next 7 days</SectionTitle>
            <List>
              {b.overdue?.map((t) => <ListRow key={`o${t.id}`} title={t.title} value={<Badge tone="danger" pop={false}>Overdue</Badge>} />)}
              {b.school?.map((e, i) => <ListRow key={`s${i}`} title={`${e.subject}: ${e.kind}`} subtitle={day(e.date)} />)}
              {b.next7?.map((t) => <ListRow key={`n${t.id}`} title={t.title} subtitle={day(t.due)} />)}
            </List>
          </>
        )}
        {b.captures_filed?.length > 0 && (
          <>
            <SectionTitle>Filed from your captures</SectionTitle>
            <List>{b.captures_filed.map((c) => <ListRow key={c.id} icon={<IconInbox />} title={c.title} subtitle={`as ${c.as?.replace('_', ' ')}`} />)}</List>
          </>
        )}
        <div className="k-pad" style={{ marginTop: 16 }}><Button variant="secondary" block onClick={close}>Close</Button></div>
      </div>
    </div>
  )
}

function useJobRunner(name, onDone) {
  const [running, setRunning] = useState(false)
  const timer = useRef(null)
  useEffect(() => () => clearInterval(timer.current), [])
  const run = async () => {
    setRunning(true)
    const before = (await api('/api/jobs')).jobs.find((j) => j.name === name)?.last?.started_at
    await api(`/api/jobs/${name}/run`, { method: 'POST', body: {} })
    let tries = 0
    timer.current = setInterval(async () => {
      tries += 1
      const last = (await api('/api/jobs').catch(() => ({ jobs: [] }))).jobs.find((j) => j.name === name)?.last
      if ((last && last.started_at !== before && last.status !== 'running') || tries > 90) {
        clearInterval(timer.current)
        setRunning(false)
        onDone(last)
      }
    }, 2000)
  }
  return [running, run]
}

function HereNow() {
  const [state, setState] = useState('idle')   // idle | locating | loading | done | error
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const use = () => {
    if (!navigator.geolocation) { setState('error'); setErr('This browser has no location.'); return }
    setState('locating')
    navigator.geolocation.getCurrentPosition(async (pos) => {
      setState('loading')
      try {
        setData(await api('/api/location/lookup', { method: 'POST', body: { lat: pos.coords.latitude, lon: pos.coords.longitude } }))
        setState('done')
      } catch (e) { setErr(e.message); setState('error') }
    }, (e) => { setErr(e.code === 1 ? 'Location not allowed. iPhone Settings → Privacy → Location Services → Safari Websites.' : 'Could not get your location.'); setState('error') },
    { enableHighAccuracy: false, timeout: 15000, maximumAge: 120000 })
  }
  const w = data?.weather
  const hm = (iso) => (iso ? new Date(iso.replace(/([+-]\d\d)(\d\d)$/, '$1:$2')).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')
  if (state !== 'done') {
    return (
      <div className="k-pad" style={{ marginBottom: 12 }}>
        <Button variant="secondary" size="sm" block icon={<IconPin />} loading={state === 'locating' || state === 'loading'} onClick={use}>
          Weather and trains here
        </Button>
        {state === 'error' && <p className="k-muted k-small" style={{ marginTop: 6 }}>{err}</p>}
        {state === 'idle' && <p className="k-muted k-small" style={{ marginTop: 6, textAlign: 'center' }}>Uses your location once. Not stored.</p>}
      </div>
    )
  }
  return (
    <Card glass>
      {w && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <IconCloud width={28} height={28} />
          <div>
            <p className="k-card-title" style={{ margin: 0 }}>{Math.round(w.now_c)}° · {w.now}</p>
            <p className="k-card-sub">Today {Math.round(w.min_c)}–{Math.round(w.max_c)}°, {w.today}{w.rain_pct != null ? `, rain ${w.rain_pct}%` : ''}</p>
          </div>
        </div>
      )}
      {data.weather_error && <p className="k-card-sub">{data.weather_error}</p>}
      {(data.stations || []).map((st) => (
        <div key={st.name} style={{ marginTop: 12 }}>
          <p className="k-eyebrow" style={{ display: 'flex', gap: 6, alignItems: 'center' }}><IconTrain width={14} height={14} />{st.name}{st.distance_m ? ` · ${st.distance_m} m` : ''}</p>
          {st.departures.map((dep, i) => (
            <p key={i} className="k-small" style={{ margin: '2px 0' }}>
              <b>{hm(dep.time)}</b> {dep.line} → {dep.to}{dep.platform ? ` · Pl. ${dep.platform}` : ''}{dep.delay ? <span style={{ color: 'var(--warn)' }}> +{dep.delay}'</span> : ''}
            </p>
          ))}
        </div>
      ))}
      {data.transport_error && <p className="k-card-sub">{data.transport_error}</p>}
    </Card>
  )
}

export default function Today({ go, onboarding }) {
  const toast = useToast()
  const [home, setHome] = useState(null)
  const [brief, setBrief] = useState(undefined)
  const [tri, setTri] = useState({ needs_reply: [], flagged: [] })
  const [asking, setAsking] = useState([])
  const [later, setLater] = useState([])
  const [notes, setNotes] = useState([])
  const [briefOpen, setBriefOpen] = useState(false)
  const [due, setDue] = useState([])
  const [ticked, setTicked] = useState({})
  const [captureOpen, setCaptureOpen] = useState(false)
  const [listening, setListening] = useState(null)   // null | loading | playing
  const listenAsked = window.location.hash === '#today/listen'   // the brief push: listen on the commute
  // v8 D: the brief is a short push to read and this to listen to (no call). iPhone plays sound only after a tap.
  const listenToBrief = async (b) => {
    if (listening === 'playing') { stopSpeaking(); setListening(null); return }
    unlockAudio()
    const spoken = [b.headline, ...(b.top3 || []).map((t, i) => `${i + 1}: ${t.title}.`)].join(' ')
    setListening('loading')
    try {
      const p = speak(spoken)
      setListening('playing')
      await p
    } catch (e) {
      if (e.name === 'NeedTap') { try { await playLastAgain() } catch { /* the sound check explains */ } }
    } finally { setListening(null) }
  }
  const [answering, setAnswering] = useState(null)
  const [answer, setAnswer] = useState('')

  const load = useCallback(async () => {
    const [hm, b, t, a, l, r, v] = await Promise.allSettled([
      api('/api/today'), api('/api/brief'), api('/api/triage'), api('/api/captures?status=asking'),
      api('/api/captures?status=later'), api('/api/reviews?status=open'), api('/api/captures?status=filed&limit=30'),
    ])
    setHome(hm.status === 'fulfilled' ? hm.value : { error: true })
    setBrief(b.status === 'fulfilled' ? b.value.brief : null)
    if (t.status === 'fulfilled') setTri(t.value)
    setAsking(a.status === 'fulfilled' ? a.value : [])
    setLater(l.status === 'fulfilled' ? l.value : [])
    setDue(r.status === 'fulfilled' ? r.value : [])
    const today = new Date().toLocaleDateString('sv-SE')
    setNotes(v.status === 'fulfilled'
      ? v.value.filter((c) => c.filed_as === 'voice_note' && parseTs(c.created_at).toLocaleDateString('sv-SE') === today) : [])
  }, [])
  useEffect(() => { load() }, [load])

  const [making, makeBrief] = useJobRunner('morning_brief', (last) => {
    toast.show({ text: last?.status === 'ok' ? 'Brief ready.' : `Brief failed: ${last?.error || 'unknown'}` })
    load()
  })

  const tick = async (t) => {
    const on = !ticked[t.id]
    setTicked((x) => ({ ...x, [t.id]: on }))
    try {
      await api(`/api/tasks/${t.id}`, { method: 'PATCH', body: { status: on ? 'done' : 'open' } })
      if (on) toast.show({ text: `Done: ${t.title}` })
    } catch (e) {
      setTicked((x) => ({ ...x, [t.id]: !on }))
      toast.show({ text: e.message })
    }
  }

  const ask = (prompt) => {
    try { sessionStorage.setItem('kairo-chat-prefill', prompt) } catch { /* private mode */ }
    go('chat')
  }

  const markDone = async (id) => {
    await api(`/api/triage/${encodeURIComponent(id)}/done`, { method: 'POST', body: {} })
    setTri((t) => ({ ...t, needs_reply: t.needs_reply.filter((m) => m.id !== id) }))
    setBrief((b) => b && { ...b, needs_reply: (b.needs_reply || []).filter((m) => m.id !== id) })
    toast.show({ text: 'Marked as done.' })
  }

  const sendAnswer = async () => {
    try {
      await api(`/api/captures/${answering.id}/answer`, { method: 'POST', body: { text: answer } })
      setAsking((a) => a.filter((c) => c.id !== answering.id))
      setAnswering(null)
      setAnswer('')
      toast.show({ text: 'Thanks. Kairo files it now.' })
    } catch (e) { toast.show({ text: e.message }) }
  }

  const dismiss = async (c) => {
    await api(`/api/captures/${c.id}/dismiss`, { method: 'POST', body: {} })
    setAsking((a) => a.filter((x) => x.id !== c.id))
    setLater((a) => a.filter((x) => x.id !== c.id))
  }

  const h = home && !home.error ? home : null
  const name = h?.name || ''
  const briefIsToday = brief && brief.date === new Date().toLocaleDateString('sv-SE')
  const st = h?.streak

  return (
    <Page title="Today" hero onRefresh={load}
      right={<Button variant="ghost" size="sm" icon={<IconPlus />} onClick={() => setCaptureOpen(true)} aria-label="Capture" />}>

      <motion.header className="k-hero" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={spring.smooth}>
        <h1>{greeting()}{name ? <>,<br /><em>{name}</em></> : ''}</h1>
        {h ? <p>{h.summary}</p> : <Skeleton w="80%" h={16} />}
        {h?.issues?.length > 0 && (
          <motion.button className="k-warnchip danger" onClick={() => { window.location.hash = '#more'; sessionStorage.setItem('kairo-more-open', 'system') }}
            whileTap={{ scale: 0.95 }} title={h.issues.join(' · ')}>
            <IconWarn />{h.issues.length === 1 ? h.issues[0] : `${h.issues.length} things need a look`}
          </motion.button>
        )}
        {/* Yellow: nothing is broken, there is something to do (e.g. no context yet: do the onboarding). */}
        {h?.notices?.length > 0 && (
          <motion.button className="k-warnchip" onClick={() => { window.location.hash = '#onboarding' }}
            whileTap={{ scale: 0.95 }} title={h.notices.join(' · ')}>
            <IconWarn />{h.notices[0]}
          </motion.button>
        )}
      </motion.header>

      {h?.loops?.count > 0 && (
        <motion.div className="k-stack" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.smooth, delay: 0.08 }}>
          {h.loops.count > 2 && <span className="k-stack-layer l2" />}
          {h.loops.count > 1 && <span className="k-stack-layer" />}
          <Card interactive onClick={() => { window.location.hash = `#loops/${h.loops.top[0].id}` }}>
            <div className="k-approval-head">
              <span className="k-stack-count"><b>{h.loops.count}</b><span className="k-muted k-small">open</span></span>
              {h.loops.urgent > 0 && <Badge tone="danger" pop={false}>{h.loops.urgent} urgent</Badge>}
            </div>
            <p className="k-loop-phrase" style={{ fontSize: 19, marginBottom: 6 }}>{h.loops.top[0].phrase}</p>
            {h.loops.top[0].offer && <p className="k-card-sub">{h.loops.top[0].offer}</p>}
          </Card>
        </motion.div>
      )}

      {due.map((r) => (
        <Card key={r.id} accent interactive onClick={() => { window.location.hash = `#reviews/${r.id}` }}>
          <p className="k-eyebrow ember">{r.title} · {r.minutes} min</p>
          <p className="k-card-title">Ready when you are</p>
          <p className="k-card-sub">Kairo filled in what it knows. A few questions, then it's saved to your vault.</p>
        </Card>
      ))}

      <OnboardingCard onOpen={onboarding} />

      <CoachCard />

      {!h ? <SkeletonCard lines={4} /> : (
        <Card className="k-streak">
          <motion.div className="k-flame" initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={spring.bouncy}>
            <span className="k-flame-disc" />
            <Flame3D />
          </motion.div>
          <p className="k-streak-num">{st.count}</p>
          <p className="k-streak-label">Day check-in streak</p>
          <p className="k-streak-sub">{st.done_today ? 'Today is in. Nice.' : st.count ? 'Check in tonight to keep it going.' : 'Check in tonight to start one.'}</p>
          <div className="k-week">
            {st.week.map((d, i) => (
              <motion.div key={d.date} className={`k-weekday ${d.state}`} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
                transition={{ ...spring.smooth, delay: 0.04 * i }}>
                <span>{d.day}</span>
                <span className={`k-weekdot ${d.state}`}>{d.state === 'missed' ? <IconX /> : null}</span>
              </motion.div>
            ))}
          </div>
        </Card>
      )}

      {h && (
        <>
          <SectionTitle>Your snapshot</SectionTitle>
          <Card>
            <div className="k-snapshot">
              {h.snapshot.map((x) => (
                <div key={x.label}>
                  <p className="k-num">{x.value ?? '–'}{x.value != null && x.unit ? <small>{x.unit}</small> : null}</p>
                  <span className="k-num-label">{x.label}</span>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}

      {h?.top3?.length > 0 && (
        <>
          <SectionTitle>Top 3 for today</SectionTitle>
          <div className="k-list">
            {h.top3.map((t) => {
              const on = !!ticked[t.id]
              return (
                <motion.button key={t.id} className={on ? 'k-task done' : 'k-task'} onClick={() => tick(t)} whileTap={{ scale: 0.98 }} transition={spring.snappy}>
                  <motion.span className={on ? 'k-tick on' : 'k-tick'} animate={{ scale: on ? [1, 1.25, 1] : 1 }} transition={spring.bouncy}>
                    {on && <IconCheck />}
                  </motion.span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span className="k-task-title" style={{ display: 'block' }}>{t.title}</span>
                    {t.why && <span className={t.overdue ? 'k-task-why overdue' : 'k-task-why'}>{t.why}</span>}
                  </span>
                </motion.button>
              )
            })}
          </div>
        </>
      )}

      {h?.next_up && (
        <>
          <SectionTitle>Next up</SectionTitle>
          <Card>
            <div className="k-nextup">
              <div className="k-nextup-time">
                <b>{h.next_up.time}</b>
                <span>{h.next_up.tomorrow ? 'tomorrow' : h.next_up.in_minutes < 60 ? `in ${h.next_up.in_minutes} min` : `in ${Math.round(h.next_up.in_minutes / 60)} h`}</span>
              </div>
              <div style={{ minWidth: 0 }}>
                <p className="k-card-title">{h.next_up.title}</p>
                {h.next_up.location && <p className="k-card-sub" style={{ display: 'flex', gap: 4, alignItems: 'center' }}><IconPin width={14} height={14} />{h.next_up.location}</p>}
              </div>
            </div>
          </Card>
        </>
      )}

      <div className="k-talk">
        <Button variant="primary" block breathe icon={<IconMic />} onClick={() => { window.location.hash = '#call/new' }}>Talk to Kairo</Button>
      </div>

      {h?.suggestions?.length > 0 && (
        <>
          <SectionTitle>Try asking</SectionTitle>
          <div className="k-chips">
            {h.suggestions.map((c) => <GlowChip key={c.text} glow={c.glow} onClick={() => ask(c.prompt)}>{c.text}</GlowChip>)}
          </div>
        </>
      )}

      <AnimatePresence initial={false}>
        {asking.map((c) => (
          <motion.div key={`q${c.id}`} layout initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, height: 0 }} transition={spring.smooth}
            style={{ marginTop: 24 }}>
            <Card accent>
              <p className="k-eyebrow ember">Kairo has a question</p>
              <p className="k-card-title">{c.question}</p>
              <p className="k-card-sub">“{(c.text || '').split('\n[Kairo asked')[0].slice(0, 140)}”</p>
              <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                <Button variant="primary" size="sm" onClick={() => { setAnswering(c); setAnswer('') }}>Answer</Button>
                <Button variant="ghost" size="sm" onClick={() => dismiss(c)}>Drop it</Button>
              </div>
            </Card>
          </motion.div>
        ))}
      </AnimatePresence>

      {notes.length > 0 && (
        <>
          <SectionTitle>Your voice notes</SectionTitle>
          <List>{notes.map((c) => (
            <ListRow key={c.id} icon={<IconMic />} title={c.result?.reply || c.title} subtitle={`You: “${c.title}”`} wrap
              onClick={() => go('chat')} />
          ))}</List>
        </>
      )}

      {brief === undefined ? null : brief ? (
        <>
          <SectionTitle>Morning brief{briefIsToday ? '' : ` · ${day(brief.date)}`}</SectionTitle>
          <Expandable
            id="brief"
            open={briefOpen}
            onOpen={() => setBriefOpen(true)}
            onClose={() => setBriefOpen(false)}
            preview={
              <Card interactive>
                <p className="k-card-title" style={{ font: '400 21px/1.3 var(--font-serif)' }}>{brief.headline}</p>
                {brief.top3?.length > 0 && <Top3 items={brief.top3} />}
                <p className="k-card-sub" style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 4 }}>Calendar, mail, the week<IconChevron width={12} height={12} /></p>
              </Card>
            }
            detail={(p) => <BriefDetail b={brief} onDone={markDone} {...p} />}
          />
          <div className="k-pad" style={{ marginBottom: 12 }}>
            <Button variant={listenAsked ? 'primary' : 'secondary'} size="sm" block loading={listening === 'loading'}
              onClick={() => listenToBrief(brief)}>{listening === 'playing' ? 'Stop' : 'Listen to the brief'}</Button>
          </div>
          {!briefIsToday && <div className="k-pad" style={{ marginBottom: 12 }}><Button variant="secondary" size="sm" block loading={making} onClick={makeBrief}>Make today's brief now</Button></div>}
        </>
      ) : (
        <>
          <SectionTitle>Morning brief</SectionTitle>
          <Card>
            <p className="k-card-sub">It arrives at 06:20 on weekdays and 08:20 on weekends.</p>
            <div style={{ marginTop: 12 }}><Button variant="secondary" size="sm" loading={making} onClick={makeBrief}>Make it now</Button></div>
          </Card>
        </>
      )}

      {tri.flagged.length > 0 && (
        <>
          <SectionTitle>Flagged mail</SectionTitle>
          <List>
            {tri.flagged.map((m) => (
              <ListRow key={m.id} icon={<IconWarn />} title={m.subject} wrap
                subtitle={`${m.from} · ${m.reason || 'tries to give orders'} · Kairo did nothing with it`}
                onClick={() => window.open(m.link, '_blank')} />
            ))}
          </List>
        </>
      )}

      {tri.needs_reply.length > 0 && (
        <>
          <SectionTitle>Needs a reply</SectionTitle>
          <List>
            {tri.needs_reply.slice(0, 5).map((m) => (
              <ListRow key={m.id} icon={<IconMail />} title={m.subject} subtitle={`${m.from} · ${m.summary || ''}`} wrap
                value={m.deadline ? <Badge tone="warn" pop={false}>{day(m.deadline)}</Badge> : undefined}
                onClick={() => window.open(m.link, '_blank')}
                swipe={{ right: { label: 'Done', onAction: () => markDone(m.id) } }} />
            ))}
          </List>
          <p className="k-muted k-small k-pad">Swipe right when it's handled.</p>
        </>
      )}

      {later.length > 0 && (
        <>
          <SectionTitle>Captured, waiting for a look</SectionTitle>
          <List>
            {later.map((c) => (
              <ListRow key={c.id} icon={<IconInbox />} title={c.kind === 'photo' ? 'Photo' : c.kind === 'pdf' ? 'PDF' : 'Voice memo'}
                subtitle={`${parseTs(c.created_at)?.toLocaleString()}${c.text ? ` · ${c.text}` : ''}`} wrap
                swipe={{ left: { label: 'Drop', onAction: () => dismiss(c) } }} />
            ))}
          </List>
        </>
      )}

      <HereNow />

      {home?.error && <EmptyState icon={<IconSparkle />} title="Kairo is not answering" text="Pull down to try again." />}

      <CaptureSheet open={captureOpen} onClose={() => setCaptureOpen(false)} />

      <Sheet open={!!answering} onClose={() => setAnswering(null)} title="Answer Kairo">
        {answering && <p className="k-small" style={{ margin: '0 0 12px' }}>{answering.question}</p>}
        <div className="k-field">
          <textarea rows={3} autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} />
        </div>
        <Button variant="primary" block disabled={!answer.trim()} onClick={sendAnswer}>Send</Button>
      </Sheet>
    </Page>
  )
}
