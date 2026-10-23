// node --test: the WebRTC line's measuring (rtclink.js). WebRTC itself only runs on the phone; what is
// tested here is when a turn starts (your last sound) and what it marks from the PC's events.
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { applyFlags, resetFlags } from '../src/flags.js'
import { makeRtcMeter } from '../src/rtclink.js'
import { FakePeer, installPhone, logged } from './phone-fakes.js'

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

test('a turn ended by your tap is timed from the tap and recorded as a tap', () => {
  const log = { clock: clock(0), turns: [] }
  const m = makeRtcMeter('call', { start: fakeStart(log), now: log.clock.now })
  log.clock.set(3300)
  m.heard({ turn: 1, ended: 'tap', server: { vad_ms: 0, turn_at_ms: 90 } }, 3200, 3000)   // a loud room: "your last sound" is now
  m.end('hung up')
  const t = log.turns[0]
  assert.equal(t.t0, 3000)
  assert.equal(t.info.ended, 'tap')
  assert.equal(t.marks.endpoint, 90)
  m.heard({ turn: 2, server: {} }, 3200, 3000)        // the pause ended this one: the tap's time doesn't count
  assert.equal(m.turn.t0, 3200)
  assert.equal(m.turn.info.ended, undefined)
})

// ---------------------------------------------------------------- finish(): "I'm done" on the WebRTC line
// The stand-ins (phone-fakes.js): a mic, Kairo's AudioContext, the peer connection with its data channel
// (what the phone sends is kept; the test plays the PC's events).

/* A call for the length of fn; always hung up, so a failing check can't leave the timers running.
   options: settings = what the mic track's getSettings() says; path = call or chat_voice; ua. */
async function withLine(on, phone, fn) {
  const made = await rtcLine(on, phone)
  try { await fn(made) } finally { made.line.close() }
}

/* options: see withLine. */
async function rtcLine(on = {}, { path = 'call', ...phone } = {}) {
  const fake = installPhone(phone)
  const { startRtc } = await import('../src/rtclink.js')
  const line = startRtc({ path, on })
  assert.equal(await line.ready, true)
  return { line, dc: FakePeer.last.dc, audio: globalThis.Audio.last, ...fake }
}

test('finish() sends "done" and says true only while the PC hears a turn of yours', async () => {
  const hearing = []
  const { line, dc } = await rtcLine({ hearing: (h) => hearing.push(h) })
  try {
    assert.equal(line.finish(), false)                 // nothing heard yet: the tap is a pause, as before
    assert.equal(dc.done(), 0)
    dc.pc({ type: 'speech' })                          // the PC's VAD hears you
    assert.equal(line.finish(), true)
    assert.equal(dc.done(), 1)
    dc.pc({ type: 'transcript', text: 'Remind me to call mum', turn: 1, ended: 'tap', server: {} })
    assert.equal(line.finish(), false)                 // the turn is over: nothing to end
    dc.pc({ type: 'heard', text: 'and the dentist', language: 'en' })   // words at a pause: a turn going on
    assert.equal(line.finish(), true)
    assert.equal(dc.done(), 2)
    dc.pc({ type: 'ignored', reason: 'not_you' })      // not you (speaker check): no turn of yours
    assert.equal(line.finish(), false)
    dc.pc({ type: 'speech' })
    dc.pc({ type: 'quiet' })                           // only noise, nothing written down
    assert.equal(line.finish(), false)
    dc.pc({ type: 'speech' })
    line.pause()
    assert.equal(line.finish(), false)                 // paused: the mic is off
    assert.equal(dc.done(), 2)
    assert.deepEqual(hearing, [true, false, true, false, true, false, true, false])
  } finally {
    line.close()
  }
})

// ---------------------------------------------------------------- g7.mic_constraints: what the mic is asked for

const flag = (name, on, def = true) => ({ name, on, default: def, what: '' })
afterEach(() => resetFlags())

test('g7.mic_constraints on: the call asks for exactly echo cancelling, noise suppression and automatic gain', async () => {
  const { line, rec } = await rtcLine()
  line.close()
  assert.deepEqual(rec.gum, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }])
})

