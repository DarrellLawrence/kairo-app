// v8 E: today's coaching on Today. The same log as telling Kairo in chat: one tap = done.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Button, Card, IconCheck, useToast } from '../ui/index.js'

export function CoachCard() {
  const [c, setC] = useState(null)
  const [busy, setBusy] = useState(null)
  const toast = useToast()
  const load = useCallback(() => api('/api/coach').then(setC).catch(() => setC(null)), [])
  useEffect(() => { load() }, [load])
  if (!c?.on) return null

  const log = async (key, body, said) => {
    setBusy(key)
    try {
      const r = await api('/api/coach/log', { method: 'POST', body })
      toast.show({ text: said(r) })
      await load()
    } catch (e) {
      toast.show({ text: `Not logged: ${e.message}` })
    } finally { setBusy(null) }
  }
  const s = c.today.session
  const doneToday = c.week.find((d) => d.day === new Date().toLocaleDateString('sv-SE'))?.done
  const w = c.water
  const f = c.faith

  return (
    <Card>
      <p className="k-eyebrow">Coach{c.streaks.training ? ` · ${c.streaks.training} in a row` : ''}</p>
      {s ? (
        <>
          <p className="k-card-title">{s.minutes} min {s.focus} at home</p>
          <p className="k-card-sub">{s.exercises.map((e) => `${e.name} ${e.dose}`).join(' · ')}</p>
          {doneToday ? <p className="k-small k-muted"><IconCheck width={14} height={14} /> Done today.</p> : (
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <Button variant="primary" size="sm" loading={busy === 't'}
                onClick={() => log('t', { module: 'training', what: `${s.minutes} min ${s.focus}`, value: s.minutes },
                  (r) => `Logged. ${r.streak} in a row.`)}>Done</Button>
              <Button size="sm" loading={busy === 'ts'}
                onClick={() => log('ts', { module: 'training', what: 'skipped (said so in the app)', done: false },
                  () => 'Skipped. Tomorrow then.')}>Skip today</Button>
            </div>
          )}
        </>
      ) : <p className="k-card-title">{c.today.fixed_training ? 'Training day' : 'Rest day'}</p>}
      {c.today.study.slice(0, 1).map((x) => (
        <p key={x.event} className="k-small" style={{ marginTop: 10 }}>Study: <b>{x.subject}</b>, {x.topic} ({x.minutes} min). Commute: flashcards.</p>
      ))}
      {w.goal_l ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 10 }}>
          <span className="k-small">Water {w.today_l.toFixed(1)} / {w.goal_l.toFixed(1)} l</span>
          <Button size="sm" loading={busy === 'w'}
            onClick={() => log('w', { module: 'water', what: 'a glass', value: 0.25 }, (r) => `${r.water_today_l.toFixed(2)} l today.`)}>+ glass</Button>
        </div>
      ) : null}
      {f.habit ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 10 }}>
          <span className="k-small">{f.habit}{f.after ? `, after ${f.after}` : ''}</span>
          {f.done_today ? <IconCheck width={16} height={16} /> : (
            <Button size="sm" loading={busy === 'f'} onClick={() => log('f', { module: 'faith', what: f.habit }, () => 'Done.')}>Done</Button>
          )}
        </div>
      ) : null}
    </Card>
  )
}
