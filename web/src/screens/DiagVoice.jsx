import { useCallback, useEffect, useState } from 'react'
import { api, parseTs } from '../api.js'
import { BackButton, Badge, Button, IconCheck, IconWarn, List, ListRow, Page, SectionTitle, SkeletonCard } from '../ui/index.js'
import { RATES, TONE, configRows, groupChecks, machineRows, meter, ms, percent, stageBars, verdict } from '../diagvoice.js'

/* Voice check (/diag/voice, More -> System): the same report as `kairo diag` on the PC (GET /api/diag/voice, owner only,
   read-only, redacted there), then the newest live run of the latency harness against the 1.0 s target. */

// A bar for a number: the fill is the value, the thin line is the target.
function Meter({ value, target }) {
  const m = meter(value, target)
  return (
    <span className="k-meter" role="img" aria-label={`${ms(value)}${target ? ` of ${ms(target)}` : ''}`}>
      <span className={m.over ? 'k-meter-fill over' : 'k-meter-fill'} style={{ width: `${m.fill}%` }} />
      {m.target != null && <span className="k-meter-target" style={{ left: `${m.target}%` }} />}
    </span>
  )
}

function Latency({ bench, checks }) {
  const latest = bench?.latest
  const note = checks.find((c) => c.id === 'bench.latest')
  if (!bench?.installed || !latest || latest.mode !== 'live' || !latest.summary) {
    return (
      <>
        <SectionTitle>Latency</SectionTitle>
        <p className="k-muted k-small k-pad">{bench?.installed ? (note?.detail || 'No live run yet.') : 'The latency harness is not on this PC yet, so there are no timings to show.'}</p>
      </>
    )
  }
  const s = latest.summary
  const target = s.target_p95_ms ?? 1000
  const when = parseTs(latest.at)?.toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  const rows = [...machineRows(latest.machine), ...configRows(latest.config)]
  return (
    <>
      <SectionTitle>Latest live run · {when || '?'}</SectionTitle>
      <List>
        {[['p50', 'p50_ms'], ['p90', 'p90_ms'], ['p95', 'p95_ms']].map(([label, key]) => (
          <ListRow key={key} title={`${label} · ${ms(s[key])}`} subtitle={<Meter value={s[key]} target={target} />}
            value={key === 'p95' ? <Badge tone={s.pass ? 'ok' : 'danger'} pop={false}>{s.pass ? 'On target' : 'Over target'}</Badge> : undefined} />
        ))}
      </List>
      <p className="k-muted k-small k-pad">Target: p95 under {ms(target)} · {s.n} turns · version {latest.version || '?'}</p>
      <SectionTitle>Where the time goes · p50 / p95</SectionTitle>
      <List>
        {stageBars(s.by_stage).map((b) => (
          <ListRow key={b.id} title={b.title} value={`${ms(b.p50)} / ${ms(b.p95)}`}
            subtitle={<span className="k-meter"><span className="k-meter-fill light" style={{ width: `${b.p95Pct}%` }} /><span className="k-meter-fill" style={{ width: `${b.p50Pct}%` }} /></span>} />
        ))}
      </List>
      <SectionTitle>How often it goes wrong</SectionTitle>
      <List>
        {RATES.map(([key, title, why]) => (
          <ListRow key={key} title={title} subtitle={why} wrap value={<Badge tone={s[key] > 0 ? 'warn' : 'ok'} pop={false}>{percent(s[key])}</Badge>} />
        ))}
      </List>
      {rows.length > 0 && (
        <>
          <SectionTitle>Machine and settings</SectionTitle>
          <List>{rows.map(([k, v]) => <ListRow key={k} title={k} subtitle={String(v)} wrap />)}</List>
        </>
      )}
    </>
  )
}

// The page for one report (or the reason there is none).
export function VoiceCheck({ report, error, busy, onRun, onBack }) {
  const v = verdict(report)
  const when = parseTs(report?.at)?.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return (
    <Page title="Voice check" left={<BackButton label="System" onClick={onBack} />} onRefresh={onRun}>
      {!report && !error && <SkeletonCard lines={5} />}
      {error && (
        <div className="k-pad">
          <p className="k-small" style={{ color: 'var(--danger)' }}>{error}</p>
          <Button block loading={busy} disabled={busy} onClick={onRun}>Try again</Button>
        </div>
      )}
      {report && (
        <>
          <List>
            <ListRow icon={v.tone === 'danger' || v.tone === 'warn' ? <IconWarn /> : <IconCheck />} title={v.label} subtitle={v.line} wrap
              value={<Badge tone={v.tone} pop={false}>{report.ok ? 'Exit 0' : 'Exit 1'}</Badge>} />
          </List>
          <p className="k-hello">
            The same checks as <b>kairo diag</b> on the PC, made {when ? `at ${when}` : 'just now'} · version {report.host?.version}. Read-only.
          </p>
          <div className="k-pad"><Button block loading={busy} disabled={busy} onClick={onRun}>Run again</Button></div>
          {groupChecks(report.checks).map((g) => (
            <div key={g.group}>
              <SectionTitle>{g.group}</SectionTitle>
              <List>
                {g.checks.map((c) => (
                  <ListRow key={c.id} icon={c.status === 'PASS' ? <IconCheck /> : <IconWarn />} title={c.name} subtitle={c.detail} wrap
                    value={<Badge tone={TONE[c.status] || 'neutral'} pop={false}>{c.status}</Badge>} />
                ))}
              </List>
            </div>
          ))}
          <Latency bench={report.bench} checks={report.checks} />
        </>
      )}
    </Page>
  )
}

export default function DiagVoice() {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    setBusy(true)
    try {
      setReport(await api('/api/diag/voice'))
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [])
  useEffect(() => { load() }, [load])
  // Back to More -> System, where the link is.
  const back = () => {
    try { sessionStorage.setItem('kairo-more-open', 'system') } catch { /* private */ }
    if (window.location.pathname !== '/') window.history.replaceState(null, '', '/')
    window.location.hash = '#more'
  }
  return <VoiceCheck report={report} error={error} busy={busy} onRun={load} onBack={back} />
}
