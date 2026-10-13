import { useCallback, useEffect, useState } from 'react'
import { api, parseTs } from '../api.js'
import {
  Badge, Button, EmptyState, IconBell, IconKey, IconList, IconMail, IconPower, IconPulse, IconShield, IconSparkle,
  List, ListRow, Page, SectionTitle, Sheet, SkeletonCard, Stack, useStack, useToast,
} from '../ui/index.js'

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

  const load = useCallback(async () => {
    const [h, s, g, k] = await Promise.allSettled([
      fetch('/health').then((r) => r.json()), api('/api/status'), api('/api/setup/google/status'), api('/api/kill-switch'),
    ])
    setHealth(h.status === 'fulfilled' ? (h.value.detail ?? h.value) : { status: 'unreachable' })
    setStatus(s.status === 'fulfilled' ? s.value : {})
    setGoogle(g.status === 'fulfilled' ? g.value : null)
    setPaused(k.status === 'fulfilled' ? k.value.paused : null)
  }, [])
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

          <SectionTitle>History</SectionTitle>
          <List>
            <ListRow icon={<IconList />} title="Activity" subtitle="Everything Kairo did, newest first" chevron
              onClick={() => stack.push('Activity', () => <ActivityPage />)} />
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
