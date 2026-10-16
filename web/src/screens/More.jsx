import { useCallback, useEffect, useState } from 'react'
import { api, parseTs } from '../api.js'
import { clientLog, device } from '../clientlog.js'
import { holdMic, playLastAgain, releaseMic, setReleaseWhileSpeaking, speak, unlockAudio } from '../voice.js'
import {
  Badge, Button, Card, EmptyState, IconBell, IconBolt, IconChart, IconCheck, IconGear, IconHeart, IconInbox, IconKey, IconList, IconMail, IconMic, IconPower,
  IconPulse, IconRefresh, IconPin, IconShield, IconSparkle, IconSpeaker, IconWarn, List, ListRow, Page, SectionTitle, Segmented, Sheet, SkeletonCard, Stack,
  useStack, useToast,
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
  const base = url.replace(/\/api\/capture$/, '')
  const rotate = async () => {
    const r = await api('/api/capture/token/rotate', { method: 'POST', body: {} })
    setSetup((s) => ({ ...s, token: r.token }))
    setConfirm(false)
    toast.show({ text: 'New key. Put it into your Shortcuts.' })
  }
  return (
    <Page title="Capture">
      <p className="k-hello">Send anything to Kairo in a second, from anywhere: say it, share it, done.</p>
      <SectionTitle>Address</SectionTitle>
      <div className="k-pad"><div className="k-code" onClick={() => copy(url, toast)}>{url}</div></div>
      <SectionTitle>Key</SectionTitle>
      <div className="k-pad">
        <div className="k-code" onClick={() => setup?.token && copy(setup.token, toast)}>{setup?.token ?? '…'}</div>
        <p className="k-muted k-small">Tap to copy. Only for your Shortcuts. If it ever leaks: </p>
        <Button variant="secondary" size="sm" onClick={() => setConfirm(true)}>Make a new key</Button>
      </div>

      <SectionTitle>Shortcut 1: Voice note (say it, done)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>Open the <b>Shortcuts</b> app → <b>+</b> → name it <b>Kairo Voice</b>.</li>
          <li>Add <b>Record Audio</b>. Tap the arrow: Start Recording <b>Immediately</b>, Finish Recording <b>On Tap</b>.</li>
          <li>Add <b>Get Contents of URL</b>. URL: <span className="k-code" style={{ display: 'inline' }} onClick={() => copy(`${base}/api/voice-note`, toast)}>{base}/api/voice-note</span> Method <b>POST</b>.</li>
          <li>Headers → Add: key <b>Authorization</b>, value <b>Bearer</b> + space + the key above.</li>
          <li>Request Body: <b>File</b> → <b>Recorded Audio</b>.</li>
          <li>Add <b>Show Notification</b>: “Kairo got it”.</li>
        </ol>
        <p className="k-muted k-small">Talk, tap stop, put the phone away. Kairo writes it down on the PC and does it: tasks, notes, reminders, your own calendar. Anything for other people waits as a draft in the app. You see the answer next time you open Kairo; a push only if it is urgent.</p>
      </div>

      <SectionTitle>Lock screen and back of the phone</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li><b>Lock screen</b> (iOS 18): on the lock screen, touch and hold → <b>Customize</b> → <b>Lock Screen</b>.</li>
          <li>Tap the <b>−</b> on the flashlight button, then the empty <b>+</b> → search <b>Shortcut</b> → pick <b>Kairo Voice</b> → <b>Done</b>.</li>
          <li><b>Back Tap</b>: iPhone Settings → <b>Accessibility</b> → <b>Touch</b> → <b>Back Tap</b> → <b>Double Tap</b> → scroll down → <b>Kairo Voice</b>.</li>
          <li>Optional: <b>Triple Tap</b> → <b>Call Kairo</b> (below).</li>
        </ol>
      </div>

      <SectionTitle>Shortcut 2: Call Kairo (talk live)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>New shortcut <b>Call Kairo</b>. Add <b>Open URLs</b>: <span className="k-code" style={{ display: 'inline' }} onClick={() => copy(`${base}/#call/new`, toast)}>{base}/#call/new</span></li>
          <li>It opens in Safari with the mic on at once. Allow the mic once: iPhone Settings → <b>Apps</b> → <b>Safari</b> → <b>Microphone</b> → <b>Allow</b>.</li>
          <li>To hear Kairo, tap the screen once (iPhone rule: sound only after a tap).</li>
        </ol>
        <p className="k-muted k-small">You can say something and hang up straight away. Kairo handles it like a voice note.</p>
      </div>

      <SectionTitle>Shortcut 3: Share to Kairo (text, links, photos, any file)</SectionTitle>
      <div className="k-pad">
        <ol className="k-steps">
          <li>Shortcuts app → <b>+</b> → name it <b>Share to Kairo</b>. Tap <b>(i)</b> → turn on <b>Show in Share Sheet</b>.</li>
          <li>At the top it says “Receive <b>Any</b> input from Share Sheet”: leave <b>Any</b>. Under it, “If there's no input”: pick <b>Ask For Text</b> (then you can also just type).</li>
          <li>Add <b>Get Type</b> (it uses Shortcut Input).</li>
          <li>Add <b>If</b> → condition: <b>Type is Text</b>. Tap <b>Add Condition</b> → <b>Type is URL</b>. Switch the If from “All” to <b>Any</b>.</li>
          <li>Inside the If: add <b>Get Contents of URL</b>. URL: <span className="k-code" style={{ display: 'inline' }} onClick={() => copy(`${url}?source=share&wait=1`, toast)}>{url}?source=share&amp;wait=1</span> Method <b>POST</b>. Header <b>Authorization</b>: <b>Bearer</b> + space + the key above. Request Body: <b>JSON</b> → key <b>text</b>, value <b>Shortcut Input</b>.</li>
          <li>Under <b>Otherwise</b>: add <b>Get Contents of URL</b> with the same URL, <b>POST</b> and Authorization header, plus a second header: key <b>X-Kairo-Filename</b>, value <b>Shortcut Input</b> → tap it → <b>Name</b>. Request Body: <b>File</b> → <b>Shortcut Input</b>.</li>
          <li>After <b>End If</b>: add <b>Get Dictionary Value</b> → key <b>message</b> from <b>If Result</b>, then <b>Show Notification</b> → <b>Dictionary Value</b>.</li>
          <li>So Kairo can read your photos: iPhone Settings → <b>Camera</b> → <b>Formats</b> → <b>Most Compatible</b>.</li>
        </ol>
        <p className="k-muted k-small">WhatsApp, Safari, Photos, Vinted, Files: share → <b>Share to Kairo</b>. No app opens; the notification says where it went. Kairo never reads WhatsApp itself: only what you share.</p>
      </div>

      <SectionTitle>What happens</SectionTitle>
      <div className="k-pad">
        <p className="k-small">Kairo files it at once and tells you in one line. Files stay only on the PC, in your vault under Files/School, Work, Personal, Money, Receipts… with their own name and a short summary next to them; the same file is kept once, and Files/ never syncs to the iPhone. Bills and appointments also become tasks. Something someone would like goes on that person's wishlist with the price and link. If Kairo isn't sure (who it's from, what it is), it asks you with its best guess the next time you talk to it. It never sends, deletes or pays anything because of a capture.</p>
      </div>

      <Sheet open={confirm} onClose={() => setConfirm(false)} title="New key?">
        <p className="k-small" style={{ textAlign: 'center', margin: '0 0 20px' }}>The old key stops working. You have to paste the new one into every Kairo Shortcut.</p>
        <Button variant="danger" block onClick={rotate}>Make a new key</Button>
        <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setConfirm(false)}>Cancel</Button>
      </Sheet>
    </Page>
  )
}

