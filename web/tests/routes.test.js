// node --test: the words on More -> Usage -> "Where the questions go" (routes.js). Plain functions on the data of
// GET /api/router; made-up routes only.
import assert from 'node:assert/strict'
import { test } from 'node:test'

const { contentWords, providerRow, routeRow, summaryLine, tallies } = await import('../src/routes.js')

const view = (over = {}) => ({
  router_on: true, local_on: false, rules_loaded: true, problems: [], total: 3, days: 7,
  by_route: { claude: 3 }, by_tier: { R1: 3 },
  providers: [
    { name: 'claude', tier: 'R1', enabled: true, status: 'on' },
    { name: 'local', tier: 'R0', enabled: true, status: 'off: g11.local_route is off' },
    { name: 'groq', tier: 'R2', enabled: false, status: 'off: switched off in routing.yaml' },
  ],
  local: { purposes: [], lab_model_hash_set: false, could_run: ['reply_check', 'triage'], running_model: '', running_model_is_lab_model: false },
  recent: [], ...over,
})

test('the router being off is said first, in words, and nothing is claimed about routes', () => {
  assert.match(summaryLine(view({ router_on: false, total: 0 })), /not writing down/i)
  assert.match(summaryLine(view({ router_on: false, total: 0 })), /Claude subscription/)
})

test('with the router on it says how many calls went where, over how many days', () => {
  const s = summaryLine(view({ total: 3, by_route: { claude: 3 } }))
  assert.match(s, /3 calls/)
  assert.match(s, /7 days/)
  assert.match(s, /Claude subscription: 3/)
  assert.match(summaryLine(view({ total: 1, days: 1, by_route: { claude: 1 } })), /1 call in the last day/)
  assert.match(summaryLine(view({ total: 0, by_route: {} })), /No calls yet/)
})

test('a missing routing.yaml is said, not hidden', () => {
  assert.match(summaryLine(view({ rules_loaded: false })), /no routing\.yaml/i)
})

test('local and none are named in words', () => {
  const s = summaryLine(view({ total: 5, by_route: { claude: 3, local: 1, none: 1 } }))
  assert.match(s, /this PC: 1/)
  assert.match(s, /refused: 1/)
})

test('a provider row says its class, what it is allowed to see and whether it is on', () => {
  const rows = view().providers.map(providerRow)
  assert.deepEqual(rows[0], { title: 'Claude subscription', subtitle: 'R1 · may see everything', on: true, why: 'on' })
  assert.equal(rows[1].title, 'This PC (local model)')
  assert.equal(rows[1].on, false)
  assert.match(rows[1].why, /g11\.local_route is off/)
  assert.equal(rows[2].subtitle, 'R2 · public and low content only')
  assert.equal(rows[2].title, 'groq (free cloud)')
  assert.equal(providerRow({ name: 'mistral_free', tier: 'R3', enabled: false, status: 'off' }).subtitle, 'R3 · public content only')
})

test('a route row says purpose, route, class of the content, what the scanner found, the model and why', () => {
  const r = routeRow({ ts: '2026-10-23T14:03:00', purpose: 'triage', mode: 'job', content: 'secret', hits: ['email', 'iban'], route: 'claude',
    tier: 'R1', model: 'haiku', reason: 'the subscription: the default for every purpose', fallback_from: null })
  assert.equal(r.title, 'triage → Claude subscription')
  assert.match(r.subtitle, /secret content/)
  assert.match(r.subtitle, /found: email, iban/)
  assert.match(r.subtitle, /Haiku/)
  assert.match(r.subtitle, /background job/)
  assert.equal(r.why, 'the subscription: the default for every purpose')
  assert.equal(r.tone, 'neutral')
})

test('a refused local answer and a route that was stopped stand out', () => {
  const fell = routeRow({ ts: '2026-10-23T14:03:00', purpose: 'triage', mode: 'job', content: 'personal', hits: [], route: 'claude', tier: 'R1',
    model: 'haiku', reason: 'local did not work out (not valid JSON); the subscription', fallback_from: 'local' })
  assert.equal(fell.tone, 'warn')
  assert.match(fell.title, /after this PC/)
  const none = routeRow({ ts: '2026-10-23T14:03:00', purpose: 'chat', mode: 'owner', content: 'secret', hits: [], route: 'none', tier: null,
    model: null, reason: 'no route allowed: claude: switched off in routing.yaml', fallback_from: null })
  assert.equal(none.tone, 'danger')
  assert.equal(none.title, 'chat → stopped')
})

test('modes and unknown values do not break the words', () => {
  assert.match(routeRow({ purpose: 'x', mode: 'call', content: 'low', hits: [], route: 'claude', reason: '' }).subtitle, /call/)
  assert.equal(routeRow({ purpose: 'x', mode: 'weird', content: 'weird', hits: null, route: 'weird', reason: '' }).title, 'x → weird')
  assert.equal(contentWords('personal'), 'personal')
})

test('tallies are sorted with the biggest first and never include a zero', () => {
  assert.deepEqual(tallies({ claude: 3, local: 10, none: 0 }), [['local', 10], ['claude', 3]])
  assert.deepEqual(tallies(null), [])
})
