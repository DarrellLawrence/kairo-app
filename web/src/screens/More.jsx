import { useCallback, useEffect, useState } from 'react'
import { api, parseTs } from '../api.js'
import {
  Badge, Button, EmptyState, IconBell, IconCheck, IconInbox, IconKey, IconList, IconMail, IconPower, IconPulse, IconRefresh,
  IconPin, IconShield, IconSparkle, IconWarn, List, ListRow, Page, SectionTitle, Sheet, SkeletonCard, Stack, useStack, useToast,
} from '../ui/index.js'

const STATUS_TONE = { ok: 'ok', skipped: 'neutral', error: 'danger', running: 'warn' }

function JobsPage() {
  const toast = useToast()
  const [data, setData] = useState(null)
  const load = useCallback(() => api('/api/jobs').then(setData).catch(() => setData({ jobs: [], brain: null })), [])
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t) }, [load])
  const run = async (j) => {
    await api(`/api/jobs/${j.name}/run`, { method: 'POST', body: {} })
    toast.show({ text: `${j.title} started.` })
    setTimeout(load, 800)
  }
  const b = data?.brain
  return (
    <Page title="Jobs" onRefresh={load}>
      {!data && <SkeletonCard lines={4} />}
      {b && (
        <p className="k-hello">
          Claude runs today: {b.runs_today} of {b.budget}.{b.paused_until ? ` Paused (usage limit) until ${parseTs(b.paused_until)?.toLocaleTimeString()}.` : ''}
        </p>
      )}
      {data && (
        <List>
          {data.jobs.map((j) => (
            <ListRow key={j.name} title={j.title} wrap
              subtitle={j.last ? `${parseTs(j.last.started_at)?.toLocaleString()} · ${j.last.status}${j.last.error ? ` · ${j.last.error}` : ''}` : 'Not run yet'}
              value={<Badge tone={STATUS_TONE[j.last?.status] || 'neutral'} pop={false}>{j.last?.status ?? '–'}</Badge>}
              onClick={() => run(j)} />
          ))}
        </List>
      )}
      <p className="k-muted k-small k-pad" style={{ marginTop: 12 }}>
        Tap a job to run it now. Jobs run one at a time, never between 23:00 and 06:00, and not while Kairo is paused.
      </p>
    </Page>
  )
}

function copy(text, toast) {
  navigator.clipboard?.writeText(text).then(() => toast.show({ text: 'Copied.' }), () => toast.show({ text: 'Long-press to copy.' }))
}

