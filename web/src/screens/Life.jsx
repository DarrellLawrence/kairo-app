import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import {
  Badge, Button, Card, EmptyState, IconPlus, IconSparkle, List, ListRow, Page, SectionTitle, Segmented, Sheet,
  SkeletonCard, useToast,
} from '../ui/index.js'

const chf = (n) => `CHF ${Number(n || 0).toFixed(2)}`
const dayLabel = (iso) => new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short' })

function Lists() {
  const toast = useToast()
  const [items, setItems] = useState(null)
  const [chores, setChores] = useState([])
  const [text, setText] = useState('')
  const [newChore, setNewChore] = useState(null)
  const load = useCallback(async () => {
    const [i, c] = await Promise.all([api('/api/shopping'), api('/api/chores')])
    setItems(i)
    setChores(c)
  }, [])
  useEffect(() => { load() }, [load])
  const add = async () => {
    const parts = text.split(/[,\n]/).map((x) => x.trim()).filter(Boolean)
    if (!parts.length) return
    await api('/api/shopping', { method: 'POST', body: { items: parts } })
    setText('')
    load()
  }
  const toggle = async (id) => { await api(`/api/shopping/${id}/toggle`, { method: 'POST', body: {} }); load() }
  const done = async (c) => { await api(`/api/chores/${c.id}/done`, { method: 'POST', body: {} }); toast.show({ text: `${c.title}: done.` }); load() }
  if (!items) return <SkeletonCard />
  const open = items.filter((i) => !i.done)
  const bought = items.filter((i) => i.done)
  return (
    <>
      <SectionTitle>Shopping ({open.length})</SectionTitle>
      <div className="k-pad" style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        <div className="k-field" style={{ flex: 1, margin: 0 }}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Milk, bread, …" onKeyDown={(e) => { if (e.key === 'Enter') add() }} />
        </div>
        <Button variant="primary" icon={<IconPlus />} onClick={add} aria-label="Add" />
      </div>
      <List>
        {open.map((i) => <ListRow key={i.id} title={i.text} subtitle={i.source !== 'app' ? `from ${i.source}` : undefined} onClick={() => toggle(i.id)}
          swipe={{ right: { label: 'Got it', onAction: () => toggle(i.id) } }} />)}
        {bought.map((i) => <ListRow key={i.id} title={<span style={{ textDecoration: 'line-through', opacity: 0.5 }}>{i.text}</span>} onClick={() => toggle(i.id)} />)}
      </List>
      {bought.length > 0 && <div className="k-pad" style={{ marginTop: 8 }}><Button variant="ghost" size="sm" onClick={async () => { await api('/api/shopping/clear-done', { method: 'POST', body: {} }); load() }}>Clear bought items</Button></div>}
      <p className="k-muted k-small k-pad">Tell Kairo “put eggs on the list” or capture “Einkaufen: Eier, Butter”.</p>

      <SectionTitle>Chores</SectionTitle>
      <List>
        {chores.map((c) => (
          <ListRow key={c.id} title={c.title} subtitle={`Every ${c.every_days} days · ${c.due ? 'due now' : `next ${dayLabel(c.next)}`}`}
            value={c.due ? <Badge tone="warn" pop={false}>Due</Badge> : <Badge tone="ok" pop={false}>OK</Badge>}
            onClick={() => done(c)} swipe={{ left: { label: 'Delete', onAction: async () => { await api(`/api/chores/${c.id}`, { method: 'DELETE' }); load() } } }} />
        ))}
        <ListRow icon={<IconPlus />} title="Add a chore" onClick={() => setNewChore({ title: '', every_days: '7' })} />
      </List>
      <p className="k-muted k-small k-pad">Tap a chore when it's done. Due chores show in the morning brief.</p>
      <Sheet open={!!newChore} onClose={() => setNewChore(null)} title="New chore">
        <label className="k-field">What<input value={newChore?.title || ''} onChange={(e) => setNewChore((x) => ({ ...x, title: e.target.value }))} autoFocus /></label>
        <label className="k-field">Every how many days<input inputMode="numeric" value={newChore?.every_days || ''} onChange={(e) => setNewChore((x) => ({ ...x, every_days: e.target.value }))} /></label>
        <Button variant="primary" block disabled={!newChore?.title?.trim()} onClick={async () => { await api('/api/chores', { method: 'POST', body: { title: newChore.title, every_days: Number(newChore.every_days) } }); setNewChore(null); load() }}>Add</Button>
      </Sheet>
    </>
  )
}

