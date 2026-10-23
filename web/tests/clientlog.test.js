// node --test: what the phone writes to the PC's log about itself (clientlog.js): the iOS and Safari versions.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { clientLog, device, flushClientLog } from '../src/clientlog.js'

function phone(ua, audioSession = 'play-and-record') {
  globalThis.window = globalThis
  Object.defineProperty(globalThis, 'navigator', { value: { userAgent: ua, audioSession: { type: audioSession } }, configurable: true })
}

// iOS freezes the OS token of the user agent (18_7) and puts the real Safari version in Version/.
const IPHONE_26 = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1'

test('device() carries the Safari Version/ token next to the iOS one', () => {
  phone(IPHONE_26)
  const d = device()
  assert.equal(d.ios, '18.7')
  assert.equal(d.safari, '26.1')
  assert.equal(d.audioSession, 'play-and-record')
})

test('device() has no Safari version when the user agent carries none (an installed web app may not)', () => {
  phone('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148')
  assert.equal(device().safari, null)
  phone('')
  assert.deepEqual([device().ios, device().safari], [null, null])
})

test('flushClientLog sends every queued event, 20 to a request', () => {
  phone(IPHONE_26)
  const bodies = []
  globalThis.fetch = (url, init) => { bodies.push(JSON.parse(init.body).events.length); return Promise.resolve({ ok: true }) }
  for (let i = 0; i < 45; i++) clientLog('t.event', { i })
  flushClientLog()
  assert.deepEqual(bodies, [20, 20, 5])
})