function CapturePage() {
  const toast = useToast()
  const [setup, setSetup] = useState(null)
  const [confirm, setConfirm] = useState(false)
  useEffect(() => { api('/api/capture/setup').then(setSetup).catch(() => setSetup({})) }, [])
  const url = setup?.url || `${window.location.origin}/api/capture`
  const rotate = async () => {
    const r = await api('/api/capture/token/rotate', { method: 'POST', body: {} })
    setSetup((s) => ({ ...s, token: r.token }))
    setConfirm(false)
    toast.show({ text: 'New key. Put it into both Shortcuts.' })
  }
  return (
    <Page title="Capture">
      <p className="k-hello">Send anything to Kairo in a second: a task, a test date, an idea, a photo of a letter.</p>
      <SectionTitle>Address</SectionTitle>
      <div className="k-pad"><div className="k-code" onClick={() => copy(url, toast)}>{url}</div></div>
      <SectionTitle>Key</SectionTitle>
      <div className="k-pad">
        <div className="k-code" onClick={() => setup?.token && copy(setup.token, toast)}>{setup?.token ?? '…'}</div>
        <p className="k-muted k-small">Tap to copy. Only for your Shortcuts. If it ever leaks: </p>
        <Button variant="secondary" size="sm" onClick={() => setConfirm(true)}>Make a new key</Button>
      </div>

      <SectionTitle>Shortcut 1: Action button (text)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>Open the <b>Shortcuts</b> app → <b>+</b> → name it <b>Kairo Capture</b>.</li>
          <li>Add <b>Ask for Input</b> (type: Text, prompt: “Capture”).</li>
          <li>Add <b>Get Contents of URL</b>. URL: the address above. Tap the arrow: Method <b>POST</b>.</li>
          <li>Headers → Add: key <b>Authorization</b>, value <b>Bearer</b> + space + the key above.</li>
          <li>Request Body: <b>JSON</b> → Add field: key <b>text</b>, type Text, value: <b>Provided Input</b>.</li>
          <li>Add <b>Show Notification</b>: “Captured”.</li>
          <li>iPhone Settings → <b>Action Button</b> → Shortcut → <b>Kairo Capture</b>.</li>
        </ol>
      </div>

      <SectionTitle>Shortcut 2: Share sheet (photos, PDFs)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>New shortcut <b>Send to Kairo</b>. Tap the <b>(i)</b> → turn on <b>Show in Share Sheet</b>, types: <b>Images</b> and <b>PDFs</b>.</li>
          <li>For photos: add <b>Convert Image</b> → <b>JPEG</b>, then <b>Resize Image</b> → width <b>2000</b> (Kairo can't read iPhone HEIC photos).</li>
          <li>Add <b>Get Contents of URL</b>. URL: the address above + <b>?source=share</b>. Method <b>POST</b>.</li>
          <li>Same <b>Authorization</b> header as above.</li>
          <li>Request Body: <b>File</b> → <b>Shortcut Input</b>.</li>
          <li>Add <b>Show Notification</b>: “Sent to Kairo”.</li>
        </ol>
        <p className="k-muted k-small">Links and text from other apps: copy, then press the Action button and paste.</p>
      </div>

      <SectionTitle>What happens</SectionTitle>
      <div className="k-pad">
        <p className="k-small">Within 15 minutes Kairo files it: task, school date, note, person, idea. Letters and bills (photo or PDF): Claude reads them, a bill becomes a “Pay …” task with its due date, a note goes to 80 Admin/Letters. Voice memos are transcribed on the PC first. If it is unclear, Kairo asks you on Today. It never sends, deletes or pays anything because of a capture.</p>
      </div>

      <Sheet open={confirm} onClose={() => setConfirm(false)} title="New key?">
        <p className="k-small" style={{ textAlign: 'center', margin: '0 0 20px' }}>The old key stops working. You have to paste the new one into both Shortcuts.</p>
        <Button variant="danger" block onClick={rotate}>Make a new key</Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setConfirm(false)}>Cancel</Button>
      </Sheet>
    </Page>
  )
}