test('g7.mic_constraints off: the old request, echo cancelling and noise suppression only', async () => {
  applyFlags([flag('g7.mic_constraints', false)])
  const { line, rec } = await rtcLine()
  line.close()
  assert.deepEqual(rec.gum, [{ audio: { echoCancellation: true, noiseSuppression: true } }])
})

// ---------------------------------------------------------------- g7.call_logs: what the PC's log learns about a call's audio

const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1'

test('g7.call_logs on: the mic settings once per call, what was asked next to what the phone gave', () => withLine({}, { ua: UA }, ({ rec }) => {
  const [one, ...more] = logged(rec, 'mic.settings')
  assert.equal(more.length, 0)
  assert.deepEqual(one.asked, { echoCancellation: true, noiseSuppression: true, autoGainControl: true })
  assert.deepEqual(one.settings, { echoCancellation: true, sampleRate: 48000, volume: 1 })   // the one setting iOS honours
  assert.deepEqual(one.supported, { echoCancellation: true, noiseSuppression: true, autoGainControl: true })
  assert.equal(one.path, 'call')
  assert.equal(one.safari, '26.1')
  assert.equal(one.ios, '18.7')
}))

test('g7.call_logs on: a mock track without getSettings is fine, the log says nothing was readable', () => withLine({}, { settings: null }, ({ rec }) => {
  assert.deepEqual(logged(rec, 'mic.settings').map((x) => x.settings), [null])
}))

test('g7.call_logs on: the mic track going silent, coming back or ending is logged', () => withLine({}, {}, ({ rec, micTrack }) => {
  micTrack.fire('mute')
  micTrack.fire('unmute')
  micTrack.fire('ended')
  assert.deepEqual(logged(rec, 'mic.track').map((x) => x.event), ['mute', 'unmute', 'ended'])
}))

test('g7.call_logs on: every audio-session change the page makes is logged, from and to', async () => {
  const { line, rec } = await rtcLine()
  line.close()
  const changes = logged(rec, 'audio.session').map((x) => `${x.from}>${x.to}`)
  assert.deepEqual(changes, ['auto>play-and-record', 'play-and-record>playback'])
})

test('g7.call_logs on: one rtc.audio line per call, with the audio element and the inbound audio, when sound first arrives', async () => {
  const { line, rec } = await rtcLine()
  try {
    assert.deepEqual(logged(rec, 'rtc.audio'), [])         // nothing yet: no sound has come in (logged() keeps all so far)
    FakePeer.last.stats = [{ type: 'inbound-rtp', kind: 'audio', packetsReceived: 240, packetsLost: 3, jitter: 0.004, jitterBufferDelay: 3, jitterBufferEmittedCount: 100 }]
    await new Promise((r) => setTimeout(r, 1300))          // the next stats tick
    const [one, ...more] = logged(rec, 'rtc.audio')
    assert.equal(more.length, 0)
    assert.equal(one.when, 'first-audio')
    assert.deepEqual(one.audio, { muted: false, paused: false, hasStream: true })   // the connection's own stream, playing
    assert.deepEqual(one.inbound, { received: 240, lost: 3, jitter_ms: 30 })
  } finally {
    line.close()
  }
  assert.equal(logged(rec, 'rtc.audio').length, 1)         // once per call: the hang-up adds nothing
})

test('g7.call_logs on: a call that never heard Kairo logs rtc.audio at hang-up, so the silence is on record', async () => {
  const { line, rec } = await rtcLine()
  line.close()
  const [one] = logged(rec, 'rtc.audio')
  assert.equal(one.when, 'close')
  assert.equal(one.inbound, null)
})

test('g7.call_logs off: none of those lines (the usual short log stays)', async () => {
  applyFlags([flag('g7.call_logs', false)])
  const { line, rec, micTrack } = await rtcLine()
  micTrack.fire('mute')
  line.close()
  for (const name of ['mic.settings', 'mic.track', 'audio.session', 'rtc.audio']) assert.deepEqual(logged(rec, name), [], name)
  assert.equal(logged(rec, 'rtc.start').length, 1)         // what was always logged still is
})

// ---------------------------------------------------------------- g7.no_aec_warning: the phone says echo cancelling is off

