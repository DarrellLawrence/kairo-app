// node --test: the sound check's call test over the real call line (soundcheck.js, g7.sound_check_rtc): the PC says one
// fixed line over WebRTC, the way Kairo's voice really reaches you on a call.
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { applyFlags, resetFlags } from '../src/flags.js'
import { CALL_TEST_TEXT, startCallCheck } from '../src/soundcheck.js'

afterEach(() => resetFlags())
const flag = (on) => [{ name: 'g7.sound_check_rtc', on, default: true, what: '' }]

/* A stand-in for startRtc: remembers how it was started and what it was told; the test plays the line's events. */
function fakeRtc({ ready = true } = {}) {
  const log = { started: [], said: [], tapped: 0, closed: 0 }
  const start = (opts) => {
    log.started.push(opts)
    log.on = opts.on
    return {
      ready: Promise.resolve(ready),
      say: (t) => log.said.push(t),
      tapToPlay: () => { log.tapped += 1; return true },
      close: () => { log.closed += 1 },
    }
  }
  return { start, log }
}
const deps = (over = {}) => ({ supported: () => true, status: async () => ({ available: true }), ...over })

test('g7.sound_check_rtc on: the call test starts the call line, has the PC say the line, and finishes when its voice has played', async () => {
  const { start, log } = fakeRtc()
  const check = await startCallCheck(deps({ start }))
  assert.ok(check)
  assert.equal(log.started[0].path, 'chat_voice')              // the voice button's line: no call is recorded
  const done = check.say()
  assert.deepEqual(log.said, [CALL_TEST_TEXT])
  log.on.answered()                                            // the PC's voice went quiet
  assert.equal(await done, 'spoken')
  check.close()
  assert.equal(log.closed, 1)
})

test('g7.sound_check_rtc on: the line can be said again (after "Tap to play")', async () => {
  const { start, log } = fakeRtc()
  const check = await startCallCheck(deps({ start }))
  const first = check.say()
  log.on.answered()
  await first
  assert.equal(check.tapToPlay(), true)
  const again = check.say()
  log.on.answered()
  assert.equal(await again, 'spoken')
  assert.equal(log.said.length, 2)
  assert.equal(log.tapped, 1)
})

test('g7.sound_check_rtc on: a voice that never ends times out, and a line that falls back says so', async () => {
  const a = fakeRtc()
  const check = await startCallCheck(deps({ start: a.start, timeoutMs: 20 }))
  assert.equal(await check.say(), 'timeout')
  const b = fakeRtc()
  const other = await startCallCheck(deps({ start: b.start }))
  const done = other.say()
  b.log.on.fallback({ reason: 'webrtc' })
  assert.equal(await done, 'fallback')
})

test('g7.sound_check_rtc on: where the call line is not possible, the old test is used (null)', async () => {
  const { start, log } = fakeRtc()
  assert.equal(await startCallCheck(deps({ start, supported: () => false })), null)
  assert.equal(await startCallCheck(deps({ start, status: async () => ({ available: false, reason: 'no model' }) })), null)
  assert.equal(await startCallCheck(deps({ start, status: async () => null })), null)
  assert.equal(log.started.length, 0)
  const refused = fakeRtc({ ready: false })                    // the line itself fell over (no mic, no answer)
  assert.equal(await startCallCheck(deps({ start: refused.start })), null)
  assert.equal(refused.log.closed, 1)                          // and nothing is left open
})

test('g7.sound_check_rtc off: always the old test, the PC is not even asked', async () => {
  applyFlags(flag(false))
  let asked = 0
  const { start, log } = fakeRtc()
  assert.equal(await startCallCheck(deps({ start, status: async () => { asked += 1; return { available: true } } })), null)
  assert.equal(asked, 0)
  assert.equal(log.started.length, 0)
})