function VoicePage() {
  const toast = useToast()
  const [st, setSt] = useState(null)
  const [test, setTest] = useState(null)
  const [testing, setTesting] = useState(false)
  const load = useCallback(() => api('/api/voice/status').then(setSt).catch(() => setSt(null)), [])
  useEffect(() => { load() }, [load])
  const runTest = async () => {
    setTesting(true)
    try { setTest(await api('/api/voice/selftest', { method: 'POST', body: {} })) } catch (e) { setTest({ ok: false, error: e.message }) } finally { setTesting(false) }
  }
  const callNow = async () => {
    const c = await api('/api/voice/calls', { method: 'POST', body: { reason: 'Test call' } })
    toast.show({ text: 'Calling you now. Lock the phone to see the push, or open it here.' })
    setTimeout(() => { window.location.hash = `#call/${c.id}` }, 1200)
  }
  const setOpt = async (key, on) => {
    const opts = await api(`/api/voice/options/${key}`, { method: 'POST', body: { on } })
    setSt((x) => ({ ...x, call_options: opts }))
  }
  const ok = (b) => <Badge tone={b ? 'ok' : 'warn'} pop={false}>{b ? 'Ready' : 'Missing'}</Badge>
  return (
    <Page title="Voice" onRefresh={load}>
      <p className="k-hello">Speech runs on your PC: nothing you say leaves it. Talk in Chat with the mic, or let Kairo call you.</p>
      {!st && <SkeletonCard />}
      {st && (
        <>
          <List>
            <ListRow icon={<IconSparkle />} title="Speech recognition" subtitle={st.stt_device ? `On the ${st.stt_device === 'cuda' ? 'graphics card' : 'processor (slower)'}` : 'Loads on first use'}
              value={ok(st.installed?.stt)} />
            <ListRow icon={<IconSparkle />} title="English voice" subtitle="Kokoro" value={ok(st.en_ready)} />
            <ListRow icon={<IconSparkle />} title="German voice" subtitle="Piper (Thorsten)" value={ok(st.de_ready)} />
          </List>
          {st.last_error && <p className="k-muted k-small k-pad">Last problem: {st.last_error}</p>}
          <div className="k-pad" style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <Button variant="secondary" block loading={testing} onClick={runTest}>Test voice</Button>
            <Button variant="primary" block onClick={callNow}>Call me now</Button>
          </div>
          {test && (
            <p className={test.ok ? 'k-small k-pad' : 'k-small k-pad'} style={{ color: test.ok ? 'var(--ok)' : 'var(--danger)', marginTop: 8 }}>
              {test.ok ? `Works. Heard “${test.heard}” in ${test.took}s on the ${test.device === 'cuda' ? 'graphics card' : 'processor'}.` : `Not working: ${test.error || `heard “${test.heard}”`}`}
            </p>
          )}
          <SectionTitle>Kairo calls you for</SectionTitle>
          <List>
            {Object.entries(st.call_option_titles || {}).map(([k, title]) => (
              <ListRow key={k} title={title} subtitle={st.call_options?.[k] ? 'On: a push “Kairo is calling”' : 'Off: a normal push'}
                value={<Badge tone={st.call_options?.[k] ? 'ok' : 'neutral'} pop={false}>{st.call_options?.[k] ? 'On' : 'Off'}</Badge>}
                onClick={() => setOpt(k, !st.call_options?.[k])} />
            ))}
          </List>
          <p className="k-muted k-small k-pad">A real ringing phone needs a small native iPhone app (CallKit) and an Apple Developer account. Later, if you want it.</p>
        </>
      )}
    </Page>
  )
}

function BackupsPage() {
  const toast = useToast()
  const [d, setD] = useState(null)
  const load = useCallback(() => api('/api/backups').then(setD).catch(() => setD(null)), [])
  useEffect(() => { load() }, [load])
  const run = async (name, label) => {
    await api(`/api/jobs/${name}/run`, { method: 'POST', body: {} })
    toast.show({ text: `${label} started.` })
    setTimeout(load, 4000)
  }
  const st = d?.status || {}
  const mb = (n) => `${(n / 1048576).toFixed(1)} MB`
  return (
    <Page title="Backups" onRefresh={load}>
      <p className="k-hello">Every night at 03:30: database, vault, settings and captures. Every Sunday Kairo restores the newest one into a test folder and checks it.</p>
      {!d && <SkeletonCard />}
      {d && (
        <>
          <List>
            <ListRow icon={<IconShield />} title="Last backup" wrap
              subtitle={st.last_error ? `Failed: ${st.last_error}` : st.last_backup_at ? `${new Date(st.last_backup_at).toLocaleString()} · ${mb(st.last_size || 0)}` : 'Not yet'}
              value={<Badge tone={st.last_error ? 'danger' : st.last_backup_at ? 'ok' : 'neutral'} pop={false}>{st.last_error ? 'Failed' : st.last_backup_at ? 'OK' : '–'}</Badge>} />
            <ListRow icon={<IconCheck />} title="Last restore test" wrap
              subtitle={st.test_at ? `${new Date(st.test_at).toLocaleString()}${st.test_ok ? ` · ${st.test_detail?.rows} rows, ${st.test_detail?.vault_files} vault files` : ` · ${st.test_detail}`}` : 'Not yet'}
              value={<Badge tone={st.test_ok === false ? 'danger' : st.test_ok ? 'ok' : 'neutral'} pop={false}>{st.test_ok === false ? 'Failed' : st.test_ok ? 'OK' : '–'}</Badge>} />
          </List>
          <div className="k-pad" style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <Button variant="secondary" block onClick={() => run('backup', 'Backup')}>Back up now</Button>
            <Button variant="secondary" block onClick={() => run('restore_test', 'Restore test')}>Test restore</Button>
          </div>
          <SectionTitle>Kept ({d.backups.length})</SectionTitle>
          <List>{d.backups.map((b) => <ListRow key={b.name} title={b.name} subtitle={mb(b.size)} />)}</List>
          <p className="k-muted k-small k-pad">On the PC in {d.folder}. Google sign-ins and keys are not in backups: after a restore, run Kairo Logins again.</p>
        </>
      )}
    </Page>
  )
}