function People() {
  const [d, setD] = useState(null)
  const [edit, setEdit] = useState(null)
  const load = useCallback(() => api('/api/people').then(setD), [])
  useEffect(() => { load() }, [load])
  const save = async () => {
    await api('/api/people', { method: 'POST', body: { name: edit.name, relation: edit.relation || null, birthday: edit.birthday || null,
      contact_every_days: edit.contact_every_days ? Number(edit.contact_every_days) : null, notes: edit.notes || null,
      ...(edit.emailTouched ? { email: edit.email || null } : {}) } })
    setEdit(null)
    load()
  }
  if (!d) return <SkeletonCard />
  return (
    <>
      {(d.birthdays.length > 0 || d.reach_out.length > 0) && (
        <Card accent>
          {d.birthdays.map((b) => <p key={b.name} className="k-card-sub">🎂 {b.name}: {b.in_days === 0 ? 'today' : b.in_days === 1 ? 'tomorrow' : `in ${b.in_days} days`}</p>)}
          {d.reach_out.map((r) => <p key={r.name} className="k-card-sub">👋 Time to get in touch with {r.name}</p>)}
        </Card>
      )}
      {d.people.length === 0 && <EmptyState icon={<IconSparkle />} title="No people yet" text="Add the people who matter, or tell Kairo about them in chat." />}
      <List>
        {d.people.map((p) => (
          <ListRow key={p.id} title={p.name} wrap chevron
            subtitle={[p.relation, p.birthday && `birthday ${dayLabel(p.birthday)}`, p.last_contact && `last talked ${dayLabel(p.last_contact)}`].filter(Boolean).join(' · ')}
            value={p.reach_out ? <Badge tone="warn" pop={false}>Reach out</Badge> : undefined}
            onClick={() => setEdit({ ...p, birthday: p.birthday || '', contact_every_days: p.contact_every_days || '' })} />
        ))}
        <ListRow icon={<IconPlus />} title="Add a person" onClick={() => setEdit({ name: '', relation: '', birthday: '', contact_every_days: '', notes: '' })} />
      </List>
      <Sheet open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? edit.name : 'New person'}>
        {!edit?.id && <label className="k-field">Name<input value={edit?.name || ''} onChange={(e) => setEdit((x) => ({ ...x, name: e.target.value }))} autoFocus /></label>}
        <div style={{ display: 'flex', gap: 8 }}>
          <label className="k-field" style={{ flex: 1 }}>Relation<input value={edit?.relation || ''} onChange={(e) => setEdit((x) => ({ ...x, relation: e.target.value }))} placeholder="friend, family…" /></label>
          <label className="k-field" style={{ flex: 1 }}>Birthday<input type="date" value={edit?.birthday || ''} onChange={(e) => setEdit((x) => ({ ...x, birthday: e.target.value }))} /></label>
        </div>
        <label className="k-field">Email (Kairo only sends to addresses you typed or confirmed)
          <input type="email" inputMode="email" autoCapitalize="off" value={edit?.email || ''}
            onChange={(e) => setEdit((x) => ({ ...x, email: e.target.value, emailTouched: true }))} placeholder="name@example.com" />
        </label>
        {edit?.email && !edit?.email_verified && !edit?.emailTouched && (
          <Button variant="secondary" size="sm" style={{ marginBottom: 12 }}
            onClick={() => setEdit((x) => ({ ...x, emailTouched: true, email_verified: true }))}>
            Kairo noted this address. It's right ({edit.email})
          </Button>
        )}
        <label className="k-field">Remind me to get in touch every … days (optional)<input inputMode="numeric" value={edit?.contact_every_days || ''} onChange={(e) => setEdit((x) => ({ ...x, contact_every_days: e.target.value }))} /></label>
        <label className="k-field">Notes<textarea rows={5} value={edit?.notes || ''} onChange={(e) => setEdit((x) => ({ ...x, notes: e.target.value }))} /></label>
        <Button variant="primary" block disabled={!edit?.name?.trim()} onClick={save}>Save</Button>
        {edit?.id && <Button variant="ghost" block style={{ marginTop: 6 }} onClick={async () => { await api('/api/people', { method: 'POST', body: { name: edit.name, contacted: true } }); setEdit(null); load() }}>We talked today</Button>}
      </Sheet>
    </>
  )
}

