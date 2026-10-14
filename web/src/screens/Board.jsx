import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Badge, Button, Card, EmptyState, IconBoard, IconCalendar, IconPlus, List, ListRow, Page, SectionTitle, Segmented, Sheet,
  SkeletonCard, Swipeable, useToast,
} from '../ui/index.js'

const COLUMNS = [
  { id: 'open', title: 'To do' },
  { id: 'doing', title: 'Doing' },
  { id: 'done', title: 'Done' },
]
const NEXT = { open: 'doing', doing: 'done', done: 'open' }
const PRIO = ['', 'Low', 'Medium', 'High']

const dayLabel = (iso) => new Date(iso).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })
const localDate = (iso) => (iso ? new Date(iso).toLocaleDateString('sv-SE') : '')

function startOfWeek(d) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7))
  return x
}

function TaskCard({ t, project, onOpen, onAdvance }) {
  const overdue = t.due && t.status !== 'done' && new Date(t.due) < new Date(new Date().toDateString())
  return (
    <Swipeable right={{ label: t.status === 'done' ? 'Reopen' : t.status === 'doing' ? 'Done' : 'Start', onAction: () => onAdvance(t) }}>
      <motion.div layout="position" transition={spring.smooth}>
        <Card interactive onClick={() => onOpen(t)} style={{ margin: '0 0 8px' }}>
          <p className="k-card-title" style={{ fontSize: 16, textDecoration: t.status === 'done' ? 'line-through' : 'none', opacity: t.status === 'done' ? 0.6 : 1 }}>{t.title}</p>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
            {t.due && <Badge tone={overdue ? 'danger' : 'neutral'} pop={false}>{dayLabel(t.due)}</Badge>}
            {t.priority >= 2 && <Badge tone="warn" pop={false}>{PRIO[t.priority]}</Badge>}
            {project && <Badge tone="accent" pop={false}>{project.title}</Badge>}
            {t.place && <Badge tone="neutral" pop={false}>@ {t.place}</Badge>}
          </div>
        </Card>
      </motion.div>
    </Swipeable>
  )
}

function Kanban({ data, onOpen, onAdvance }) {
  const projects = useMemo(() => Object.fromEntries(data.projects.map((p) => [p.id, p])), [data.projects])
  return (
    <div className="k-kanban">
      {COLUMNS.map((c) => {
        const all = data.tasks.filter((t) => t.status === c.id)
        const items = c.id === 'done' ? all.slice(0, 10) : all
        return (
          <section key={c.id} className="k-kanban-col">
            <h3 className="k-section-title" style={{ margin: '0 0 8px' }}>{c.title} · {all.length}</h3>
            <AnimatePresence initial={false}>
              {items.map((t) => (
                <motion.div key={t.id} initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }} transition={spring.smooth}>
                  <TaskCard t={t} project={projects[t.project_id]} onOpen={onOpen} onAdvance={onAdvance} />
                </motion.div>
              ))}
            </AnimatePresence>
            {items.length === 0 && <p className="k-muted k-small">Nothing here.</p>}
          </section>
        )
      })}
    </div>
  )
}

function Timeline({ data, onOpen }) {
  const today = new Date(new Date().toDateString())
  const items = [
    ...data.tasks.filter((t) => t.due && t.status !== 'done').map((t) => ({ kind: 'task', date: new Date(t.due), title: t.title, t })),
    ...data.school.map((e) => ({ kind: 'school', date: new Date(e.date), title: `${e.subject}: ${e.kind}` })),
    ...data.goals.filter((g) => g.target_date).map((g) => ({ kind: 'goal', date: new Date(g.target_date), title: `Goal: ${g.title}` })),
  ].sort((a, b) => a.date - b.date)
  if (!items.length) return <EmptyState icon={<IconCalendar />} title="Nothing dated yet" text="Give tasks a due date, add test dates or goal dates." />
  const overdue = items.filter((i) => i.date < today && i.kind === 'task')
  const rest = items.filter((i) => !(i.date < today && i.kind === 'task'))
  const groups = []
  for (const i of rest) {
    const w = startOfWeek(i.date).getTime()
    const diff = Math.round((w - startOfWeek(today).getTime()) / (7 * 864e5))
    const wd = new Date(w)
    const label = diff <= 0 ? 'This week' : diff === 1 ? 'Next week' : `Week of ${wd.toLocaleDateString([], { day: 'numeric', month: 'short', ...(wd.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}) })}`
    if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, items: [] })
    groups[groups.length - 1].items.push(i)
  }
  const row = (i, k) => (
    <ListRow key={k} title={i.title} subtitle={dayLabel(i.date.toISOString())}
      value={<Badge tone={i.kind === 'school' ? 'warn' : i.kind === 'goal' ? 'accent' : 'neutral'} pop={false}>{i.kind}</Badge>}
      onClick={i.t ? () => onOpen(i.t) : undefined} />
  )
  return (
    <>
      {overdue.length > 0 && <><SectionTitle>Overdue</SectionTitle><List>{overdue.map((i, k) => row(i, `o${k}`))}</List></>}
      {groups.map((g) => <div key={g.label}><SectionTitle>{g.label}</SectionTitle><List>{g.items.map((i, k) => row(i, `${g.label}${k}`))}</List></div>)}
    </>
  )
}