function LocationPage() {
  const toast = useToast()
  const [d, setD] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const load = useCallback(() => api('/api/location').then(setD).catch(() => setD(null)), [])
  useEffect(() => { load() }, [load])
  const setPause = async (paused) => {
    await api('/api/location/pause', { method: 'POST', body: { paused } })
    toast.show({ text: paused ? 'Location paused. Nothing is stored.' : 'Location on again.' })
    load()
  }
  const del = async (id) => {
    await api(`/api/location/events/${id}`, { method: 'DELETE' })
    setD((x) => ({ ...x, history: x.history.filter((e) => e.id !== id) }))
  }
  const delAll = async () => {
    const r = await api('/api/location/events', { method: 'DELETE' })
    setConfirm(false)
    toast.show({ text: `Deleted ${r.deleted} entries.` })
    load()
  }
  const url = d?.url || `${window.location.origin}/api/location/event`
  return (
    <Page title="Location" onRefresh={load}>
      <p className="k-hello">Only the place name and the time, only on your PC. Never sent anywhere. Kept {d?.keep_days ?? 365} days.</p>
      {!d && <SkeletonCard />}
      {d && (
        <>
          <List>
            <ListRow icon={<IconPower />} title={d.paused ? 'Paused' : 'On'} subtitle={d.paused ? 'Nothing is stored. Tap to turn on.' : `Tap to pause${d.now ? ` · now: ${d.now}` : ''}`}
              value={<Badge tone={d.paused ? 'warn' : 'ok'} pop={false}>{d.paused ? 'Paused' : 'On'}</Badge>}
              onClick={() => setPause(!d.paused)} />
          </List>

          {d.patterns.length > 0 && (
            <>
              <SectionTitle>What Kairo noticed</SectionTitle>
              <List>{d.patterns.map((f) => <ListRow key={f} title={f.split(':')[0]} subtitle={f.includes(':') ? f.slice(f.indexOf(':') + 1).trim() : ''} wrap />)}</List>
            </>
          )}

          <SectionTitle>History ({d.history.length})</SectionTitle>
          {d.history.length === 0 ? <p className="k-muted k-small k-pad">Nothing stored.</p> : (
            <>
              <List>
                {d.history.slice(0, 100).map((e) => (
                  <ListRow key={e.id} title={`${e.event === 'arrive' ? 'Arrived at' : 'Left'} ${e.place}`}
                    subtitle={new Date(e.ts).toLocaleString()} swipe={{ left: { label: 'Delete', onAction: () => del(e.id) } }} />
                ))}
              </List>
              <p className="k-muted k-small k-pad">Swipe left to delete one.</p>
              <div className="k-pad"><Button variant="danger" block onClick={() => setConfirm(true)}>Delete all location history</Button></div>
            </>
          )}

          <SectionTitle>iPhone setup (once per place)</SectionTitle>
          <div className="k-pad">
            <ol className="k-steps">
              <li>Open <b>Shortcuts</b> → <b>Automation</b> tab → <b>+</b> → <b>Arrive</b>.</li>
              <li>Location: <b>Choose</b> → pick Home (search the address) → Done. Set <b>Run Immediately</b> (and turn off “Notify When Run”).</li>
              <li><b>New Blank Automation</b> → add <b>Get Contents of URL</b>. URL: <span className="k-code" style={{ display: 'inline' }} onClick={() => copy(url, toast)}>{url}</span></li>
              <li>Method <b>POST</b>. Headers → <b>Authorization</b> = <b>Bearer</b> + space + the key below.</li>
              <li>Request Body <b>JSON</b>: <b>place</b> = <b>Home</b>, <b>event</b> = <b>arrive</b>. Done.</li>
              <li>Same again with <b>Leave</b> and event = <b>leave</b>.</li>
              <li>Repeat for <b>School</b>, <b>Work</b>, <b>Gym</b> (8 automations in total). Use exactly these names.</li>
            </ol>
            <p className="k-small" style={{ marginTop: 12 }}>Key (tap to copy):</p>
            <div className="k-code" onClick={() => copy(d.token, toast)}>{d.token}</div>
            <p className="k-muted k-small">Tip: tell Kairo “remind me at the gym to …” and it pops up when you arrive there.</p>
          </div>
        </>
      )}
      <Sheet open={confirm} onClose={() => setConfirm(false)} title="Delete all?">
        <p className="k-small" style={{ textAlign: 'center', margin: '0 0 20px' }}>Every stored arrive/leave entry is deleted. This can't be undone.</p>
        <Button variant="danger" block onClick={delAll}>Delete all</Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setConfirm(false)}>Cancel</Button>
      </Sheet>
    </Page>
  )
}