function Money() {
  const toast = useToast()
  const [days, setDays] = useState(30)
  const [d, setD] = useState(null)
  const file = useRef(null)
  const load = useCallback(() => api(`/api/finance?days=${days}`).then(setD), [days])
  useEffect(() => { load() }, [load])
  const upload = async (e) => {
    const f = e.target.files?.[0]
    if (!f) return
    const res = await fetch('/api/finance/import', { method: 'POST', headers: { 'Content-Type': 'application/xml' }, body: await f.arrayBuffer() })
    const r = await res.json()
    toast.show({ text: res.ok ? `Imported ${r.added} payments (${r.skipped} already there).` : r.detail })
    e.target.value = ''
    load()
  }
  if (!d) return <SkeletonCard />
  const max = Math.max(1, ...Object.values(d.by_category))
  return (
    <>
      <div className="k-pad" style={{ marginBottom: 8 }}>
        <Segmented id="moneydays" value={days} onChange={setDays} options={[{ value: 7, label: '7 days' }, { value: 30, label: '30 days' }, { value: 365, label: 'Year' }]} />
      </div>
      <Card>
        <p className="k-eyebrow">Spent</p>
        <div className="k-stat"><b>{chf(d.spent_chf)}</b>{d.income_chf > 0 && <span className="k-muted k-small">in: {chf(d.income_chf)}</span>}</div>
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {Object.entries(d.by_category).map(([k, v]) => (
            <div key={k} className="k-small">
              <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>{k}</span><span>{chf(v)}</span></div>
              <div style={{ height: 6, borderRadius: 3, background: 'var(--surface-strong)' }}><div style={{ width: `${(v / max) * 100}%`, height: '100%', borderRadius: 3, background: 'var(--accent)' }} /></div>
            </div>
          ))}
        </div>
      </Card>
      <SectionTitle>Recent</SectionTitle>
      {d.recent.length === 0 && <p className="k-muted k-small k-pad">No payments yet. Set up the Wallet Shortcut or import a UBS statement below.</p>}
      <List>{d.recent.map((t) => <ListRow key={t.id} title={t.merchant || '?'} subtitle={`${dayLabel(t.ts)} · ${t.category} · ${t.source}`} value={<span style={{ color: t.amount < 0 ? 'var(--text)' : 'var(--ok)' }}>{t.amount.toFixed(2)}</span>} />)}</List>
      <div className="k-pad" style={{ marginTop: 12 }}>
        <input ref={file} type="file" accept=".xml,application/xml,text/xml" style={{ display: 'none' }} onChange={upload} />
        <Button variant="secondary" block onClick={() => file.current?.click()}>Import UBS statement (camt.053)</Button>
        <p className="k-muted k-small">UBS e-banking → Accounts → account → Export → camt.053 (XML). Read only: Kairo can never pay or move money.</p>
      </div>
      <SectionTitle>Apple Pay → Kairo (once)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>Shortcuts → Automation → + → <b>Transaction</b> → choose your cards → <b>Run Immediately</b>.</li>
          <li>New Blank Automation → <b>Get Contents of URL</b>: your Kairo address + <b>/api/finance/transaction</b>, Method POST.</li>
          <li>Header <b>Authorization</b> = <b>Bearer</b> + the Shortcut key (More → Capture).</li>
          <li>Request Body JSON: <b>amount</b> = Shortcut Input → Amount, <b>merchant</b> = Merchant, <b>card</b> = Card or Pass.</li>
        </ol>
      </div>
    </>
  )
}

