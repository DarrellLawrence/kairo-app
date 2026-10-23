// node --test: the Routes part of More -> Usage renders what GET /api/router sends, in words, and nothing else.
// Vite loads the JSX for the server-side render; no browser. Made-up data only.
import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let server
let RoutesView
before(async () => {
  server = await createServer({ configFile: false, root: fileURLToPath(new URL('..', import.meta.url)),
    logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null }, optimizeDeps: { noDiscovery: true, include: [] },
    ssr: { noExternal: ['motion'] }, esbuild: { jsx: 'automatic' } })
  ;({ RoutesView } = await server.ssrLoadModule('/src/screens/Routes.jsx'))
})
after(async () => { await server?.close() })

const base = () => ({
  router_on: true, local_on: false, rules_loaded: true, problems: [], total: 2, days: 7, by_route: { claude: 2 }, by_tier: { R1: 2 },
  providers: [
    { name: 'claude', tier: 'R1', enabled: true, status: 'on' },
    { name: 'local', tier: 'R0', enabled: true, status: 'off: g11.local_route is off' },
    { name: 'groq', tier: 'R2', enabled: false, status: 'off: switched off in routing.yaml' },
  ],
  local: { purposes: [], lab_model_hash_set: false, could_run: ['reply_check', 'triage'], running_model: '', running_model_is_lab_model: false },
  recent: [
    { ts: '2026-10-23T14:03:00', purpose: 'triage', mode: 'job', content: 'secret', hits: ['email'], route: 'claude', tier: 'R1', model: 'haiku',
      reason: 'the subscription: the default for every purpose', fallback_from: null },
    { ts: '2026-10-23T14:01:00', purpose: 'chat', mode: 'owner', content: 'secret', hits: [], route: 'none', tier: null, model: null,
      reason: 'no route allowed: claude: switched off in routing.yaml', fallback_from: null },
  ],
})
const html = (v) => renderToStaticMarkup(createElement(RoutesView, { v }))

test('the providers, the summary and the latest routes are shown with their reasons', () => {
  const out = html(base())
  assert.match(out, /Where the questions go/)
  assert.match(out, /2 calls in the last 7 days/)
  assert.match(out, /Claude subscription/)
  assert.match(out, /g11\.local_route is off/)
  assert.match(out, /groq \(free cloud\)/)
  assert.match(out, /triage → Claude subscription/)
  assert.match(out, /the subscription: the default for every purpose/)
  assert.match(out, /found: email/)
  assert.match(out, /chat → stopped/)
  assert.match(out, /Stopped/)
})

test('with the router off it says so and shows no routes', () => {
  const out = html({ ...base(), router_on: false, total: 0, recent: [], by_route: {} })
  assert.match(out, /g11\.router is off/)
  assert.doesNotMatch(out, /Latest routes/)
})

test('the running model and a refused row of routing.yaml are shown', () => {
  const out = html({ ...base(), problems: ['groq: R0 is more trusted than the code allows'], local: { ...base().local, running_model: 'a1b2c3d4e5f6' } })
  assert.match(out, /routing\.yaml: groq: R0 is more trusted/)
  assert.match(out, /a1b2c3d4e5f6/)
  assert.match(out, /lab_model_hash/)
})