function UsagePage() {
  const [days, setDays] = useState(7)
  const [u, setU] = useState(null)
  useEffect(() => { api(`/api/usage?days=${days}`).then(setU).catch(() => setU(null)) }, [days])
  const row = ([k, v]) => (
    <ListRow key={k} title={k} wrap subtitle={`${v.runs} runs · ${v.minutes} min · ${(v.tokens / 1000).toFixed(0)}k tokens${v.failed ? ` · ${v.failed} failed` : ''}`}
      value={<span className="k-muted k-small">${v.usd.toFixed(2)}</span>} />
  )
  return (
    <Page title="Usage">
      <div className="k-pad" style={{ marginBottom: 8 }}>
        <Segmented id="usagedays" value={days} onChange={setDays} options={[{ value: 1, label: 'Today' }, { value: 7, label: '7 days' }, { value: 30, label: '30 days' }]} />
      </div>
      {!u && <SkeletonCard />}
      {u?.realtime && <RealtimeSpend b={u.realtime} />}
      {u && (
        <>
          <SectionTitle>By model</SectionTitle>
          <List>{['haiku', 'sonnet', 'opus'].filter((m) => u.by_model[m]).map((m) => row([m[0].toUpperCase() + m.slice(1), u.by_model[m]]))}
            {u.total_runs === 0 && <ListRow title="No runs yet" />}</List>
          <SectionTitle>By job</SectionTitle>
          <List>{Object.entries(u.by_purpose).filter(([k]) => !k.startsWith('voice_realtime')).map(row)}</List>
          <p className="k-muted k-small k-pad">$ is the list-price equivalent; your Claude subscription covers it (realtime voice above is real money). Cheapest model per job: Haiku for sorting and short replies, Sonnet for writing, Opus only for big planning and long reviews.</p>
        </>
      )}
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
                value={c.ok && c.level === 'warn' ? <Badge tone="warn" pop={false}>To do</Badge>
                  : <Badge tone={c.ok ? 'ok' : 'danger'} pop={false}>{c.ok ? 'OK' : 'Failed'}</Badge>} />
            ))}
          </List>
        </>
      )}
    </Page>
  )
}