test('g7.no_aec_warning on: a mic that reports echo cancelling off warns the call screen once', async () => {
  const warns = []
  await withLine({ warn: (w) => warns.push(w) }, { settings: { echoCancellation: false, sampleRate: 48000 } }, ({ rec }) => {
    assert.equal(warns.length, 1)
    assert.equal(warns[0].reason, 'no-aec')
    assert.match(warns[0].message, /echo/i)
    assert.equal(logged(rec, 'rtc.warn').length, 1)                      // and it is on record
  })
})

test('g7.no_aec_warning on: no warning when echo cancelling is on, unknown, or the track can\'t say', async () => {
  for (const settings of [{ echoCancellation: true }, { sampleRate: 48000 }, null]) {
    const warns = []
    await withLine({ warn: (w) => warns.push(w) }, { settings }, () => {})
    assert.deepEqual(warns, [], JSON.stringify(settings))
  }
})

test('g7.no_aec_warning off: no warning even when echo cancelling is off', async () => {
  applyFlags([flag('g7.no_aec_warning', false)])
  const warns = []
  await withLine({ warn: (w) => warns.push(w) }, { settings: { echoCancellation: false } }, () => {})
  assert.deepEqual(warns, [])
})

// ---------------------------------------------------------------- g7.listen_on_barge_in: the screen follows the PC, not the mic

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

/* A call in which Kairo is speaking turn 1, then a loud sound on the mic (a cough, a "mhm", a real interruption). */
async function speakingThenNoise(fn) {
  const states = []
  await withLine({ state: (s) => states.push(s) }, {}, async ({ dc, rec }) => {
    dc.pc({ type: 'transcript', text: 'What is on today?', turn: 1, server: {} })
    dc.pc({ type: 'say', text: 'You have two things.', turn: 1 })
    assert.equal(states.at(-1), 'speaking')
    rec.micLevel = 0.001
    await wait(450)                                   // the room is quiet: the phone learns its floor
    rec.micLevel = 0.1
    await wait(350)                                   // a loud sound on the mic
    await fn({ dc, rec, states })
  })
}

test('g7.listen_on_barge_in on: a loud sound on the mic does not flip the screen while Kairo goes on talking', () =>
  speakingThenNoise(({ states }) => assert.equal(states.at(-1), 'speaking')))

test('g7.listen_on_barge_in on: the PC\'s barge_in event is what switches the screen to listening, and ends the turn as talked over', () =>
  speakingThenNoise(({ dc, states }) => {
    dc.pc({ type: 'barge_in' })
    assert.equal(states.at(-1), 'listening')
    dc.pc({ type: 'barge_in' })                       // a second one changes nothing
    assert.deepEqual(states.filter((s) => s === 'listening').length, 2)   // connecting>listening at the start, and this one
  }))

test('g7.listen_on_barge_in on: an answer the PC reports as interrupted also ends "speaking" (the PC that sends no barge_in yet)', () =>
  speakingThenNoise(({ dc, states }) => {
    dc.pc({ type: 'done', text: 'You have two', interrupted: true, model: 'm', server: {} })
    assert.equal(states.at(-1), 'listening')
  }))

test('g7.listen_on_barge_in on: a normal end of an answer and a barge_in with nothing speaking change nothing', async () => {
  const states = []
  await withLine({ state: (s) => states.push(s) }, {}, ({ dc }) => {
    dc.pc({ type: 'barge_in' })                       // listening already: nothing to cut
    assert.deepEqual(states, ['listening'])
    dc.pc({ type: 'transcript', text: 'Hi', turn: 1, server: {} })
    dc.pc({ type: 'done', text: 'Hello', interrupted: false, model: 'm', server: {} })
    assert.equal(states.at(-1), 'thinking')           // not 'listening' until its voice has played (the old rule)
  })
})

test('g7.listen_on_barge_in off: the old way, any loud sound on the phone flips the screen, and barge_in is not listened to', async () => {
  applyFlags([flag('g7.listen_on_barge_in', false)])
  await speakingThenNoise(({ states }) => assert.equal(states.at(-1), 'listening'))
  const states = []
  await withLine({ state: (s) => states.push(s) }, {}, ({ dc }) => {
    dc.pc({ type: 'transcript', text: 'What is on today?', turn: 1, server: {} })
    dc.pc({ type: 'say', text: 'x', turn: 1 })
    dc.pc({ type: 'barge_in' })
    assert.equal(states.at(-1), 'speaking')
  })
})
