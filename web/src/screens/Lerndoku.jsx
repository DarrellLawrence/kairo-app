import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Badge, Button, Card, IconMic, List, ListRow, Page, SectionTitle, SkeletonCard, useToast } from '../ui/index.js'
import { startRecording, transcribe } from '../voice.js'

const TONE = { approved: 'ok', draft: 'warn', waiting: 'accent', none: 'neutral' }
const LABEL = { approved: 'Approved', draft: 'Draft', waiting: 'Waiting for you', none: 'Not started' }
const range = (c) => `${new Date(c.start).toLocaleDateString([], { day: 'numeric', month: 'short' })} – ${new Date(c.end).toLocaleDateString([], { day: 'numeric', month: 'short' })}`

export default function Lerndoku() {
  const toast = useToast()
  const [week, setWeek] = useState(null)
  const [d, setD] = useState(null)
  const [notes, setNotes] = useState('')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [rec, setRec] = useState('idle')   // idle | recording | busy
  const recRef = useRef(null)

  const toggleRec = async () => {
    if (rec === 'idle') {
      try { recRef.current = await startRecording(); setRec('recording') } catch { toast.show({ text: 'Microphone not allowed.' }) }
      return
    }
    if (rec !== 'recording') return
    setRec('busy')
    try {
      const { text } = await transcribe(await recRef.current.stop(), 'de')
      if (text) setNotes((n) => (n.trim() ? `${n.trim()}\n${text}` : text))
    } catch (e) { toast.show({ text: e.message, duration: 6000 }) } finally { setRec('idle') }
  }

  const load = useCallback(async () => {
    const r = await api(`/api/lerndoku${week ? `?week=${week}` : ''}`)
    setD(r)
    setNotes(r.current.notes || '')
    setDraft(r.current.draft || '')
  }, [week])
  useEffect(() => { load().catch((e) => toast.show({ text: e.message })) }, [load, toast])

  const c = d?.current
  const write = async () => {
    setBusy(true)
    try {
      const r = await api(`/api/lerndoku/${c.week}/notes`, { method: 'POST', body: { notes } })
      setD((x) => ({ ...x, current: r }))
      setDraft(r.draft || '')
      toast.show({ text: 'Draft ready. Check it, then approve.' })
    } catch (e) { toast.show({ text: e.message, duration: 6000 }) } finally { setBusy(false) }
  }
  const saveDraft = async () => {
    try { await api(`/api/lerndoku/${c.week}`, { method: 'PUT', body: { draft } }); toast.show({ text: 'Saved.' }) } catch (e) { toast.show({ text: e.message }) }
  }
  const approve = async (approved) => {
    try {
      if (approved && draft !== c.draft) await api(`/api/lerndoku/${c.week}`, { method: 'PUT', body: { draft } })
      const r = await api(`/api/lerndoku/${c.week}/approve`, { method: 'POST', body: { approved } })
      setD((x) => ({ ...x, current: r }))
      setDraft(r.draft || '')
      toast.show({ text: approved ? 'Approved. It is in your vault.' : 'Back to draft.' })
      load()
    } catch (e) { toast.show({ text: e.message }) }
  }

  return (
    <Page title="Lerndoku" onRefresh={load}>
      {!c && <SkeletonCard lines={4} />}
      {c && (
        <>
          <p className="k-hello">Week {c.week.split('-W')[1]} · {range(c)} <Badge tone={TONE[c.status]} pop={false}>{LABEL[c.status]}</Badge></p>
          {c.status !== 'approved' && (
            <Card>
              <p className="k-eyebrow">1 · Tell Kairo about your week</p>
              <p className="k-card-sub">What you did, with what, what went wrong, what you learned. Tap the mic on the keyboard to talk.</p>
              <div className="k-field" style={{ marginTop: 10 }}>
                <textarea rows={6} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Diese Woche habe ich…" />
              </div>
              <Button variant="secondary" block icon={<IconMic />} loading={rec === 'busy'} onClick={toggleRec} style={{ marginBottom: 8 }}>
                {rec === 'recording' ? 'Stop recording' : 'Record a voice note (on the PC, private)'}
              </Button>
              <Button variant={c.draft ? 'secondary' : 'primary'} block loading={busy} disabled={notes.trim().length < 20} onClick={write}>
                {c.draft ? 'Write the draft again' : 'Write the German draft'}
              </Button>
              {busy && <p className="k-muted k-small" style={{ textAlign: 'center', marginTop: 8 }}>Claude is writing. About a minute.</p>}
            </Card>
          )}
          {c.draft !== null && (
            <Card>
              <p className="k-eyebrow">{c.status === 'approved' ? 'Approved entry' : '2 · Check and fix, then approve'}</p>
              <div className="k-field">
                <textarea rows={16} value={draft} readOnly={c.status === 'approved'} onChange={(e) => setDraft(e.target.value)} />
              </div>
              {c.status !== 'approved' ? (
                <div style={{ display: 'flex', gap: 8 }}>
                  <Button variant="secondary" block disabled={draft === c.draft} onClick={saveDraft}>Save</Button>
                  <Button variant="primary" block onClick={() => approve(true)}>Approve</Button>
                </div>
              ) : <Button variant="ghost" block onClick={() => approve(false)}>Reopen to change</Button>}
              <p className="k-muted k-small" style={{ marginTop: 8 }}>In your vault: {c.vault_path}</p>
            </Card>
          )}
          {d.weeks.length > 0 && (
            <>
              <SectionTitle>Earlier weeks</SectionTitle>
              <List>
                {d.weeks.map((w) => (
                  <ListRow key={w.week} title={`Week ${w.week.split('-W')[1]}, ${w.week.slice(0, 4)}`} subtitle={range(w)} chevron
                    value={<Badge tone={TONE[w.status]} pop={false}>{LABEL[w.status]}</Badge>}
                    onClick={() => { setWeek(w.week); window.scrollTo(0, 0) }} />
                ))}
              </List>
            </>
          )}
        </>
      )}
    </Page>
  )
}