/* The self-test (fix P0): the owner-chat path adding a real task, context, voice and browser, with proof. */
function SelfTestPage({ st }) {
  if (!st) return <Page title="Self-test" tabbar><EmptyState icon={<IconShield />} title="No self-test yet" text="It runs after every update and at every boot." /></Page>
  const c = st.checks || {}
  const rows = [
    ['Safety hook', c.hook_deny && c.hook_allow, c.hook_deny ? 'blocks what it must, lets Kairo tools through' : 'broken'],
    ['Tool server', c.mcp === 'connected', c.mcp],
    ['All tools reach the model', (c.tools_missing || []).length === 0, (c.tools_missing || []).length ? `missing: ${c.tools_missing.join(', ')}` : 'all listed'],
    ['Adds a task like a chat message', c.task_added, st.proof?.task ? `"${st.proof.task.title}", due ${new Date(st.proof.task.due).toLocaleString()} (removed again)` : 'no task'],
    ['Profile and Goals in the prompt', c.context === 'empty' ? 'warn' : (c.profile && c.goals),
      c.context === 'empty' ? 'No context yet: do the onboarding (Today → Get to know you)' : (c.profile && c.goals ? 'yes' : 'notes exist but do not reach the model')],
    ['Personality is current', c.personality !== 'older', c.personality],
    ['Claude', c.claude === 'ok', c.claude],
    ...(c.voice === null || c.voice === undefined ? [] : [['Voice: real transcription', c.voice, st.proof?.transcription ? `heard "${st.proof.transcription}"` : (st.details?.voice?.reason || '')]]),
    ...(c.browser === null || c.browser === undefined ? [] : [['Browser', c.browser, st.details?.browser?.reason || 'renders a page']]),
  ]
  return (
    <Page title="Self-test" tabbar>
      <p className="k-hello">{st.ok ? 'Everything works the way your messages use it.' : `Failed: ${st.reason}`} {new Date(st.at).toLocaleString()} ({st.seconds}s)</p>
      <List>
        {rows.map(([name, ok, detail]) => (
          <ListRow key={name} title={name} subtitle={String(detail ?? '')} wrap
            value={ok === 'warn' ? <Badge tone="warn" pop={false}>To do</Badge>
              : <Badge tone={ok ? 'ok' : 'danger'} pop={false}>{ok ? 'OK' : 'Failed'}</Badge>} />
        ))}
      </List>
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

/* Run diagnosis: the numbers the fix plan needs, from the phone. Read-only on the PC; secrets redacted there. */
function DiagnosisPage() {
  const toast = useToast()
  const [state, setState] = useState({ busy: false, text: null, error: null })
  const run = async () => {
    setState({ busy: true, text: null, error: null })
    try {
      const r = await api('/api/diagnosis', { method: 'POST', body: {} })
      setState({ busy: false, text: r.text, error: null })
    } catch (e) {
      setState({ busy: false, text: null, error: e.message })
    }
  }
  const share = async () => {
    const name = `kairo-diagnosis-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.txt`
    const file = typeof File === 'function' ? new File([state.text], name, { type: 'text/plain' }) : null
    try {
      if (file && navigator.canShare?.({ files: [file] })) await navigator.share({ files: [file], title: 'Kairo diagnosis' })
      else if (navigator.share) await navigator.share({ title: 'Kairo diagnosis', text: state.text })
      else copy(state.text, toast)
    } catch (e) {
      if (e?.name !== 'AbortError') copy(state.text, toast)
    }
  }
  return (
    <Page title="Diagnosis">
      <p className="k-hello">
        Counts and timings from the last 14 days, the self-test, the boot check and the log. It only reads: nothing changes.
        Passwords, tokens and keys are removed, addresses shortened.
      </p>
      <div className="k-pad">
        <Button variant="primary" block loading={state.busy} disabled={state.busy} onClick={run}>
          {state.text ? 'Run again' : 'Run diagnosis'}
        </Button>
        {state.error && <p className="k-small" style={{ color: 'var(--danger)', marginTop: 12 }}>{state.error}</p>}
      </div>
      {state.text && (
        <>
          <div className="k-pad" style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button block onClick={() => copy(state.text, toast)}>Copy</Button>
            <Button block onClick={share}>Share</Button>
          </div>
          <div className="k-pad"><pre className="k-pre selectable" style={{ fontSize: 11 }}>{state.text}</pre></div>
        </>
      )}
    </Page>
  )
}

/* Sound check (fix A1): proves on this iPhone which way Kairo's voice is audible, and sets calls up
   to match. Every answer goes to the PC log, so the real cause is on record. */
function SoundCheckPage() {
  const [state, setState] = useState({ step: null, busy: false, heard: {}, needTap: false })
  const play = async (step) => {
    unlockAudio()                       // inside the tap
    setState((s) => ({ ...s, step, busy: true, needTap: false }))
    try {
      if (step === 'speaker') { releaseMic(); await speak('This is Kairo. Sound check, through the speaker.') }
      else { await holdMic(); unlockAudio(); await speak('Sound check two. This is how I sound on a call.', { call: true, keepMic: true }) }
      setState((s) => ({ ...s, busy: false }))
    } catch (e) {
      setState((s) => ({ ...s, busy: false, needTap: e.name === 'NeedTap', error: e.name === 'NeedTap' ? null : e.message }))
    }
  }
  const answer = (step, heard) => {
    clientLog('audio.check', { step, heard, ...device() })
    if (step === 'call') { setReleaseWhileSpeaking(!heard); releaseMic() }
    setState((s) => ({ ...s, step: null, heard: { ...s.heard, [step]: heard } }))
  }
  const h = state.heard
  return (
    <Page title="Sound check">
      <p className="k-hello">Two short tests, 30 seconds. Volume up. Do it once with the silent switch on and once with AirPods.</p>
      <List>
        <ListRow icon={<IconSpeaker />} title="1. Through the speaker" subtitle="Mic off, like a chat reply"
          value={h.speaker === undefined ? undefined : <Badge tone={h.speaker ? 'ok' : 'danger'} pop={false}>{h.speaker ? 'Heard' : 'Silent'}</Badge>}
          onClick={() => !state.busy && play('speaker')} />
        <ListRow icon={<IconMic />} title="2. Like a call" subtitle="Mic on while Kairo talks"
          value={h.call === undefined ? undefined : <Badge tone={h.call ? 'ok' : 'warn'} pop={false}>{h.call ? 'Heard' : 'Silent'}</Badge>}
          onClick={() => !state.busy && play('call')} />
      </List>
      {state.needTap && (
        <div className="k-pad"><Button variant="primary" block onClick={() => { setState((s) => ({ ...s, needTap: false })); playLastAgain().catch(() => {}) }}>Tap to play</Button></div>
      )}
      {state.step && !state.busy && (
        <div className="k-pad" style={{ display: 'flex', gap: 8 }}>
          <Button block onClick={() => answer(state.step, true)}>I heard it</Button>
          <Button block variant="danger" onClick={() => answer(state.step, false)}>Nothing</Button>
        </div>
      )}
      {state.error && <p className="k-small k-pad" style={{ color: 'var(--danger)' }}>{state.error}</p>}
      <p className="k-muted k-small k-pad" style={{ marginTop: 12 }}>
        {h.call === false ? 'Calls on this phone: the mic pauses while Kairo talks. Tap the orb to interrupt.'
          : h.call ? 'Calls on this phone: Kairo keeps listening while it talks, so you can talk over it.'
            : 'If test 1 is silent too, the phone itself is muted for media or connected to another speaker.'}
      </p>
    </Page>
  )
}

/* Acceptance (KICKOFF v8): done = proven on this PC and this iPhone. The PC runs the automated tests
   itself after every update; the manual ones are yours to tap through (max 5 minutes). */
const KIND_LABEL = { auto: 'Automated on the PC', manual: 'You, on the iPhone', ci: 'Proven in CI', later: 'Comes with a later block' }

function AcceptancePage() {
  const toast = useToast()
  const [rep, setRep] = useState(null)
  const load = useCallback(() => api('/api/acceptance').then(setRep).catch(() => setRep({ tests: [] })), [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!rep?.running) return undefined
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [rep?.running, load])
  const runAll = async () => {
    const r = await api('/api/acceptance/run', { method: 'POST', body: {} })
    toast.show({ text: r.started ? 'Running on the PC with the real Claude. A few minutes.' : 'Already running.' })
    load()
  }
  const mark = async (id, ok) => { setRep(await api(`/api/acceptance/${id}/manual`, { method: 'POST', body: { ok } })) }
  const badge = (t) => {
    const r = t.result
    if (t.kind === 'later') return <Badge tone="neutral" pop={false}>{t.block}</Badge>
    if (t.kind === 'ci') return <Badge tone="neutral" pop={false}>CI</Badge>
    if (!r) return <Badge tone="neutral" pop={false}>Not run</Badge>
    return <Badge tone={r.ok ? (t.stale ? 'warn' : 'ok') : 'danger'} pop={false}>{r.ok ? (t.stale ? 'Old pass' : 'Pass') : 'Fail'}</Badge>
  }
  const sub = (t) => {
    const r = t.result
    if (!r || t.kind === 'later') return t.how
    const failed = Object.entries(r.checks || {}).filter(([, v]) => !v).map(([k]) => k)
    return [failed.length ? `Failed: ${failed.join(', ')}` : null, r.detail, r.at && new Date(r.at).toLocaleString()].filter(Boolean).join(' · ')
  }
  const s = rep?.summary
  return (
    <Page title="Acceptance" onRefresh={load}>
      {!rep && <SkeletonCard lines={4} />}
      {rep && (
        <>
          <p className="k-hello">
            {s ? `Automated: ${s.auto_passed}/${s.auto} pass. Yours: ${s.manual_passed}/${s.manual}.` : ''} Done means it passes here, on this PC and this iPhone.
          </p>
          <div className="k-pad">
            <Button variant="primary" block loading={rep.running} disabled={rep.running} onClick={runAll}>
              {rep.running ? 'Running on the PC…' : 'Run the automated tests'}
            </Button>
          </div>
          {['auto', 'manual', 'ci', 'later'].map((kind) => {
            const tests = rep.tests.filter((t) => t.kind === kind)
            if (!tests.length) return null
            return (
              <div key={kind}>
                <SectionTitle>{KIND_LABEL[kind]}</SectionTitle>
                <List>
                  {tests.map((t) => (
                    <div key={t.id}>
                      <ListRow title={`${t.id}. ${t.name}`} subtitle={sub(t)} wrap value={badge(t)} />
                      {kind === 'manual' && (
                        <div className="k-pad" style={{ display: 'flex', gap: 8, paddingTop: 0 }}>
                          <Button size="sm" block onClick={() => mark(t.id, true)}>Pass</Button>
                          <Button size="sm" block variant="danger" onClick={() => mark(t.id, false)}>Fail</Button>
                        </div>
                      )}
                    </div>
                  ))}
                </List>
              </div>
            )
          })}
        </>
      )}
    </Page>
  )
}

/* Fix plan 33: a note changed on two devices at once. Pick this PC's version, the other one, or Kairo's
   merge (shown first, editable, written only on "Use this"). The replaced version is kept on the PC. */
function ConflictsPage() {
  const toast = useToast()
  const [list, setList] = useState(null)
  const [merged, setMerged] = useState({})
  const [busy, setBusy] = useState('')
  const load = useCallback(() => api('/api/vault/conflicts').then((d) => setList(d.conflicts)).catch(() => setList([])), [])
  useEffect(() => { load() }, [load])
  const resolve = async (c, keep, text) => {
    setBusy(c.path)
    try {
      await api('/api/vault/conflicts/resolve', { method: 'POST', body: { path: c.path, keep, text } })
      toast.show({ text: 'Done. The other version is kept on the PC, just in case.' })
      await load()
    } catch (e) { toast.show({ text: e.message }) } finally { setBusy('') }
  }
  const merge = async (c) => {
    setBusy(c.path)
    try {
      const r = await api('/api/vault/conflicts/merge', { method: 'POST', body: { path: c.path } })
      setMerged((m) => ({ ...m, [c.path]: r.text }))
    } catch (e) { toast.show({ text: e.message }) } finally { setBusy('') }
  }
  const when = (t) => (t ? new Date(t).toLocaleString() : 'missing')
  return (
    <Page title="Sync conflicts" onRefresh={load}>
      <p className="k-hello">These notes were changed on two devices at the same time. Kairo doesn’t read either copy into a chat until you pick one.</p>
      {list === null && <SkeletonCard lines={3} />}
      {list?.length === 0 && <EmptyState title="Nothing to sort out" text="Every note has one version." />}
      {list?.map((c) => (
        <Card key={c.path}>
          <p className="k-eyebrow">{c.original}</p>
          <p className="k-small k-muted">This PC · {when(c.pc_at)}</p>
          <pre className="k-approval-preview">{c.pc_text || '(empty)'}</pre>
          <p className="k-small k-muted">Other device · {when(c.other_at)}</p>
          <pre className="k-approval-preview">{c.other_text || '(empty)'}</pre>
          {merged[c.path] !== undefined ? (
            <>
              <p className="k-eyebrow">Kairo’s merge (you can edit it)</p>
              <div className="k-field">
                <textarea rows={10} value={merged[c.path]} onChange={(e) => setMerged((m) => ({ ...m, [c.path]: e.target.value }))} />
              </div>
              <Button block disabled={busy === c.path || !merged[c.path].trim()} onClick={() => resolve(c, 'merged', merged[c.path])}>Use this</Button>
            </>
          ) : (
            <Button variant="secondary" block disabled={busy === c.path} onClick={() => merge(c)}>{busy === c.path ? 'Merging…' : 'Merge both with Kairo'}</Button>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button variant="secondary" block disabled={busy === c.path} onClick={() => resolve(c, 'pc')}>Keep this PC’s</Button>
            <Button variant="secondary" block disabled={busy === c.path} onClick={() => resolve(c, 'other')}>Keep the other</Button>
          </div>
        </Card>
      ))}
    </Page>
  )
}

const secs = (ms) => (ms == null ? '–' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`)

// Every voice and chat turn, step by step (owner's request 2026-10-15; server/app/turns.py):
// p50 / p95 per step for this version and the one before, the targets, and the last turns.
const NET = { direct: 'Direct', relay: 'Through Tailscale’s relay', unknown: 'Not known' }

function VoiceSpeedPage() {
  const [rep, setRep] = useState(null)
  const [net, setNet] = useState(null)
  const load = useCallback(() => {
    api('/api/voice/turns?days=7').then(setRep).catch(() => setRep({ versions: [], recent: [] }))
    api('/api/rtc?network=1').then(setNet).catch(() => setNet(null))
  }, [])
  useEffect(() => { load() }, [load])
  if (!rep) return <Page title="Voice speed"><SkeletonCard lines={6} /></Page>
  const [now, before] = rep.versions
  const pathsOf = (v) => Object.entries(v?.paths || {})
  return (
    <Page title="Voice speed" onRefresh={load}>
      {!now ? <p className="k-muted k-small k-pad">No turns measured yet: talk to Kairo (Chat or a call) and come back.</p> : null}
      {pathsOf(now).map(([path, p]) => {
        const tgt = rep.targets?.[path] || {}
        const tot = p.steps.total_ms
        const ok = tot && tgt.p50 && tot.p50 <= tgt.p50 && (!tgt.p95 || tot.p95 <= tgt.p95)
        return (
          <div key={path}>
            <SectionTitle>{p.title} · {p.n} turns · played {p.played}/{p.n}</SectionTitle>
            <List>
              {Object.entries(p.steps).map(([k, s]) => {
                const old = before?.paths?.[path]?.steps?.[k]
                return (
                  <ListRow key={k} title={s.title}
                    subtitle={`p50 ${secs(s.p50)} · p95 ${secs(s.p95)}${old ? ` (before: ${secs(old.p50)} · ${secs(old.p95)})` : ''}`}
                    value={k === 'total_ms' && tgt.p50 ? <Badge tone={ok ? 'ok' : 'warn'} pop={false}>{ok ? 'On target' : `Target ${secs(tgt.p50)}${tgt.p95 ? ` / ${secs(tgt.p95)}` : ''}`}</Badge> : undefined} />
                )
              })}
              {Object.entries(p.failures || {}).map(([why, n]) => (
                <ListRow key={why} icon={<IconWarn />} title={`Not played: ${why}`} value={<Badge tone="warn" pop={false}>{n}</Badge>} />
              ))}
            </List>
          </div>
        )
      })}
      {before && now && <p className="k-muted k-small k-pad">Now = version {now.version}. Before = version {before.version}.</p>}
      {/* Voice V3: how this phone reaches the PC, and the network over the last calls */}
      <SectionTitle>Network</SectionTitle>
      <List>
        <ListRow icon={<IconPulse />} title={`This phone: ${NET[net?.network?.net] || NET.unknown}${net?.network?.via ? ` (${net.network.via})` : ''}${net?.network?.lan ? ' · same Wi-Fi' : ''}`}
          subtitle={net?.available ? `Calls over WebRTC${net?.network?.age_s != null ? ` · checked ${net.network.age_s} s ago` : ''}` : `Calls on the old line${net?.reason ? ` (${net.reason})` : ''}`}
          value={net?.network?.net === 'relay' ? <Badge tone="warn" pop={false}>Relayed</Badge> : net?.network?.net === 'direct' ? <Badge tone="ok" pop={false}>Direct</Badge> : undefined} />
        {Object.entries(rep.network || {}).map(([k, v]) => (
          <ListRow key={k} title={`Calls ${NET[k] ? NET[k].toLowerCase() : k}: ${v.turns} turns`} subtitle={`Round trip p50 ${v.rtt_p50 != null ? `${v.rtt_p50} ms` : '–'}`} />
        ))}
      </List>
      {rep.slow?.length > 0 && (
        <>
          <SectionTitle>Over 1 s · slowest step</SectionTitle>
          <List>
            {Object.entries(rep.slow_causes || {}).map(([k, n]) => (
              <ListRow key={k} icon={<IconWarn />} title={rep.titles?.[k] || k} value={<Badge tone="warn" pop={false}>{n}</Badge>} />
            ))}
            {rep.slow.slice(0, 8).map((x, i) => (
              <ListRow key={`s${i}`} title={`${new Date(parseTs(x.ts)).toLocaleTimeString()} · ${secs(x.total_ms)}`}
                subtitle={`${x.slowest_title || '?'} ${secs(x.slowest_ms)} · ${x.line === 'webrtc' ? 'WebRTC' : 'old line'}${x.net ? ` · ${NET[x.net] || x.net}` : ''}`} />
            ))}
          </List>
        </>
      )}
      {rep.recent.length > 0 && (
        <>
          <SectionTitle>Last turns</SectionTitle>
          <List>
            {rep.recent.map((r, i) => (
              <ListRow key={i} title={`${new Date(parseTs(r.ts)).toLocaleTimeString()} · ${r.path.replace('_', ' ')} · ${r.model || '?'}`}
                subtitle={r.line === 'webrtc'
                  ? `${r.total_ms != null ? secs(r.total_ms) : 'no audio'}${r.reason ? ` · ${r.reason}` : ''} · turn ${secs(r.steps.endpoint_ms)} · first word ${secs(r.steps.llm_ms)} · voice ${secs(r.steps.tts_ms)} · network ${secs(r.steps.net_ms)}${r.net ? ` (${r.net})` : ''}`
                  : `${r.total_ms != null ? secs(r.total_ms) : 'no audio'}${r.reason ? ` · ${r.reason}` : ''} · STT ${secs(r.steps.stt_ms)} · first words ${secs(r.steps.first_words_ms)} · voice ${secs(r.steps.tts_ms)}`}
                value={<Badge tone={['played', 'text'].includes(r.outcome) ? 'ok' : 'warn'} pop={false}>{r.outcome}</Badge>} />
            ))}
          </List>
        </>
      )}
    </Page>
  )
}

// The voice lab (owner's request 2026-10-16): the voice candidates measured on the PC, and the pick.
const pctTxt = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`)
const msTxt = (s) => (s?.p50 == null ? '–' : `${s.p50} / ${s.p95 ?? '–'} ms`)

function VoiceLabPage() {
  const toast = useToast()
  const [d, setD] = useState(null)
  const load = useCallback(() => api('/api/voice/lab').then(setD).catch(() => setD(null)), [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!d?.state?.running) return undefined
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [d?.state?.running, load])
  const run = async (language) => {
    try { await api('/api/voice/lab/run', { method: 'POST', body: { language } }); toast.show({ text: 'Voice lab started: 10–20 minutes. Calls use the old voice meanwhile.', duration: 5000 }); setTimeout(load, 1000) } catch (e) { toast.show({ text: e.message, duration: 6000 }) }
  }
  const choose = async (model) => { try { setD(await api('/api/voice/lab/choose', { method: 'POST', body: { model } })); toast.show({ text: 'Switched. It loads in a few seconds.' }) } catch (e) { toast.show({ text: e.message }) } }
  const kyutai = async () => { await api('/api/voice/lab/kyutai', { method: 'POST', body: {} }); toast.show({ text: 'Installed by the PC within the next minutes (about 10 GB). Then run the lab again.', duration: 6000 }); load() }
  if (!d) return <Page title="Voice lab"><SkeletonCard lines={6} /></Page>
  const r = d.last || {}
  const models = Object.entries(r.models || {})
  const vm = d.voice_model || {}
  const sp = r.speech || {}
  const k = r.kyutai || {}
  const live = d.live || {}
  const card = d.card || { sections: {}, titles: {} }
  return (
    <Page title="Voice lab" onRefresh={load}>
      <p className="k-hello">The voice candidates, measured on your PC’s graphics card: how fast they answer, whether they hand real work to Claude every time, and how natural they sound. Kairo picks the fastest one that passes.</p>
      <List>
        <ListRow icon={<IconMic />} title={vm.state === 'ready' ? `Talking: ${vm.model}` : `Voice model: ${vm.state || '?'}`}
          subtitle={vm.error || (d.installed.length ? `Installed: ${d.installed.join(', ')}` : 'Not installed yet: the next update downloads them')}
          value={<Badge tone={vm.state === 'ready' ? 'ok' : 'warn'} pop={false}>{d.engine === 'local' ? 'Local' : 'OpenAI'}</Badge>} />
        {d.gpu && <ListRow icon={<IconPulse />} title={d.gpu.name} subtitle={`${d.gpu.used_mb} of ${d.gpu.total_mb} MB in use · driver ${d.gpu.driver}`} />}
        <ListRow icon={<IconBolt />} title={d.state?.running ? `Running: ${d.state.step}` : 'Run the voice lab'}
          subtitle={d.state?.running ? 'Calls use the old voice until it is done' : 'English and German, about 10–20 minutes'}
          onClick={d.state?.running || !d.installed.length ? undefined : () => run('auto')} />
        {!d.state?.running && d.installed.length > 0 && (
          <ListRow icon={<IconBolt />} title="Run it English-only" subtitle="If you only talk English on calls" onClick={() => run('en')} />
        )}
      </List>
      <SectionTitle>Your calls, last 7 days</SectionTitle>
      <List>
        <ListRow title="Answered on the PC (Today card)" subtitle={live.questions ? `${live.card} of ${live.questions} questions · ${live.talk} small talk` : 'No call questions yet'}
          value={<Badge tone="ok" pop={false}>{live.on_pc_pct ?? '–'}%</Badge>} />
        <ListRow title="Went to Claude" subtitle="Everything the card can’t answer" value={<span className="k-small">{live.via_claude_pct ?? '–'}%</span>} />
      </List>
      <p className="k-muted k-small k-pad">“Question” is read from what you said, so this split is an estimate; each model’s exact numbers on fixed questions are below.</p>
      {r.pick && (
        <p className="k-small k-pad selectable"><b>{r.pick.model ? `Picked: ${r.pick.model}` : 'Nothing picked'}</b> — {r.pick.why}{r.at ? ` (${parseTs(r.at).toLocaleString()}, ${r.seconds}s)` : ''}</p>
      )}
      {r.error && <p className="k-small k-pad" style={{ color: 'var(--warn)' }}>{r.error}</p>}
      {models.map(([id, m]) => (
        <div key={id}>
          <SectionTitle>{m.title || id}{m.quant ? ` · ${m.quant}` : ''}{d.active === id ? ' · talking' : ''}</SectionTitle>
          {m.error ? <p className="k-small k-pad" style={{ color: 'var(--warn)' }}>{m.error}</p> : (
            <List>
              <ListRow title="First word (p50 / p95)" value={<span className="k-small">{msTxt(m.first_word_ms)}</span>} />
              <ListRow title="First token (p50 / p95)" subtitle={`cold start: ${Object.entries(m.cold_first_token_ms || {}).map(([l, v]) => `${l} ${v} ms`).join(', ')}`} value={<span className="k-small">{msTxt(m.first_token_ms)}</span>} />
              <ListRow title="Speed" subtitle={`load ${m.load_s ?? '–'} s · VRAM ${m.vram_mb ?? '–'} MB`} value={<span className="k-small">{m.tokens_per_s ?? '–'} tok/s</span>} />
              <ListRow title="Calls ask_kairo when needed" value={<Badge tone={(m.tool_recall ?? 0) >= 0.9 ? 'ok' : 'warn'} pop={false}>{pctTxt(m.tool_recall)}</Badge>} />
              <ListRow title="Calls it when not needed" value={<Badge tone={(m.false_calls ?? 1) <= 0.15 ? 'ok' : 'warn'} pop={false}>{pctTxt(m.false_calls)}</Badge>} />
              <ListRow title="Keeps Claude’s facts" value={<Badge tone={(m.fidelity ?? 0) >= 0.9 ? 'ok' : 'warn'} pop={false}>{pctTxt(m.fidelity)}</Badge>} />
              {m.card_on_pc != null && <ListRow title="Answers from the Today card" subtitle={`wrong ${pctTxt(m.card_wrong)} · via Claude ${pctTxt(m.card_via_claude)}`}
                value={<Badge tone={m.card_on_pc >= 0.7 && (m.card_wrong ?? 1) <= 0.1 ? 'ok' : 'warn'} pop={false}>{pctTxt(m.card_on_pc)}</Badge>} />}
              <ListRow title="Natural (Claude, blind)" subtitle={m.natural?.note || ''} value={<span className="k-small">EN {m.natural?.en ?? '–'} · DE {m.natural?.de ?? '–'}</span>} />
              {(m.samples?.en || []).slice(0, 1).map((x, i) => <ListRow key={`en${i}`} wrap title={`“${x.owner}”`} subtitle={x.kairo} />)}
              {(m.samples?.de || []).slice(0, 1).map((x, i) => <ListRow key={`de${i}`} wrap title={`„${x.owner}“`} subtitle={x.kairo} />)}
              {d.installed.includes(id) && d.active !== id && <ListRow title="Use this one" onClick={() => choose(id)} chevron />}
            </List>
          )}
        </div>
      ))}
      {(sp.stt || sp.tts) && (
        <>
          <SectionTitle>Speech on the PC</SectionTitle>
          <List>
            <ListRow title="Whisper (English + German)" subtitle={`word errors ${pctTxt(sp.stt?.auto?.wer)}`} value={<span className="k-small">{msTxt(sp.stt?.auto?.ms)}</span>} />
            <ListRow title="Whisper (English only)" subtitle={`word errors ${pctTxt(sp.stt?.en?.wer)}`} value={<span className="k-small">{msTxt(sp.stt?.en?.ms)}</span>} />
            <ListRow title="Kokoro: first words (EN)" value={<span className="k-small">{msTxt(sp.tts?.en?.first_clause_ms)}</span>} />
            <ListRow title="Piper: first words (DE)" value={<span className="k-small">{msTxt(sp.tts?.de?.first_clause_ms)}</span>} />
            {sp.smart_turn?.pad_side && <ListRow title="Smart Turn (is the owner done?)" subtitle={`complete ${sp.smart_turn[sp.smart_turn.pad_side].complete} · cut off ${sp.smart_turn[sp.smart_turn.pad_side].cut}`}
              value={<span className="k-small">{pctTxt(sp.smart_turn[sp.smart_turn.pad_side].accuracy)} · {msTxt(sp.smart_turn[sp.smart_turn.pad_side].ms)}</span>} />}
          </List>
        </>
      )}
      <SectionTitle>Kyutai (streaming speech, English only)</SectionTitle>
      <List>
        {k.state === 'measured' ? (
          <>
            <ListRow title="Kyutai STT: words after you stop" subtitle={`${k.stt?.ms_per_frame?.p50 ?? '–'} ms per 80 ms frame · word errors ${pctTxt(k.stt?.wer)}`} value={<span className="k-small">{msTxt(k.stt?.after_end_ms)}</span>} />
            <ListRow title="Kyutai TTS: first audio" subtitle={`real-time factor ${k.tts?.rtf ?? '–'} · VRAM ${k.vram_mb ?? '–'} MB`} value={<span className="k-small">{msTxt(k.tts?.first_audio_ms)}</span>} />
          </>
        ) : k.state === 'failed' ? <ListRow wrap title="Kyutai failed on this PC" subtitle={k.error} />
          : d.kyutai.ready ? <ListRow title="Installed" subtitle="Run the lab to measure it" />
            : d.kyutai.requested ? <ListRow title="Install requested" subtitle="The PC installs it within minutes (about 10 GB)" />
              : <ListRow title="Install the Kyutai test" subtitle="About 10 GB, only for this comparison" onClick={kyutai} chevron />}
      </List>
      <SectionTitle>Today card (what the voice model knows right now)</SectionTitle>
      <List>
        {Object.entries(card.titles).filter(([key]) => card.sections[key]).map(([key, title]) => (
          <ListRow key={key} wrap title={title} subtitle={<span className="selectable" style={{ whiteSpace: 'pre-line' }}>{card.sections[key]}</span>} />
        ))}
        {!Object.keys(card.sections).length && <ListRow title="Not read yet" subtitle="Kairo reads it within a minute of starting" />}
      </List>
      {r.estimate_ms && (
        <p className="k-muted k-small k-pad">On the PC, before the phone: quiet {r.estimate_ms.endpoint} + speech to text {r.estimate_ms.stt ?? '–'} + first word {r.estimate_ms.voice_model_first_word ?? '–'} + first audio {r.estimate_ms.first_audio ?? '–'} = {r.estimate_ms.total ?? '–'} ms{r.estimate_ms.kyutai_total ? ` (Kyutai: ${r.estimate_ms.kyutai_total} ms)` : ''}. The phone adds the network and playback: see Voice speed.</p>
      )}
    </Page>
  )
}

function SystemPage() {
  const stack = useStack()
  const toast = useToast()
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [google, setGoogle] = useState(null)
  const [paused, setPaused] = useState(null)
  const [askPause, setAskPause] = useState(false)
  const [speed, setSpeed] = useState([])
  const [conflicts, setConflicts] = useState(0)

  const load = useCallback(async () => {
    const [h, s, g, k, l, c] = await Promise.allSettled([
      fetch('/health').then((r) => r.json()), api('/api/status'), api('/api/setup/google/status'), api('/api/kill-switch'),
      api('/api/latency'), api('/api/vault/conflicts'),
    ])
    setHealth(h.status === 'fulfilled' ? (h.value.detail ?? h.value) : { status: 'unreachable' })
    setStatus(s.status === 'fulfilled' ? s.value : {})
    setGoogle(g.status === 'fulfilled' ? g.value : null)
    setPaused(k.status === 'fulfilled' ? k.value.paused : null)
    setSpeed(l.status === 'fulfilled' ? l.value.rows : [])
    setConflicts(c.status === 'fulfilled' ? c.value.conflicts.length : 0)
  }, [])
  useEffect(() => { load() }, [load])

  const setKill = async (value) => {
    const r = await api('/api/kill-switch', { method: 'POST', body: { paused: value } })
    setPaused(r.paused)
    toast.show({ text: r.paused ? 'Kairo is paused. Nothing runs.' : 'Kairo is running again.' })
  }

  const boot = status?.boot_check
  const googleOk = google?.read && google?.write && !google?.signed_out
  const googleEnd = google && [google.read_may_end, google.write_may_end].filter(Boolean).sort()[0]
  return (
    <Page title="System" onRefresh={load}>
      {health === null ? <SkeletonCard lines={4} /> : (
        <>
          <List>
            <ListRow icon={<IconPower />} title={paused ? 'Paused (kill switch)' : 'Running'}
              subtitle={paused ? 'Nothing runs until you resume' : 'Tap to pause everything'}
              value={<Badge tone={paused ? 'danger' : 'ok'} pop={false}>{paused ? 'Paused' : 'On'}</Badge>}
              onClick={() => (paused ? setKill(false) : setAskPause(true))} />
            <ListRow icon={<IconPulse />} title="Kairo" subtitle={`API ${health.status} · database ${health.db ?? '–'}`}
              value={<Badge tone={health.status === 'ok' ? 'ok' : 'danger'} pop={false}>{health.status === 'ok' ? 'OK' : 'Down'}</Badge>} />
            <ListRow icon={<IconSparkle />} title="Claude" subtitle={health.brain_busy ? 'Working on something' : 'Ready'}
              value={<Badge tone={health.brain === 'ok' ? 'ok' : 'warn'} pop={false}>{health.brain === 'ok' ? 'OK' : (health.brain ?? '–')}</Badge>} />
            <ListRow icon={<IconMail />} title="Google" chevron
              subtitle={!google ? 'Unknown' : google.signed_out ? 'Signed out: sign in again' : google.read ? `Connected${googleEnd ? ` · ends ${new Date(googleEnd * 1000).toLocaleDateString()}` : ''}` : 'Not connected yet'}
              value={<Badge tone={googleOk ? 'ok' : 'warn'} pop={false}>{googleOk ? 'OK' : 'Fix'}</Badge>}
              onClick={() => stack.push('Google', () => <GooglePage st={google} />)} />
            <ListRow icon={<IconShield />} title="Self-test" chevron
              subtitle={status?.selftest ? `${new Date(status.selftest.at).toLocaleString()}${status.selftest.ok ? '' : ` · ${status.selftest.reason}`}` : 'Not run yet'}
              value={status?.selftest ? <Badge tone={status.selftest.ok ? 'ok' : 'danger'} pop={false}>{status.selftest.ok ? 'OK' : 'Failed'}</Badge> : undefined}
              onClick={() => stack.push('Self-test', () => <SelfTestPage st={status?.selftest} />)} />
            <ListRow icon={<IconShield />} title="Last boot check" chevron
              subtitle={boot ? new Date(boot.time).toLocaleString() : 'Not run yet'}
              value={boot ? <Badge tone={boot.all_ok ? 'ok' : 'danger'} pop={false}>{boot.all_ok ? 'OK' : 'Failed'}</Badge> : undefined}
              onClick={() => stack.push('Boot check', () => <BootCheckPage check={boot} />)} />
          </List>
          {conflicts > 0 && (
            <List>
              <ListRow icon={<IconWarn />} title="Sync conflicts" subtitle="Notes changed on two devices at once: pick a version" chevron
                value={<Badge tone="warn" pop={false}>{conflicts}</Badge>}
                onClick={() => stack.push('Sync conflicts', () => <ConflictsPage />)} />
            </List>
          )}
          <SectionTitle>Speed (last 7 days)</SectionTitle>
          <List>
            <ListRow icon={<IconBolt />} title="Voice speed" subtitle="Every turn, step by step: before and after, model, played or not" chevron
              onClick={() => stack.push('Voice speed', () => <VoiceSpeedPage />)} />
            <ListRow icon={<IconMic />} title="Voice lab" subtitle="The local voice models measured on this PC, and the pick" chevron
              onClick={() => stack.push('Voice lab', () => <VoiceLabPage />)} />
            {speed.map((r) => (
              <ListRow key={r.kind} icon={<IconBolt />} title={r.title} subtitle={`Typical ${secs(r.p50_ms)} · slow ones ${secs(r.p95_ms)} · ${r.n}×`} />
            ))}
          </List>
          <SectionTitle>Behind the scenes</SectionTitle>
          <List>
            <ListRow icon={<IconRefresh />} title="Jobs" subtitle="Brief, email triage, captures, reviews" chevron
              onClick={() => stack.push('Jobs', () => <JobsPage />)} />
            <ListRow icon={<IconList />} title="Activity" subtitle="Everything Kairo did, newest first" chevron
              onClick={() => stack.push('Activity', () => <ActivityPage />)} />
            <ListRow icon={<IconShield />} title="Backups" subtitle="Nightly backup and weekly restore test" chevron
              onClick={() => stack.push('Backups', () => <BackupsPage />)} />
            <ListRow icon={<IconChart />} title="Run diagnosis" subtitle="Read-only report to copy or share" chevron
              onClick={() => stack.push('Diagnosis', () => <DiagnosisPage />)} />
            <ListRow icon={<IconCheck />} title="Acceptance" subtitle="Proven on this PC and this iPhone" chevron
              onClick={() => stack.push('Acceptance', () => <AcceptancePage />)} />
            <ListRow icon={<IconSpeaker />} title="Sound check" subtitle="Is Kairo's voice audible on this iPhone?" chevron
              onClick={() => stack.push('Sound check', () => <SoundCheckPage />)} />
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

const KINDS = ['home', 'school', 'work', 'training', 'other']

/* Places and the settings you'd otherwise edit in config files. Same as saying it to Kairo. */
function PrefsBlock() {
  const toast = useToast()
  const [d, setD] = useState(null)
  const [edit, setEdit] = useState(null)
  const load = useCallback(() => api('/api/settings').then(setD).catch(() => setD({ settings: {}, places: [] })), [])
  useEffect(() => { load() }, [load])
  const save = async (key, value) => {
    try { setD(await api(`/api/settings/${key}`, { method: 'PUT', body: { value } })); toast.show({ text: 'Saved.' }) } catch (e) { toast.show({ text: e.message }) }
  }
  const savePlace = async () => {
    try { setD(await api('/api/places', { method: 'POST', body: edit })); setEdit(null); toast.show({ text: 'Place saved.' }) } catch (e) { toast.show({ text: e.message }) }
  }
  const removePlace = async (name) => { setD(await api(`/api/places/${encodeURIComponent(name)}`, { method: 'DELETE' })); setEdit(null) }
  if (!d) return <SkeletonCard lines={3} />
  const st = d.settings || {}
  const time = (label, value, onSave) => (
    <label className="k-field">{label}<input type="time" defaultValue={value || ''} onBlur={(e) => e.target.value && e.target.value !== value && onSave(e.target.value)} /></label>
  )
  return (
    <>
      <SectionTitle>Places</SectionTitle>
      <List>
        {(d.places || []).map((p) => (
          <ListRow key={p.name} icon={<IconPin />} title={p.name} chevron
            subtitle={[p.kind, p.station && `station ${p.station}`, p.valid_from && `from ${p.valid_from}`, p.valid_until && `until ${p.valid_until}`].filter(Boolean).join(' · ')}
            onClick={() => setEdit({ ...p })} />
        ))}
        <ListRow icon={<IconPin />} title="Add a place" subtitle="Home, school, work, training" onClick={() => setEdit({ name: '', kind: 'home' })} />
      </List>
      <p className="k-muted k-small k-pad">Or just tell Kairo: “my home station is …”. Only the station name is sent anywhere; an address stays on the PC.</p>
      <SectionTitle>Times</SectionTitle>
      <div className="k-pad">
        {time('Quiet from', st.quiet_hours?.start, (v) => save('quiet_hours', { start: v, end: st.quiet_hours?.end || '06:30' }))}
        {time('Quiet until', st.quiet_hours?.end, (v) => save('quiet_hours', { start: st.quiet_hours?.start || '22:30', end: v }))}
        {time('Brief on weekdays', st.brief_times?.weekdays || '06:20', (v) => save('brief_times', { weekdays: v, weekends: st.brief_times?.weekends || '08:20' }))}
        {time('Brief on weekends', st.brief_times?.weekends || '08:20', (v) => save('brief_times', { weekdays: st.brief_times?.weekdays || '06:20', weekends: v }))}
        <label className="k-field">Most pings a day
          <input type="number" min={1} max={20} defaultValue={st.max_pings || 6} onBlur={(e) => save('max_pings', Number(e.target.value))} />
        </label>
        {st.recap === 'off'
          ? <Button variant="secondary" size="sm" onClick={() => save('recap', '20:30')} style={{ marginBottom: 12 }}>Turn on the evening recap mail</Button>
          : (
            <>
              {time('Evening recap mail', st.recap || '20:30', (v) => save('recap', v))}
              <Button variant="secondary" size="sm" onClick={() => save('recap', 'off')} style={{ marginBottom: 12 }}>No recap mail</Button>
            </>
          )}
        <label className="k-field">Language
          <select defaultValue={st.language || 'English'} onChange={(e) => save('language', e.target.value)}><option value="English">English</option><option value="German">Deutsch</option></select>
        </label>
      </div>
      <Sheet open={!!edit} onClose={() => setEdit(null)} title={edit?.name ? edit.name : 'New place'}>
        {edit && (
          <>
            <label className="k-field">Name<input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} placeholder="Home, School, Gym" /></label>
            <label className="k-field">Kind<select value={edit.kind} onChange={(e) => setEdit({ ...edit, kind: e.target.value })}>{KINDS.map((k) => <option key={k}>{k}</option>)}</select></label>
            <label className="k-field">Nearest station<input value={edit.station || ''} onChange={(e) => setEdit({ ...edit, station: e.target.value })} placeholder="e.g. Bern" /></label>
            <label className="k-field">Address (private, stays on the PC)<input value={edit.address || ''} onChange={(e) => setEdit({ ...edit, address: e.target.value })} /></label>
            <label className="k-field">From (optional)<input type="date" value={edit.valid_from || ''} onChange={(e) => setEdit({ ...edit, valid_from: e.target.value })} /></label>
            <label className="k-field">Until (optional)<input type="date" value={edit.valid_until || ''} onChange={(e) => setEdit({ ...edit, valid_until: e.target.value })} /></label>
            <Button variant="primary" block disabled={!edit.name?.trim()} onClick={savePlace}>Save</Button>
            {(d.places || []).some((p) => p.name === edit.name) && (
              <Button variant="danger" block style={{ marginTop: 8 }} onClick={() => removePlace(edit.name)}>Remove</Button>
            )}
          </>
        )}
      </Sheet>
    </>
  )
}

function LoginsPage() {
  const toast = useToast()
  const [rows, setRows] = useState(null)
  const [edit, setEdit] = useState(null)
  const [busy, setBusy] = useState('')
  const load = useCallback(() => api('/api/logins').then(setRows).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load])
  const save = async () => {
    setBusy('save')
    try {
      await api(`/api/logins/${edit.site}`, { method: 'PUT', body: { username: edit.username, password: edit.password } })
      setEdit(null); toast.show({ text: 'Saved, encrypted on your PC.' }); load()
    } catch (e) { toast.show({ text: e.message }) } finally { setBusy('') }
  }
  const remove = async (site) => { await api(`/api/logins/${site}`, { method: 'DELETE' }); setEdit(null); load() }
  const check = async (site) => {
    setBusy('check')
    const before = rows.find((x) => x.site === site)?.report?.at
    try {
      await api(`/api/jobs/${site}/run`, { method: 'POST', body: {} })
      toast.show({ text: 'Checking OLAT (read-only). Takes a minute or two.', duration: 4000 })
      for (let i = 0; i < 40; i++) {   // up to ~3 minutes
        await new Promise((r) => setTimeout(r, 4500))
        const next = await api('/api/logins').catch(() => null)
        if (next && next.find((x) => x.site === site)?.report?.at !== before) { setRows(next); break }
      }
    } catch (e) { toast.show({ text: e.message, duration: 6000 }) } finally { setBusy('') }
  }
  if (!rows) return <Page title="Logins"><SkeletonCard lines={3} /></Page>
  return (
    <Page title="Logins" onRefresh={load}>
      <p className="k-hello">Sites Kairo reads for you, read-only. Your password is typed here only (never in chat), encrypted on your PC and never shown again.</p>
      {rows.map((r) => {
        const rep = r.report
        return (
          <div key={r.site}>
            <List>
              <ListRow icon={<IconKey />} title={r.label} chevron
                subtitle={r.password_set ? `${r.username} · ${r.encrypted ? 'encrypted on the PC' : 'saved (dev)'}` : 'Not set up · tap to add'}
                onClick={() => setEdit({ site: r.site, label: r.label, username: r.username || '', password: '' })} />
              {r.password_set && (
                <ListRow icon={<IconRefresh />} title={busy === 'check' ? 'Checking…' : 'Check now'} subtitle={r.browser_installed ? 'Logs in read-only and looks for new material' : 'Browser not installed yet (comes with the next update)'}
                  onClick={busy || !r.browser_installed ? undefined : () => check(r.site)} />
              )}
            </List>
            {rep && (
              <p className="k-muted k-small k-pad selectable" style={{ whiteSpace: 'pre-wrap' }}>
                {rep.error ? `Last check: ${rep.error}` : [
                  `Last check ${parseTs(rep.at).toLocaleString()}: ${rep.courses} courses, ${rep.new_files} new files, ${rep.new_pages} new or changed pages.`,
                  `Read: ${Object.entries(rep.nodes || {}).map(([k, v]) => `${k} ${v}`).join(', ') || 'nothing'}.`,
                  `Never opened (tests, tasks, enrollment…): ${Object.entries(rep.skipped || {}).map(([k, v]) => `${k} ${v}`).join(', ') || 'none'}.`,
                  `Timetable: ${rep.timetable?.found ? rep.timetable.file : 'not found in OLAT'}.`,
                  ...(rep.errors || []).slice(0, 3),
                ].join('\n')}
              </p>
            )}
          </div>
        )
      })}
      <Sheet open={!!edit} onClose={() => setEdit(null)} title={edit?.label || 'Login'}>
        {edit && (
          <>
            <label className="k-field">Username<input autoComplete="username" value={edit.username} onChange={(e) => setEdit({ ...edit, username: e.target.value })} /></label>
            <label className="k-field">Password<input type="password" autoComplete="current-password" value={edit.password} onChange={(e) => setEdit({ ...edit, password: e.target.value })} /></label>
            <p className="k-muted k-small">Kairo only reads: it never opens tests, quizzes or assignments and never sends anything.</p>
            <Button variant="primary" block loading={busy === 'save'} disabled={!edit.username.trim() || !edit.password} onClick={save}>Save</Button>
            {rows.find((x) => x.site === edit.site)?.password_set && (
              <Button variant="danger" block style={{ marginTop: 8 }} onClick={() => remove(edit.site)}>Remove login</Button>
            )}
          </>
        )}
      </Sheet>
    </Page>
  )
}

// Realtime voice (owner's decision 2026-10-15): this month's spend against the cap (real money).
function RealtimeSpend({ b }) {
  const tone = b.over ? 'danger' : b.pct >= 80 ? 'warn' : 'ok'
  return (
    <>
      <SectionTitle>Realtime voice · {b.month}</SectionTitle>
      <List>
        <ListRow icon={<IconMic />} title={`${b.spent_chf.toFixed(2)} of ${b.cap_chf} CHF`}
          subtitle={`${b.calls} calls · ${b.minutes} min · $${b.spent_usd.toFixed(2)} at OpenAI${b.over ? ' · cap reached: local voice until next month' : ''}`}
          value={<Badge tone={tone} pop={false}>{b.pct}%</Badge>} />
      </List>
    </>
  )
}

const REALTIME_REASON = {
  local: 'The voice model on your PC talks (System → Voice lab shows which one and how fast).',
  off: 'Off: calls use the local voice.', no_key: 'No OpenAI key yet: calls use the local voice.',
  cap: 'This month’s cap is reached: local voice until next month.', down: 'OpenAI didn’t answer a moment ago: local voice for now.',
}

function RealtimePage() {
  const toast = useToast()
  const [st, setSt] = useState(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => api('/api/voice/realtime').then(setSt).catch(() => setSt(null)), [])
  useEffect(() => { load() }, [load])
  const save = async (changes) => {
    try { setSt(await api('/api/voice/realtime/settings', { method: 'PUT', body: changes })); toast.show({ text: 'Saved.' }) } catch (e) { toast.show({ text: e.message }) }
  }
  const saveKey = async () => {
    setBusy(true)
    try { setSt(await api('/api/voice/realtime/key', { method: 'PUT', body: { key: key.trim() } })); setKey(''); toast.show({ text: 'Saved, encrypted on your PC.' }) } catch (e) { toast.show({ text: e.message }) } finally { setBusy(false) }
  }
  const removeKey = async () => { setSt(await api('/api/voice/realtime/key', { method: 'DELETE' })); toast.show({ text: 'Key removed from the PC.' }) }
  if (!st) return <Page title="Voice"><SkeletonCard lines={4} /></Page>
  const s = st.settings
  return (
    <Page title="Voice" onRefresh={load}>
      <p className="k-hello">On calls and with the voice button a fast voice model talks and Claude does the real work. Local: everything stays on your PC. OpenAI: your voice and the call’s words go to OpenAI.</p>
      <SectionTitle>Who talks on calls</SectionTitle>
      <div className="k-pad">
        <Segmented id="voiceengine" value={s.engine || 'local'} onChange={(v) => save({ engine: v })}
          options={[{ value: 'local', label: 'Local (free)' }, { value: 'openai', label: 'OpenAI Realtime' }]} />
        <p className="k-muted k-small" style={{ marginTop: 8 }}>
          {s.engine === 'openai'
            ? 'OpenAI’s realtime model talks; Claude still does the work. Costs money, capped below.'
            : 'The voice model on your PC talks (free, nothing leaves the PC); Claude does the work. If it isn’t ready, the old voice answers.'}
        </p>
        <label className="k-field">Language on calls
          <select value={s.language || 'auto'} onChange={(e) => save({ language: e.target.value })}>
            <option value="auto">English and German</option>
            <option value="en">English only (a little faster)</option>
          </select>
        </label>
      </div>
      <List>
        <ListRow icon={<IconMic />} title={st.available ? 'OpenAI Realtime on' : s.engine === 'openai' ? 'OpenAI Realtime not ready' : 'Local voice'}
          subtitle={st.available ? `${s.model} · voice ${s.voice}` : (REALTIME_REASON[st.reason] || st.message)}
          value={<Badge tone={st.available || st.reason === 'local' ? 'ok' : 'warn'} pop={false}>{st.available ? 'OpenAI' : 'Local'}</Badge>} />
      </List>
      <SectionTitle>OpenAI Realtime (optional)</SectionTitle>
      <RealtimeSpend b={st.budget} />
      <SectionTitle>OpenAI key</SectionTitle>
      <div className="k-pad">
        {st.key ? <p className="k-muted k-small">Saved on the PC, encrypted: {st.key}. It never leaves the PC and is never shown again.</p>
          : <p className="k-muted k-small">Paste the key from platform.openai.com (it starts with sk-). It is stored encrypted on the PC only.</p>}
        <label className="k-field">{st.key ? 'Replace key' : 'API key'}<input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk-…" /></label>
        <Button variant="primary" block loading={busy} disabled={!key.trim()} onClick={saveKey}>Save key</Button>
        {st.key && <Button variant="danger" block style={{ marginTop: 8 }} onClick={removeKey}>Remove key</Button>}
      </div>
      <SectionTitle>Budget and voice</SectionTitle>
      <div className="k-pad">
        <label className="k-field">Monthly cap (CHF): at 80% a warning, at 100% the local voice
          <input type="number" min={0} max={200} step={1} defaultValue={s.cap_chf} onBlur={(e) => Number(e.target.value) !== s.cap_chf && save({ cap_chf: Number(e.target.value) })} />
        </label>
        <label className="k-field">Model
          <select value={s.model} onChange={(e) => save({ model: e.target.value })}>{st.models.map((m) => <option key={m} value={m}>{m}</option>)}</select>
        </label>
        <label className="k-field">Voice
          <select value={s.voice} onChange={(e) => save({ voice: e.target.value })}>{st.voices.map((v) => <option key={v} value={v}>{v}</option>)}</select>
        </label>
        <label className="k-field">Quiet before Kairo answers
          <select value={s.silence_ms} onChange={(e) => save({ silence_ms: Number(e.target.value) })}>
            {[300, 400, 500, 700].map((v) => <option key={v} value={v}>{v} ms{v === 400 ? ' (default)' : v < 400 ? ' (faster, may cut you off)' : ''}</option>)}
          </select>
        </label>
        <label className="k-field">Where OpenAI handles it
          <select value={s.region} onChange={(e) => save({ region: e.target.value })}>
            <option value="global">Standard (api.openai.com)</option>
            <option value="eu">EU data residency (needs an EU project approved by OpenAI)</option>
          </select>
        </label>
        <Button variant="secondary" size="sm" onClick={() => save({ enabled: !s.enabled })}>{s.enabled ? 'Turn realtime voice off' : 'Turn realtime voice on'}</Button>
        <p className="k-muted k-small" style={{ marginTop: 12 }}>Speed per turn is in System → Voice speed (“Call, realtime”). Cost per minute of talk: about 2–3 Rappen.</p>
      </div>
    </Page>
  )
}

function SettingsPage() {
  const toast = useToast()
  const stack = useStack()
  const [perm, setPerm] = useState(() => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission))
  const [google, setGoogle] = useState(null)
  useEffect(() => { api('/api/setup/google/status').then(setGoogle).catch(() => {}) }, [])
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
  return (
    <Page title="Settings">
      <PrefsBlock />
      <SectionTitle>Phone and account</SectionTitle>
      <List>
        <ListRow icon={<IconBell />} title="Notifications"
          subtitle={perm === 'unsupported' ? 'Open Kairo from the Home Screen first' : perm === 'granted' ? 'On · tap to reconnect' : 'Tap to turn on'}
          onClick={perm === 'unsupported' ? undefined : enablePush} />
        <ListRow icon={<IconMail />} title="Google account" chevron subtitle={google?.signed_out ? 'Signed out' : google?.read ? 'Connected' : 'Not connected'}
          onClick={() => stack.push('Google', () => <GooglePage st={google} />)} />
        <ListRow icon={<IconKey />} title="Logins" subtitle="School portal (OLAT), read-only" chevron
          onClick={() => stack.push('Logins', () => <LoginsPage />)} />
        <ListRow icon={<IconMic />} title="Voice" subtitle="Local voice model or OpenAI Realtime, language" chevron
          onClick={() => stack.push('Voice', () => <RealtimePage />)} />
      </List>
      <SectionTitle>Developer</SectionTitle>
      <List>
        <ListRow icon={<IconKey />} title="Components" subtitle="Every component and animation" chevron
          onClick={() => { window.location.hash = '#/dev/components' }} />
      </List>
    </Page>
  )
}

function Root() {
  const stack = useStack()
  const toast = useToast()
  const [todos, setTodos] = useState(null)
  const [issues, setIssues] = useState([])

  const load = useCallback(async () => {
    api('/api/owner-todos').then(setTodos).catch(() => setTodos({}))
    api('/api/today').then((t) => setIssues(t.issues || [])).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])
  // Today's warning chip opens System straight away.
  useEffect(() => {
    let open = null
    try { open = sessionStorage.getItem('kairo-more-open'); sessionStorage.removeItem('kairo-more-open') } catch { /* private */ }
    if (open === 'system') stack.push('System', () => <SystemPage />)
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  const toggleTodo = async (id) => {
    const next = await api(`/api/owner-todos/${id}`, { method: 'POST', body: { done: !todos[id] } })
    setTodos(next)
    stack.pop()
    toast.show({ text: next[id] ? 'Nice. Ticked off.' : 'Back on the list.' })
  }
  const openTodos = TODOS.filter((t) => !(todos || {})[t.id])
  const doneTodos = TODOS.filter((t) => (todos || {})[t.id])
  return (
    <Page title="More" onRefresh={load}>
      {todos === null ? <SkeletonCard lines={3} /> : (
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

          <List>
            <ListRow icon={<IconMic />} title="Voice" subtitle="Calls, voice notes, test" chevron
              onClick={() => stack.push('Voice', () => <VoicePage />)} />
            <ListRow icon={<IconHeart />} title="Life" subtitle="Shopping, chores, people, money, health" chevron
              onClick={() => { window.location.hash = '#life' }} />
            <ListRow icon={<IconInbox />} title="Capture" subtitle="Shortcuts, lock screen, Back Tap" chevron
              onClick={() => stack.push('Capture', () => <CapturePage />)} />
            <ListRow icon={<IconPin />} title="Location" subtitle="Places, history, pause" chevron
              onClick={() => stack.push('Location', () => <LocationPage />)} />
            <ListRow icon={<IconChart />} title="Usage" subtitle="Claude runs per model and job, realtime voice spend" chevron
              onClick={() => stack.push('Usage', () => <UsagePage />)} />
            <ListRow icon={<IconPulse />} title="System" subtitle={issues.length ? issues.join(' · ') : 'Kairo, Claude, Google, backups'} chevron
              value={issues.length ? <Badge tone="warn" pop={false}>{issues.length}</Badge> : <Badge tone="ok" pop={false}>OK</Badge>}
              onClick={() => stack.push('System', () => <SystemPage />)} />
            <ListRow icon={<IconGear />} title="Settings" subtitle="Notifications, account" chevron
              onClick={() => stack.push('Settings', () => <SettingsPage />)} />
          </List>

          <SectionTitle>Plans and school</SectionTitle>
          <List>
            <ListRow icon={<IconList />} title="Board" subtitle="Goals, projects, tasks" chevron
              onClick={() => { window.location.hash = '#board' }} />
            <ListRow icon={<IconSparkle />} title="School" subtitle="Dates, grades, flashcards, study guides" chevron
              onClick={() => { window.location.hash = '#school' }} />
            <ListRow icon={<IconList />} title="Lerndokumentation" subtitle="Weekly entry, German draft by Kairo" chevron
              onClick={() => { window.location.hash = '#lerndoku' }} />
            <ListRow icon={<IconRefresh />} title="Reviews" subtitle="Check-ins and weekly to yearly reviews" chevron
              onClick={() => { window.location.hash = '#reviews' }} />
          </List>

          {doneTodos.length > 0 && (
            <>
              <SectionTitle>Done</SectionTitle>
              <List>
                {doneTodos.map((t) => (
                  <ListRow key={t.id} icon={<IconCheck />} title={t.title} subtitle="Done" chevron
                    onClick={() => stack.push(t.title, () => <TodoPage todo={t} done onToggle={() => toggleTodo(t.id)} />)} />
                ))}
              </List>
            </>
          )}
        </>
      )}
    </Page>
  )
}

export default function More() {
  return <Stack root={<Root />} rootTitle="More" />
}
