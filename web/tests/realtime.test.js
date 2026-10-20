// node --test: the realtime line's measuring (realtime.js). WebRTC itself only runs on the phone;
// what is tested here is when a turn starts (your last sound) and what it marks.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QUIET_REASONS, makeLastVoice, makeLiveMeter } from '../src/realtime.js'

function clock(t = 0) {
  return { now: () => t, set: (v) => { t = v } }
}

function fakeStart(log) {
  return (path, t0) => {
    const marks = {}
    const turn = {
      path, t0, marks, outcome: null, reason: null, modelName: null,
      mark(name, at = log.clock.now()) { if (!(name in marks)) marks[name] = Math.round(at - t0) },
      has(name) { return name in marks },
      model(m) { turn.modelName = m },
      server() {},
      finish(outcome, reason) { turn.outcome = outcome; turn.reason = reason ?? null; log.turns.push(turn) },
    }
    return turn
  }
}

test('your last loud frame is when you stopped talking', () => {
  const lv = makeLastVoice()
  let t = 0
  for (let i = 0; i < 50; i++) lv.feed(0.001, (t += 20))     // a quiet room, 1 s
  assert.equal(lv.last, null)
  for (let i = 0; i < 25; i++) lv.feed(0.1, (t += 20))       // half a second of talking
  const spokeUntil = t
  for (let i = 0; i < 20; i++) lv.feed(0.001, (t += 20))     // quiet again
  assert.equal(lv.last, spokeUntil)
})

test('a realtime turn: endpoint, first words, audio, playing, all from your last sound', () => {
  const log = { clock: clock(), turns: [] }
  const m = makeLiveMeter('call_live', { start: fakeStart(log), now: log.clock.now })
  m.model('rt-2.1-mini')
  log.clock.set(1400)
  m.stopped(1000)                                            // OpenAI said you stopped; you went quiet at 1000
  log.clock.set(1500); m.words()
  log.clock.set(1550); m.audio()
  log.clock.set(1580); m.level(0.002)                         // not audible yet
  log.clock.set(1600); m.level(0.05)
  m.done()
  const [turn] = log.turns
  assert.equal(turn.path, 'call_live')
  assert.equal(turn.modelName, 'rt-2.1-mini')
  assert.deepEqual(turn.marks, { endpoint: 400, sent: 400, first_words: 500, audio_ready: 550, playing: 600 })
  assert.equal(turn.outcome, 'played')
})

test('a phone whose level reads nothing is timed by the audio event, and says so', () => {
  const log = { clock: clock(), turns: [] }
  const m = makeLiveMeter('chat_live', { start: fakeStart(log), now: log.clock.now })
  log.clock.set(1000); m.stopped(700)
  log.clock.set(1300); m.audio()
  log.clock.set(2500); m.done()
  assert.equal(log.turns[0].marks.playing, 600)
  assert.equal(log.turns[0].outcome, 'played')
  assert.match(log.turns[0].reason, /audio event/)
})

test('talking over Kairo or hanging up ends the turn as interrupted; an old last sound is not trusted', () => {
  const log = { clock: clock(), turns: [] }
  const m = makeLiveMeter('call_live', { start: fakeStart(log), now: log.clock.now, silenceMs: 400 })
  log.clock.set(10000); m.stopped(1000)                      // 9 s ago: not this turn
  assert.equal(log.turns.length, 0)
  m.audio()
  m.cut('talked over')
  assert.equal(log.turns[0].t0, 9600)
  assert.equal(log.turns[0].outcome, 'interrupted')
  log.clock.set(12000); m.stopped(11800)
  m.end('hung up')
  assert.equal(log.turns[1].outcome, 'no_speech')            // nothing came back before you hung up
})

test('the call screen stays quiet when local voice is a choice, and speaks up when something is wrong', () => {
  for (const r of ['local', 'off', 'no_key']) assert.ok(QUIET_REASONS.includes(r), r)
  for (const r of ['cap', 'down', 'api', 'sideband', 'paused', 'updating', 'webrtc']) assert.ok(!QUIET_REASONS.includes(r), r)
})
