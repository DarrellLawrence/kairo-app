import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import {
  Badge, Card, EmptyState, Expandable, IconCalendar, IconMail, IconPulse, IconShield, IconSparkle,
  List, ListRow, Page, SectionTitle, SkeletonCard,
} from '../ui/index.js'

function greeting(d = new Date()) {
  const h = d.getHours()
  return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

function BriefDetail({ startDrag, close }) {
  return (
    <div className="k-detail-body">
      <div className="k-detail-head" onPointerDown={startDrag}>
        <div className="k-sheet-grab"><span /></div>
        <p className="k-eyebrow">Morning brief</p>
        <h2 className="k-card-title" style={{ fontSize: 28 }}>Your day at a glance</h2>
      </div>
      <EmptyState
        icon={<IconSparkle />}
        title="Arrives with Milestone C"
        text="Every morning: the three things that matter, your calendar, deadlines and what Kairo already handled overnight."
      />
      <div className="k-pad"><button className="k-btn secondary block" onClick={close}>Close</button></div>
    </div>
  )
}

export default function Today({ openCards, go }) {
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [google, setGoogle] = useState(null)
  const [briefOpen, setBriefOpen] = useState(false)

  const load = useCallback(async () => {
    const [h, s, g] = await Promise.allSettled([
      fetch('/health').then((r) => r.json()),
      api('/api/status'),
      api('/api/setup/google/status'),
    ])
    setHealth(h.status === 'fulfilled' ? (h.value.detail ?? h.value) : { status: 'unreachable' })
    setStatus(s.status === 'fulfilled' ? s.value : {})
    setGoogle(g.status === 'fulfilled' ? g.value : null)
  }, [])

  useEffect(() => { load() }, [load])

  const loading = health === null
  const boot = status?.boot_check
  const allOk = health?.status === 'ok' && health?.brain === 'ok' && (!boot || boot.all_ok)
  const googleEnd = google && [google.read_may_end, google.write_may_end].filter(Boolean).sort()[0]

  return (
    <Page title="Today" onRefresh={load}>
      <p className="k-hello">{greeting()} · {new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}</p>

      <Expandable
        id="brief"
        open={briefOpen}
        onOpen={() => setBriefOpen(true)}
        onClose={() => setBriefOpen(false)}
        preview={
          <Card accent interactive>
            <p className="k-eyebrow">Morning brief</p>
            <p className="k-card-title">Your day at a glance</p>
            <p className="k-card-sub">Top 3, calendar and deadlines arrive with Milestone C. Tap to preview.</p>
          </Card>
        }
        detail={(p) => <BriefDetail {...p} />}
      />

      {loading ? <><SkeletonCard lines={2} /><SkeletonCard lines={2} /></> : (
        <>
          <div className="k-grid2">
            <Card interactive glass onClick={() => go('approvals')}>
              <p className="k-eyebrow">Waiting</p>
              <div className="k-stat"><b>{openCards}</b><span className="k-muted k-small">{openCards === 1 ? 'approval' : 'approvals'}</span></div>
            </Card>
            <Card interactive glass onClick={() => go('more')}>
              <p className="k-eyebrow">Kairo</p>
              <div className="k-stat">
                <Badge tone={allOk ? 'ok' : 'warn'} pop={false}>{allOk ? 'All good' : 'Check'}</Badge>
              </div>
            </Card>
          </div>

          <SectionTitle>Status</SectionTitle>
          <List>
            <ListRow icon={<IconPulse />} title="Kairo" subtitle={health.status === 'ok' ? 'Running' : `Problem: ${health.status}`}
              value={<Badge tone={health.status === 'ok' ? 'ok' : 'danger'} pop={false}>{health.status === 'ok' ? 'OK' : 'Down'}</Badge>} />
            <ListRow icon={<IconSparkle />} title="Claude" subtitle={health.brain_busy ? 'Working on something' : 'Ready'}
              value={<Badge tone={health.brain === 'ok' ? 'ok' : 'warn'} pop={false}>{health.brain === 'ok' ? 'OK' : (health.brain ?? '–')}</Badge>} />
            <ListRow icon={<IconMail />} title="Google"
              subtitle={!google ? 'Unknown' : google.signed_out ? 'Signed out: sign in again under More' : google.read ? `Connected${googleEnd ? ` · ends ${new Date(googleEnd * 1000).toLocaleDateString()}` : ''}` : 'Not connected yet'}
              value={<Badge tone={google?.read && !google?.signed_out ? 'ok' : 'warn'} pop={false}>{google?.read && !google?.signed_out ? 'OK' : 'Fix'}</Badge>} />
            <ListRow icon={<IconShield />} title="Last boot check" subtitle={boot ? new Date(boot.time).toLocaleString() : 'Not run yet'}
              value={boot ? <Badge tone={boot.all_ok ? 'ok' : 'danger'} pop={false}>{boot.all_ok ? 'OK' : 'Failed'}</Badge> : undefined} />
          </List>

          <SectionTitle>Calendar</SectionTitle>
          <List>
            <ListRow icon={<IconCalendar />} title="Your calendar shows here" subtitle="With the morning brief (Milestone C)" />
          </List>
        </>
      )}
    </Page>
  )
}