function Health() {
  const [d, setD] = useState(null)
  useEffect(() => { api('/api/health/summary?days=14').then(setD) }, [])
  if (!d) return <SkeletonCard />
  const a = d.avg
  return (
    <>
      <div className="k-grid2">
        <Card glass><p className="k-eyebrow">Sleep</p><div className="k-stat"><b>{a.sleep_h ?? '–'}</b><span className="k-muted k-small">h / night</span></div></Card>
        <Card glass><p className="k-eyebrow">Steps</p><div className="k-stat"><b>{a.steps ? Math.round(a.steps).toLocaleString() : '–'}</b><span className="k-muted k-small">/ day</span></div></Card>
        <Card glass><p className="k-eyebrow">Resting HR</p><div className="k-stat"><b>{a.resting_hr ?? '–'}</b><span className="k-muted k-small">bpm</span></div></Card>
        <Card glass><p className="k-eyebrow">Workouts</p><div className="k-stat"><b>{a.workout_min ?? '–'}</b><span className="k-muted k-small">min / day</span></div></Card>
      </div>
      <SectionTitle>Last 14 days</SectionTitle>
      {d.daily.length === 0 ? <p className="k-muted k-small k-pad">Nothing yet. Set up the Shortcut below.</p> : (
        <List>{d.daily.slice().reverse().map((r) => <ListRow key={r.date} title={dayLabel(r.date)} subtitle={[r.sleep_h != null && `sleep ${r.sleep_h} h`, r.steps != null && `${r.steps} steps`, r.workout_min && `${r.workout_min} min workout`].filter(Boolean).join(' · ')} />)}</List>
      )}
      <SectionTitle>Apple Health → Kairo (once)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>Shortcuts → Automation → + → <b>Time of Day</b> 07:00, daily → <b>Run Immediately</b>.</li>
          <li>Add <b>Find Health Samples</b>: Steps, start date is yesterday → <b>Calculate Statistics</b> Sum. Same for <b>Sleep</b> (Asleep, hours), <b>Resting Heart Rate</b> (Average), <b>Workouts</b> (Duration, minutes).</li>
          <li><b>Get Contents of URL</b>: your Kairo address + <b>/api/health</b>, POST, header <b>Authorization</b> = Bearer + Shortcut key.</li>
          <li>Request Body JSON: <b>steps</b>, <b>sleep_h</b>, <b>resting_hr</b>, <b>workout_min</b> = the results above.</li>
        </ol>
        <p className="k-muted k-small">Only these daily numbers reach the PC. Kairo uses them for the brief (“short night”) and the reviews.</p>
      </div>
    </>
  )
}

export default function Life() {
  const [view, setView] = useState(() => { try { return localStorage.getItem('life.view') || 'lists' } catch { return 'lists' } })
  useEffect(() => { try { localStorage.setItem('life.view', view) } catch { /* private */ } }, [view])
  return (
    <Page title="Life">
      <div className="k-pad" style={{ marginBottom: 12 }}>
        <Segmented id="lifeview" value={view} onChange={setView}
          options={[{ value: 'lists', label: 'Lists' }, { value: 'people', label: 'People' }, { value: 'money', label: 'Money' }, { value: 'health', label: 'Health' }]} />
      </div>
      {view === 'lists' && <Lists />}
      {view === 'people' && <People />}
      {view === 'money' && <Money />}
      {view === 'health' && <Health />}
    </Page>
  )
}