function Goals({ data, onNewProject, onNewGoal }) {
  const byGoal = (gid) => data.projects.filter((p) => p.goal_id === gid)
  const progress = (pid) => {
    const ts = data.tasks.filter((t) => t.project_id === pid)
    return ts.length ? `${ts.filter((t) => t.status === 'done').length}/${ts.length} done` : 'no tasks yet'
  }
  const loose = data.projects.filter((p) => !p.goal_id)
  return (
    <>
      {data.goals.length === 0 && <EmptyState icon={<IconBoard />} title="No goals yet" text="Add one, or do the onboarding interview first." />}
      {data.goals.map((g) => (
        <div key={g.id}>
          <SectionTitle>{g.title}{g.target_date ? ` · by ${dayLabel(g.target_date)}` : ''}</SectionTitle>
          <List>
            {byGoal(g.id).map((p) => <ListRow key={p.id} title={p.title} subtitle={progress(p.id)} />)}
            <ListRow title="Add a project" icon={<IconPlus />} onClick={() => onNewProject(g.id)} />
          </List>
        </div>
      ))}
      {loose.length > 0 && <><SectionTitle>Projects without a goal</SectionTitle><List>{loose.map((p) => <ListRow key={p.id} title={p.title} subtitle={progress(p.id)} />)}</List></>}
      <div className="k-pad" style={{ marginTop: 12, display: 'flex', gap: 8 }}>
        <Button variant="secondary" block onClick={onNewGoal}>New goal</Button>
        <Button variant="secondary" block onClick={() => onNewProject(null)}>New project</Button>
      </div>
    </>
  )
}

function TaskSheet({ open, task, projects, onClose, onSave }) {
  const [f, setF] = useState({})
  useEffect(() => {
    if (open) setF(task ? { title: task.title, due: localDate(task.due), priority: task.priority, project_id: task.project_id ?? '', place: task.place ?? '', status: task.status }
      : { title: '', due: '', priority: 0, project_id: '', place: '', status: 'open' })
  }, [open, task])
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }))
  const save = () => {
    const body = { title: f.title.trim(), priority: Number(f.priority), status: f.status }
    const clear = []
    if (f.due) body.due = new Date(`${f.due}T12:00:00`).toISOString(); else if (task?.due) clear.push('due')
    if (f.project_id) body.project_id = Number(f.project_id); else if (task?.project_id) clear.push('project_id')
    if (f.place.trim()) body.place = f.place.trim(); else if (task?.place) clear.push('place')
    if (task && clear.length) body.clear = clear
    onSave(body)
  }
  return (
    <Sheet open={open} onClose={onClose} title={task ? 'Task' : 'New task'}>
      <label className="k-field">Title<input value={f.title ?? ''} onChange={set('title')} autoFocus={!task} /></label>
      <div style={{ display: 'flex', gap: 8 }}>
        <label className="k-field" style={{ flex: 1 }}>Due<input type="date" value={f.due ?? ''} onChange={set('due')} /></label>
        <label className="k-field" style={{ flex: 1 }}>Priority
          <select value={f.priority ?? 0} onChange={set('priority')}>{PRIO.map((p, i) => <option key={i} value={i}>{p || 'None'}</option>)}</select>
        </label>
      </div>
      <label className="k-field">Project
        <select value={f.project_id ?? ''} onChange={set('project_id')}>
          <option value="">None</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
        </select>
      </label>
      <label className="k-field">Remind me at (place)<input value={f.place ?? ''} onChange={set('place')} placeholder="Home, School, Work, Gym" /></label>
      {task && <Segmented id="taskstatus" value={f.status} onChange={(v) => setF((x) => ({ ...x, status: v }))} options={COLUMNS.map((c) => ({ value: c.id, label: c.title }))} />}
      <Button variant="primary" block style={{ marginTop: 16 }} disabled={!f.title?.trim()} onClick={save}>{task ? 'Save' : 'Add task'}</Button>
      {task && <Button variant="ghost" block style={{ marginTop: 6 }} onClick={() => onSave({ status: 'dropped' })}>Drop this task</Button>}
    </Sheet>
  )
}