const TODOS = [
  { id: 'google_publish', title: 'Publish the Google app', why: 'Stops Google from signing Kairo out every 7 days.',
    steps: ['On the PC open console.cloud.google.com and pick the Kairo project.', 'Menu → Google Auth Platform → Audience.',
      'Under “Publishing status” click Publish app → Confirm.', 'Google shows “unverified app”: fine, it is only you.',
      'Then in Kairo: More → Google → Sign in to Google again (once).'] },
  { id: 'bios_power_on', title: 'BIOS: turn on after power loss', why: 'After a power cut the PC starts by itself, so Kairo comes back.',
    steps: ['Restart the PC and press Del (or F2) right away to open the BIOS.', 'Look for “Restore on AC Power Loss”, “AC Back” or “Power On after Power Failure” (often under Advanced → APM / Power).',
      'Set it to Power On (or “Last State”).', 'Save and exit (usually F10).', 'Test: pull the plug, plug it back in. It should start alone.'] },
  { id: 'github_2fa', title: 'GitHub: two-factor login', why: 'Kairo updates come from your GitHub; protect that account.',
    steps: ['github.com → your picture → Settings → Password and authentication.', 'Enable two-factor authentication → Authenticator app.',
      'Scan the code with an authenticator app (e.g. the iPhone Passwords app).', 'Save the recovery codes in a safe place (not in OneDrive Kairo).'] },
]

function TodoPage({ todo, done, onToggle }) {
  return (
    <Page title={todo.title}>
      <p className="k-hello">{todo.why}</p>
      <div className="k-pad"><ol className="k-steps">{todo.steps.map((s) => <li key={s}>{s}</li>)}</ol></div>
      <div className="k-pad" style={{ marginTop: 20 }}>
        <Button variant={done ? 'secondary' : 'primary'} block onClick={onToggle}>{done ? 'Mark as not done' : 'I did it'}</Button>
      </div>
    </Page>
  )
}

