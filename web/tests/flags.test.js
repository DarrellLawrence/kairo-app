// node --test: the phone's side of the owner-only switches (flags.js): what a switch is when the PC hasn't
// answered, what it is once it has, and how More -> System -> Experiments groups and words them.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, test } from 'node:test'
import { DEFAULTS, applyFlags, changedCount, describeFlag, flagOn, groupFlags, refreshFlags, resetFlags, setFlag } from '../src/flags.js'

const row = (name, on, def = on, what = `what ${name} does`) => ({ name, on, default: def, what })

// Two stand-in switches, so the logic is checked whatever the real list holds.
const TEST_DEFAULTS = { 't.on': true, 't.off': false }
beforeEach(() => Object.assign(DEFAULTS, TEST_DEFAULTS))
afterEach(() => { resetFlags(); delete globalThis.fetch; for (const k of Object.keys(TEST_DEFAULTS)) delete DEFAULTS[k] })

test('before the PC has answered a switch is its default, and a name nobody knows is off', () => {
  for (const [name, def] of Object.entries(DEFAULTS)) assert.equal(flagOn(name), def, name)
  assert.equal(flagOn('nobody.knows'), false)
})

test('once the PC has answered, its value wins, in both directions', () => {
  for (const name of Object.keys(DEFAULTS)) {
    applyFlags([row(name, false, true)])
    assert.equal(flagOn(name), false, `${name} turned off`)
    applyFlags([row(name, true, false)])
    assert.equal(flagOn(name), true, `${name} turned on`)
    applyFlags([row('other.flag', true)])                      // the PC doesn't list it: the phone's own default
    assert.equal(flagOn(name), DEFAULTS[name], `${name} not listed`)
  }
  applyFlags([row('x.new', true, false)])                      // a switch only the PC knows
  assert.equal(flagOn('x.new'), true)
})

test('refreshFlags asks the PC once, keeps what it says, and never throws', async () => {
  const calls = []
  globalThis.fetch = async (url) => { calls.push(url); return { ok: true, json: async () => [row('x.fix', false, true)] } }
  const rows = await refreshFlags()
  assert.deepEqual(calls, ['/api/flags'])
  assert.equal(rows.length, 1)
  assert.equal(flagOn('x.fix'), false)
  globalThis.fetch = async () => { throw new Error('offline') }
  assert.equal(await refreshFlags(), null)                     // the PC is away: the last answer stays
  assert.equal(flagOn('x.fix'), false)
  globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) })
  assert.equal(await refreshFlags(), null)
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ detail: 'not a list' }) })
  assert.equal(await refreshFlags(), null)                     // nonsense is not kept
  assert.equal(flagOn('x.fix'), false)
})

test('setFlag sends true, false or null (the default) and keeps the PC\'s new list', async () => {
  const sent = []
  globalThis.fetch = async (url, init) => {
    sent.push([url, init.method, JSON.parse(init.body)])
    const on = JSON.parse(init.body).on
    return { ok: true, text: async () => JSON.stringify([row('x.wait', on ?? false, false)]) }
  }
  await setFlag('x.wait', true)
  assert.equal(flagOn('x.wait'), true)
  await setFlag('x.wait', false)
  assert.equal(flagOn('x.wait'), false)
  const rows = await setFlag('x.wait', null)
  assert.deepEqual(sent.map((s) => s[2]), [{ on: true }, { on: false }, { on: null }])
  assert.ok(sent.every((s) => s[0] === '/api/flags/x.wait' && s[1] === 'PUT'))
  assert.equal(rows[0].name, 'x.wait')
  globalThis.fetch = async () => ({ ok: false, status: 404, text: async () => JSON.stringify({ detail: 'unknown flag' }) })
  await assert.rejects(() => setFlag('x.nope', true), /unknown flag/)
})

test('the list is grouped by the build group in the name, groups in number order, switches by name', () => {
  const groups = groupFlags([row('g10.b', true), row('g2.z', false), row('g2.a', true), row('g7.q', true), row('misc.one', true), row('plain', false)])
  assert.deepEqual(groups.map((g) => g.prefix), ['g2', 'g7', 'g10', 'misc', 'plain'])
  assert.deepEqual(groups[0].rows.map((r) => r.name), ['g2.a', 'g2.z'])
  assert.equal(groups[0].title, 'Whisper')
  assert.equal(groups[1].title, 'Phone')
  assert.equal(groups[3].title, 'misc')                         // a prefix nobody named: itself
  assert.deepEqual(groupFlags([]), [])
  assert.deepEqual(groupFlags(null), [])
})

test('what a row says: on or off, the default, and whether the owner changed it', () => {
  assert.deepEqual(describeFlag(row('a', true, true)), { now: 'On', default: 'On', changed: false, next: false, nextLabel: 'Turn off' })
  assert.deepEqual(describeFlag(row('a', false, true)), { now: 'Off', default: 'On', changed: true, next: true, nextLabel: 'Turn on' })
  assert.deepEqual(describeFlag(row('a', true, false)), { now: 'On', default: 'Off', changed: true, next: false, nextLabel: 'Turn off' })
  assert.equal(changedCount([row('a', true, true), row('b', false, true), row('c', true, false)]), 2)
  assert.equal(changedCount(null), 0)
})

test('the phone\'s own defaults are the PC\'s: same names, same values (server/app/flags.py)', () => {
  const src = readFileSync(new URL('../../server/app/flags.py', import.meta.url), 'utf8')
  const server = {}
  for (const m of src.matchAll(/^\s+"(g7\.[a-z0-9_]+)":\s*\((True|False),/gm)) server[m[1]] = m[2] === 'True'
  for (const k of Object.keys(TEST_DEFAULTS)) delete DEFAULTS[k]
  assert.deepEqual(DEFAULTS, server)
})
