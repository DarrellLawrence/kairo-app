import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { applyFlags, changedCount, describeFlag, groupFlags, setFlag } from '../flags.js'
import { Badge, Button, EmptyState, IconBolt, List, ListRow, Page, SectionTitle, Sheet, SkeletonCard, useToast } from '../ui/index.js'

/* More -> System -> Experiments: every switch the PC lists (GET /api/flags), whatever it lists, so a switch a
   later build adds shows up here with no change to this page. A switch is read when a call or a job starts,
   so a change applies from the next one. Owner only, like every route. */
export default function ExperimentsPage() {
  const toast = useToast()
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(null)       // the name of the switch whose sheet is open
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api('/api/flags')
    .then((r) => { setRows(applyFlags(r)); setError('') })
    .catch((e) => { setError(e.message); setRows((x) => x ?? []) }), [])
  useEffect(() => { load() }, [load])

  const change = async (name, value) => {
    setBusy(true)
    try {
      setRows(await setFlag(name, value))
      toast.show({ text: value === null ? 'Back to the default.' : value ? 'Turned on.' : 'Turned off.' })
      setOpen(null)
    } catch (e) {
      toast.show({ text: e.message })
    } finally {
      setBusy(false)
    }
  }

  const found = rows?.find((r) => r.name === open) || null
  const shown = useRef(null)               // the sheet keeps its words while it slides away
  if (found) shown.current = found
  const current = found || shown.current
  const d = current && describeFlag(current)
  const changed = changedCount(rows)
  return (
    <Page title="Experiments" onRefresh={load}>
      {rows === null && <SkeletonCard lines={4} />}
      {rows !== null && (
        <>
          <p className="k-hello">
            Switches for changes that wait for your OK or for a measurement on the PC. A switch is read when a call or a job starts, so a
            change applies from the next one. “Back to default” gives up your choice and goes with what the build recommends.
            {changed > 0 ? ` ${changed} changed by you.` : ''}
          </p>
          {error && <p className="k-small k-pad" style={{ color: 'var(--danger)' }}>{error}</p>}
          {rows.length === 0 && !error && <EmptyState icon={<IconBolt />} title="No switches yet" text="Builds add them here when a change needs your OK." />}
          {groupFlags(rows).map((g) => (
            <div key={g.prefix}>
              <SectionTitle>{g.title}</SectionTitle>
              <List>
                {g.rows.map((r) => {
                  const x = describeFlag(r)
                  return (
                    <ListRow key={r.name} icon={<IconBolt />} title={r.name} subtitle={r.what} wrap chevron
                      value={<Badge tone={r.on ? 'ok' : 'neutral'} pop={false}>{x.now}{x.changed ? ' · changed' : ''}</Badge>}
                      onClick={() => setOpen(r.name)} />
                  )
                })}
              </List>
            </div>
          ))}
        </>
      )}
      <Sheet open={Boolean(found)} onClose={() => setOpen(null)} title={current?.name}>
        {current && (
          <>
            <p className="k-small" style={{ margin: '0 0 12px' }}>{current.what}</p>
            <p className="k-muted k-small" style={{ margin: '0 0 20px' }}>
              Now: {d.now}. Default: {d.default}.{d.changed ? ' You changed it.' : ''}
            </p>
            <Button variant="primary" block loading={busy} disabled={busy} onClick={() => change(current.name, d.next)}>{d.nextLabel}</Button>
            <Button block style={{ marginTop: 8 }} disabled={busy || !d.changed} onClick={() => change(current.name, null)}>Back to default ({d.default})</Button>
            <Button variant="secondary" block style={{ marginTop: 8 }} onClick={() => setOpen(null)}>Close</Button>
          </>
        )}
      </Sheet>
    </Page>
  )
}