function urlBase64ToUint8Array(base64) {
  const pad = '='.repeat((4 - (base64.length % 4)) % 4)
  const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'))
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)))
}

function BootCheckPage({ check }) {
  return (
    <Page title="Boot check" tabbar>
      {!check && <EmptyState icon={<IconShield />} title="No boot check yet" text="It runs 3 minutes after the PC starts, with nobody logged in." />}
      {check?.error && <div className="k-banner">{check.error}</div>}
      {check?.checks && (
        <>
          <p className="k-hello">{check.all_ok ? 'Everything came back by itself.' : 'Something did not come back.'} {new Date(check.time).toLocaleString()} ({check.trigger})</p>
          <List>
            {check.checks.map((c) => (
              <ListRow key={c.name} title={c.name} subtitle={c.detail} wrap
                value={<Badge tone={c.ok ? 'ok' : 'danger'} pop={false}>{c.ok ? 'OK' : 'Failed'}</Badge>} />
            ))}
          </List>
        </>
      )}
    </Page>
  )
}

function ActivityPage() {
  const [rows, setRows] = useState(null)
  const load = useCallback(() => api('/api/activity?limit=100').then(setRows).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load])
  return (
    <Page title="Activity" onRefresh={load}>
      {rows === null && <><SkeletonCard /><SkeletonCard /></>}
      {rows?.length === 0 && <EmptyState icon={<IconList />} title="Nothing yet" />}
      {rows?.length > 0 && (
        <List>
          {rows.map((r) => (
            <ListRow key={r.id} title={r.action} wrap
              subtitle={`${parseTs(r.ts)?.toLocaleString()} · ${r.actor}${r.tier !== null ? ` · tier ${r.tier}` : ''} · ${r.outcome}`} />
          ))}
        </List>
      )}
    </Page>
  )
}

function GooglePage({ st }) {
  const ok = st?.read && st?.write && !st?.signed_out
  const ends = st ? [st.read_may_end, st.write_may_end].filter(Boolean) : []
  const end = ends.length ? new Date(Math.min(...ends) * 1000) : null
  return (
    <Page title="Google">
      {!st && <SkeletonCard />}
      {st && (
        <>
          <List>
            <ListRow icon={<IconMail />} title={st.read || st.expected || 'Gmail'} subtitle={st.signed_out ? 'Google signed Kairo out' : ok ? 'Connected (read + approved writes)' : 'Not connected yet'}
              value={<Badge tone={ok ? 'ok' : 'warn'} pop={false}>{ok ? 'OK' : 'Fix'}</Badge>} />
            {ok && end && <ListRow title="Sign-in may end" subtitle={`${end.toLocaleString()} (Google Testing mode). Reminder the day before.`} wrap />}
          </List>
          <div className="k-pad">
            {(st.read || st.write) && st.phone_client && (
              <a className="k-btn primary block" href="/setup/google/start?kind=read">Sign in to Google again</a>
            )}
            {(st.read || st.write) && !st.phone_client && (
              <p className="k-muted k-small">To sign in again from the iPhone, run Kairo Logins on the PC once (Google step).</p>
            )}
            {!st.read && <p className="k-muted k-small">On the PC: Start menu → Kairo → Kairo Logins.</p>}
          </div>
        </>
      )}
    </Page>
  )
}

