// node --test: the WebRTC line's measuring (rtclink.js). WebRTC itself only runs on the phone; what is
// tested here is when a turn starts (your last sound) and what it marks from the PC's events.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { makeRtcMeter } from '../src/rtclink.js'

function clock(t = 0) {
  return { now: () => t, set: (v) => { t = v } }
}

function fakeStart(log) {
  return (path, t0, info) => {
    const marks = {}
    const srv = {}
    const turn = {
      path, t0, info, marks, srv, outcome: null, reason: null, modelName: null,
      mark(name, at = log.clock.now()) { if (!(name in marks)) marks[name] = Math.round(at - t0) },
      has(name) { return name in marks },
      model(m) { turn.modelName = m },
      server(v) { Object.assign(srv, v) },
      finish(outcome, reason) { turn.outcome = outcome; turn.reason = reason ?? null; log.turns.push(turn) },
    }
    return turn
  }
}

test('a WebRTC turn: from your last sound to Kairo heard, with the PC steps and the path', () => {
  const log = { clock: clock(1000), turns: [] }
  const m = makeRtcMeter('call', { start: fakeStart(log), now: log.clock.now })
  m.network({ net: 'direct' })
  log.clock.set(1400)
  m.heard({ turn: 1, server: { vad_ms: 200, stt_ms: 120, turn_at_ms: 260 } }, 1000)   // you stopped at 1000
  m.words({ turn: 1 })
  log.clock.set(1450)
  m.timing({ turn: 1, server: { first_audio_at_ms: 330, cached_opener: true } })
  m.level(0.001)                                      // not yet heard
  log.clock.set(1520)
  m.level(0.2)
  m.rtt(18)
  m.done()
  const t = log.turns[0]
  assert.equal(t.outcome, 'played')
  assert.equal(t.info.line, 'webrtc')
  assert.equal(t.info.net, 'direct')
  assert.equal(t.marks.endpoint, 260)
  assert.equal(t.marks.audio_ready, 450)
  assert.equal(t.marks.playing, 520)
  assert.equal(t.srv.first_audio_at_ms, 330)
  assert.equal(t.srv.rtt_ms, 18)
})

test('events of another turn are ignored, and a turn that never played is a failure', () => {
  const log = { clock: clock(0), turns: [] }
  const m = makeRtcMeter('call', { start: fakeStart(log), now: log.clock.now })
  m.heard({ turn: 2, server: {} }, null)
  m.timing({ turn: 1, server: { first_audio_at_ms: 99 } })
  assert.equal(m.turn.has('audio_ready'), false)
  m.done()
  assert.equal(log.turns[0].outcome, 'failed')
  assert.equal(log.turns[0].t0, -300)               // no sound heard: 300 ms before the PC said so
})

test('talking on before Kairo was heard ends the turn as interrupted', () => {
  const log = { clock: clock(0), turns: [] }
  const m = makeRtcMeter('chat_voice', { start: fakeStart(log), now: log.clock.now })
  m.heard({ turn: 1, server: {} }, 0)
  m.heard({ turn: 2, server: {} }, 0)
  assert.equal(log.turns[0].outcome, 'interrupted')
  m.cut('talked over')
  assert.equal(log.turns[1].outcome, 'interrupted')
})