function NameSheet({ open, title, withDate, onClose, onSave }) {
  const [name, setName] = useState('')
  const [date, setDate] = useState('')
  useEffect(() => { if (open) { setName(''); setDate('') } }, [open])
  return (
    <Sheet open={open} onClose={onClose} title={title}>
      <label className="k-field">Name<input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
      {withDate && <label className="k-field">Target date (optional)<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>}
      <Button variant="primary" block disabled={!name.trim()} onClick={() => onSave(name.trim(), date || null)}>Add</Button>
    </Sheet>
  )
}

export default function Board() {
  const toast = useToast()
  const [view, setView] = useState(() => { try { return localStorage.getItem('board.view') || 'kanban' } catch { return 'kanban' } })
  const [data, setData] = useState(null)
  const [editing, setEditing] = useState(null)   // task | 'new' | null
  const [newGoal, setNewGoal] = useState(false)
  const [newProject, setNewProject] = useState(undefined)   // goal id | null | undefined (closed)

  const load = useCallback(() => api('/api/board').then(setData).catch((e) => toast.show({ text: e.message })), [toast])
  useEffect(() => { load() }, [load])
  useEffect(() => { try { localStorage.setItem('board.view', view) } catch { /* private mode */ } }, [view])

  const patchTask = async (id, body) => {
    const t = await api(`/api/tasks/${id}`, { method: 'PATCH', body })
    setData((d) => ({ ...d, tasks: t.status === 'dropped' ? d.tasks.filter((x) => x.id !== id) : d.tasks.map((x) => (x.id === id ? t : x)) }))
    return t
  }
  const advance = async (t) => {
    const next = NEXT[t.status]
    await patchTask(t.id, { status: next })
    toast.show({ text: next === 'done' ? 'Done. Nice.' : next === 'doing' ? 'Started.' : 'Back to do.', action: { label: 'Undo', onAction: () => patchTask(t.id, { status: t.status }) } })
  }
  const save = async (body) => {
    try {
      if (editing === 'new') {
        const t = await api('/api/tasks', { method: 'POST', body })
        setData((d) => ({ ...d, tasks: [t, ...d.tasks] }))
      } else {
        await patchTask(editing.id, body)
      }
      setEditing(null)
    } catch (e) { toast.show({ text: e.message }) }
  }

  return (
    <Page title="Board" onRefresh={load}
      right={<Button variant="ghost" size="sm" icon={<IconPlus />} onClick={() => setEditing('new')} aria-label="New task" />}>
      <div className="k-pad" style={{ marginBottom: 12 }}>
        <Segmented id="boardview" value={view} onChange={setView}
          options={[{ value: 'kanban', label: 'Tasks' }, { value: 'timeline', label: 'Timeline' }, { value: 'goals', label: 'Goals' }]} />
      </div>
      {!data && <><SkeletonCard /><SkeletonCard /></>}
      {data && view === 'kanban' && <Kanban data={data} onOpen={setEditing} onAdvance={advance} />}
      {data && view === 'timeline' && <Timeline data={data} onOpen={setEditing} />}
      {data && view === 'goals' && <Goals data={data} onNewGoal={() => setNewGoal(true)} onNewProject={(gid) => setNewProject(gid)} />}
      {data && view === 'kanban' && <p className="k-muted k-small k-pad">Swipe a card right to move it on. Tap to edit.</p>}

      <TaskSheet open={editing !== null} task={editing === 'new' ? null : editing} projects={data?.projects || []}
        onClose={() => setEditing(null)} onSave={save} />
      <NameSheet open={newGoal} title="New goal" withDate onClose={() => setNewGoal(false)}
        onSave={async (title, date) => { await api('/api/goals', { method: 'POST', body: { title, ...(date ? { target_date: date } : {}) } }); setNewGoal(false); load() }} />
      <NameSheet open={newProject !== undefined} title="New project" onClose={() => setNewProject(undefined)}
        onSave={async (title) => { await api('/api/projects', { method: 'POST', body: { title, ...(newProject ? { goal_id: newProject } : {}) } }); setNewProject(undefined); load() }} />
    </Page>
  )
}
