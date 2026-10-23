// More -> Usage -> "Where the questions go" (G11): the rules in force and the routes taken, from GET /api/router.
// Only the class of the content and the reason are shown, never a prompt (the server doesn't send one).
import { parseTs } from '../api.js'
import { providerRow, routeRow, summaryLine } from '../routes.js'
import { Badge, List, ListRow, SectionTitle } from '../ui/index.js'

export function RoutesView({ v }) {
  return (
    <>
      <SectionTitle>Where the questions go</SectionTitle>
      <p className="k-muted k-small k-pad">{summaryLine(v)}</p>
      <List>{v.providers.map(providerRow).map((p) => (
        <ListRow key={p.title} wrap title={p.title} subtitle={`${p.subtitle}${p.on ? '' : ` · ${p.why.replace(/^off: /, '')}`}`}
          value={<Badge tone={p.on ? 'ok' : 'neutral'} pop={false}>{p.on ? 'On' : 'Off'}</Badge>} />
      ))}</List>
      {v.problems.length > 0 && <p className="k-muted k-small k-pad">routing.yaml: {v.problems.join(' ')}</p>}
      {v.local.running_model && (
        <p className="k-muted k-small k-pad">
          Model on this PC: <b>{v.local.running_model}</b>{v.local.running_model_is_lab_model ? ' (the one the router lab passed)' : ''}. After the router lab passes a job on it, this goes into routing.yaml as lab_model_hash.
        </p>
      )}
      {v.recent.length > 0 && (
        <>
          <SectionTitle>Latest routes</SectionTitle>
          <List>{v.recent.map(routeRow).map((r, i) => (
            <ListRow key={i} wrap title={r.title}
              subtitle={<><span style={{ display: 'block' }}>{r.subtitle}</span><span style={{ display: 'block' }}>{r.why} · {parseTs(r.ts)?.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}</span></>}
              value={r.tone === 'neutral' ? undefined : <Badge tone={r.tone} pop={false}>{r.tone === 'danger' ? 'Stopped' : 'Fell back'}</Badge>} />
          ))}</List>
        </>
      )}
      <p className="k-muted k-small k-pad">Your Claude subscription takes every call today. The class of the content (public, low, personal, secret) comes from the job and from what the check finds in the text (addresses, numbers, names of your people); it can only get stricter. Free cloud models and the model on this PC are off until you and the router lab say otherwise. Only the class and the reason are written down, never your text.</p>
    </>
  )
}