function Root() {
  const stack = useStack()
  const toast = useToast()
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [google, setGoogle] = useState(null)
  const [paused, setPaused] = useState(null)
  const [askPause, setAskPause] = useState(false)
  const [perm, setPerm] = useState(() => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission))
  const [todos, setTodos] = useState({})

  const load = useCallback(async () => {
    const [h, s, g, k] = await Promise.allSettled([
      fetch('/health').then((r) => r.json()), api('/api/status'), api('/api/setup/google/status'), api('/api/kill-switch'),
    ])
    setHealth(h.status === 'fulfilled' ? (h.value.detail ?? h.value) : { status: 'unreachable' })
    setStatus(s.status === 'fulfilled' ? s.value : {})
    setGoogle(g.status === 'fulfilled' ? g.value : null)
    setPaused(k.status === 'fulfilled' ? k.value.paused : null)
    api('/api/owner-todos').then(setTodos).catch(() => {})
  }, [])

  const toggleTodo = async (id) => {
    const next = await api(`/api/owner-todos/${id}`, { method: 'POST', body: { done: !todos[id] } })
    setTodos(next)
    stack.pop()
    toast.show({ text: next[id] ? 'Nice. Ticked off.' : 'Back on the list.' })
  }
  const openTodos = TODOS.filter((t) => !todos[t.id])
  useEffect(() => { load() }, [load])

  const setKill = async (value) => {
    const r = await api('/api/kill-switch', { method: 'POST', body: { paused: value } })
    setPaused(r.paused)
    toast.show({ text: r.paused ? 'Kairo is paused. Nothing runs.' : 'Kairo is running again.' })
  }

  const enablePush = async () => {
    try {
      const p = await Notification.requestPermission()
      setPerm(p)
      if (p !== 'granted') return
      const reg = await navigator.serviceWorker.ready
      const { publicKey } = await api('/api/push/key')
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(publicKey) })
      const json = sub.toJSON()
      await api('/api/push/subscribe', { method: 'POST', body: { endpoint: json.endpoint, keys: json.keys, device: navigator.userAgent.includes('iPhone') ? 'iphone' : 'browser' } })
      await api('/api/push/test', { method: 'POST' })
      toast.show({ text: 'Done. A test notification is on its way.' })
    } catch (e) { toast.show({ text: `Could not turn on: ${e.message}`, duration: 5000 }) }
  }

  const boot = status?.boot_check
  const googleOk = google?.read && google?.write && !google?.signed_out
  return (
    <Page title="More" onRefresh={load}>
      {health === null ? <SkeletonCard lines={3} /> : (
        <>
          {openTodos.length > 0 && (
            <>
              <SectionTitle>Your to-dos ({openTodos.length})</SectionTitle>
              <List>
                {openTodos.map((t) => (
                  <ListRow key={t.id} icon={<IconWarn />} title={t.title} subtitle={t.why} wrap chevron
                    onClick={() => stack.push(t.title, () => <TodoPage todo={t} done={false} onToggle={() => toggleTodo(t.id)} />)} />
                ))}
              </List>
            </>
          )}

          <SectionTitle>Kairo</SectionTitle>
          <List>
            <ListRow icon={<IconPower />} title={paused ? 'Paused (kill switch)' : 'Running'}
              subtitle={paused ? 'Nothing runs until you resume' : 'Tap to pause everything'}
              value={<Badge tone={paused ? 'danger' : 'ok'} pop={false}>{paused ? 'Paused' : 'On'}</Badge>}
              onClick={() => (paused ? setKill(false) : setAskPause(true))} />
            <ListRow icon={<IconPulse />} title="System" subtitle={`API ${health.status} · database ${health.db ?? '–'}`}
              value={<Badge tone={health.status === 'ok' ? 'ok' : 'danger'} pop={false}>{health.status === 'ok' ? 'OK' : 'Down'}</Badge>} />
            <ListRow icon={<IconSparkle />} title="Claude" subtitle={health.brain_busy ? 'Working' : 'Ready'}
              value={<Badge tone={health.brain === 'ok' ? 'ok' : 'warn'} pop={false}>{health.brain === 'ok' ? 'OK' : (health.brain ?? '–')}</Badge>} />
            <ListRow icon={<IconShield />} title="Last boot check" chevron
              subtitle={boot ? new Date(boot.time).toLocaleString() : 'Not run yet'}
              value={boot ? <Badge tone={boot.all_ok ? 'ok' : 'danger'} pop={false}>{boot.all_ok ? 'OK' : 'Failed'}</Badge> : undefined}
              onClick={() => stack.push('Boot check', () => <BootCheckPage check={boot} />)} />
          </List>

          <SectionTitle>Accounts</SectionTitle>
          <List>
            <ListRow icon={<IconMail />} title="Google" chevron subtitle={google?.signed_out ? 'Signed out' : google?.read ?? 'Not connected'}
              value={<Badge tone={googleOk ? 'ok' : 'warn'} pop={false}>{googleOk ? 'OK' : 'Fix'}</Badge>}
              onClick={() => stack.push('Google', () => <GooglePage st={google} />)} />
            <ListRow icon={<IconBell />} title="Notifications"
              subtitle={perm === 'unsupported' ? 'Open Kairo from the Home Screen first' : perm === 'granted' ? 'On · tap to reconnect' : 'Tap to turn on'}
              onClick={perm === 'unsupported' ? undefined : enablePush} />
          </List>

          <SectionTitle>Work</SectionTitle>
          <List>
            <ListRow icon={<IconSparkle />} title="School" subtitle="Dates, grades, flashcards, study guides" chevron
              onClick={() => { window.location.hash = '#school' }} />
            <ListRow icon={<IconList />} title="Lerndokumentation" subtitle="Weekly entry, German draft by Kairo" chevron
              onClick={() => { window.location.hash = '#lerndoku' }} />
            <ListRow icon={<IconRefresh />} title="Reviews" subtitle="Check-ins and weekly to yearly reviews" chevron
              onClick={() => { window.location.hash = '#reviews' }} />
            <ListRow icon={<IconRefresh />} title="Jobs" subtitle="Brief, email triage, captures, reviews" chevron
              onClick={() => stack.push('Jobs', () => <JobsPage />)} />
            <ListRow icon={<IconInbox />} title="Capture" subtitle="Action button and share sheet setup" chevron
              onClick={() => stack.push('Capture', () => <CapturePage />)} />
            <ListRow icon={<IconSparkle />} title="Voice" subtitle="Talk to Kairo, calls, test" chevron
              onClick={() => stack.push('Voice', () => <VoicePage />)} />
            <ListRow icon={<IconPin />} title="Location" subtitle="Places, history, pause" chevron
              onClick={() => stack.push('Location', () => <LocationPage />)} />
          </List>

          <SectionTitle>History</SectionTitle>
          <List>
            <ListRow icon={<IconList />} title="Activity" subtitle="Everything Kairo did, newest first" chevron
              onClick={() => stack.push('Activity', () => <ActivityPage />)} />
            <ListRow icon={<IconShield />} title="Backups" subtitle="Nightly backup and weekly restore test" chevron
              onClick={() => stack.push('Backups', () => <BackupsPage />)} />
            {TODOS.filter((t) => todos[t.id]).map((t) => (
              <ListRow key={t.id} icon={<IconCheck />} title={t.title} subtitle="Done" chevron
                onClick={() => stack.push(t.title, () => <TodoPage todo={t} done onToggle={() => toggleTodo(t.id)} />)} />
            ))}
          </List>

          <SectionTitle>Developer</SectionTitle>
          <List>
            <ListRow icon={<IconKey />} title="Components" subtitle="Every component and animation" chevron
              onClick={() => { window.location.hash = '#/dev/components' }} />
          </List>
        </>
      )}

      <Sheet open={askPause} onClose={() => setAskPause(false)} title="Pause Kairo?">
        <p className="k-small" style={{ textAlign: 'center', margin: '0 0 20px' }}>It stops what it is doing and does nothing until you resume.</p>
        <Button variant="danger" block onClick={() => { setAskPause(false); setKill(true) }}>Pause all</Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setAskPause(false)}>Cancel</Button>
      </Sheet>
    </Page>
  )
}

export default function More() {
  return <Stack root={<Root />} rootTitle="More" />
}
