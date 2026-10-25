// node --test: the service worker's push handler (public/push-sw.js), run against a stand-in for the worker's `self`.
// A push that carries its own `tag` (a timer: G10) is shown under that tag, so two timers that ring together stay two
// notifications; every other push still uses its title as the tag, as before. Nothing here talks to the network.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import vm from 'node:vm'

const SRC = readFileSync(new URL('../public/push-sw.js', import.meta.url), 'utf8')

function worker() {
  const handlers = {}
  const shown = []
  const self = {
    addEventListener: (type, fn) => { handlers[type] = fn },
    registration: { showNotification: (title, opts) => { shown.push({ title, opts }); return Promise.resolve() } },
    clients: { matchAll: () => Promise.resolve([]), openWindow: () => Promise.resolve(null) },
  }
  vm.runInNewContext(SRC, { self })
  return { handlers, shown }
}

async function push(w, payload) {
  const waits = []
  w.handlers.push({ data: { json: () => payload }, waitUntil: (p) => waits.push(p) })
  await Promise.all(waits)
}

test('a push without a tag is shown under its title, as before', async () => {
  const w = worker()
  await push(w, { title: 'Kairo', body: 'Approve the reply?', url: '/#approvals' })
  assert.equal(w.shown.length, 1)
  assert.equal(w.shown[0].title, 'Kairo')
  assert.equal(w.shown[0].opts.tag, 'Kairo')
  assert.equal(w.shown[0].opts.body, 'Approve the reply?')
  assert.equal(w.shown[0].opts.data.url, '/#approvals')
})

test('a timer push is shown under its own tag: two timers with the same title are two notifications', async () => {
  const w = worker()
  await push(w, { title: 'Timer', body: 'Your timer for 10 minutes is up (set 09:50).', url: '/', tag: 'timer-1' })
  await push(w, { title: 'Timer', body: 'Your timer for 10 minutes is up (set 09:50).', url: '/', tag: 'timer-2' })
  assert.deepEqual(w.shown.map((n) => n.opts.tag), ['timer-1', 'timer-2'])
})

test('a tag that is not a short text is ignored (the title stays the tag)', async () => {
  const w = worker()
  await push(w, { title: 'Timer', body: 'x', tag: { evil: 1 } })
  await push(w, { title: 'Timer', body: 'y', tag: 'a'.repeat(300) })
  await push(w, { title: 'Timer', body: 'z', tag: '' })
  assert.equal(w.shown[0].opts.tag, 'Timer')
  assert.ok(w.shown[1].opts.tag.length <= 64)
  assert.equal(w.shown[2].opts.tag, 'Timer')
})

test('a plain-text push still shows a notification', async () => {
  const w = worker()
  const waits = []
  w.handlers.push({ data: { json: () => { throw new Error('not json') } }, waitUntil: (p) => waits.push(p) })
  await Promise.all(waits)
  assert.equal(w.shown.length, 1)
  assert.equal(w.shown[0].title, 'Kairo')
})
