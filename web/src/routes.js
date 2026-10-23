// More -> Usage -> "Where the questions go" (G11): turns GET /api/router into words. No React in here, so node --test
// can check them. The server never sends a prompt; neither does anything here.

const ROUTE_WORDS = { claude: 'Claude subscription', local: 'This PC (local model)', none: 'stopped' }
const SHORT_WORDS = { claude: 'Claude subscription', local: 'this PC', none: 'refused' }
const MODE_WORDS = { owner: 'your message', call: 'call', job: 'background job' }
const MAY_SEE = { R0: 'may see everything', R1: 'may see everything', R2: 'public and low content only', R3: 'public content only' }

export function contentWords(c) {
  return c || 'unknown'
}

export function providerRow(p) {
  const title = p.name === 'claude' || p.name === 'local' ? ROUTE_WORDS[p.name] : `${p.name} (free cloud)`
  return { title, subtitle: `${p.tier} · ${MAY_SEE[p.tier] || 'unknown class'}`, on: p.status === 'on', why: p.status }
}

export function tallies(byRoute) {
  return Object.entries(byRoute || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1])
}

export function summaryLine(v) {
  if (!v.router_on) return 'Kairo is not writing down where each question goes (g11.router is off). Every call goes to your Claude subscription, as always.'
  const lead = v.rules_loaded ? '' : 'There is no routing.yaml, so only the Claude subscription can be used. '
  if (!v.total) return `${lead}No calls yet in this period.`
  const span = v.days === 1 ? 'the last day' : `the last ${v.days} days`
  const parts = tallies(v.by_route).map(([r, n]) => `${SHORT_WORDS[r] || r}: ${n}`).join(' · ')
  return `${lead}${v.total} call${v.total === 1 ? '' : 's'} in ${span}. ${parts}`
}

export function routeRow(r) {
  const model = r.model && r.model !== 'local' ? r.model[0].toUpperCase() + r.model.slice(1) : null
  const found = r.hits && r.hits.length ? `found: ${r.hits.join(', ')}` : null
  const bits = [`${contentWords(r.content)} content`, found, model, MODE_WORDS[r.mode] || r.mode].filter(Boolean)
  const route = ROUTE_WORDS[r.route] || r.route
  const title = r.fallback_from ? `${r.purpose} → ${route}, after ${SHORT_WORDS[r.fallback_from] || r.fallback_from} did not work out` : `${r.purpose} → ${route}`
  const tone = r.route === 'none' ? 'danger' : r.fallback_from ? 'warn' : 'neutral'
  return { title, subtitle: bits.join(' · '), why: r.reason, tone, ts: r.ts }
}
