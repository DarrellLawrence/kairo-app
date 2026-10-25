// node --test: the WebRTC line's measuring (rtclink.js). WebRTC itself only runs on the phone; what is
// tested here is when a turn starts (your last sound) and what it marks from the PC's events.
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { applyFlags, flagOn, resetFlags } from '../src/flags.js'
import { makeRtcMeter, readStats } from '../src/rtclink.js'
import { FakePeer, flushLogs, installPhone, logged } from './phone-fakes.js'

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
const wait = (ms) => new Promise((r) => setTimeout(r, ms))
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

/* What the PC's transport sends while Kairo is not speaking: silent packets (pipecat's audio_out_auto_silence is on), so the
   inbound counters climb from the moment the line connects. */
const SILENT_PACKETS = { type: 'inbound-rtp', kind: 'audio', packetsReceived: 50, packetsLost: 0, jitterBufferDelay: 2.5, jitterBufferEmittedCount: 50 }

test('g7.call_logs on: packets flowing is not a sound: while Kairo has said nothing there is no rtc.audio line', async () => {
  const { line, rec } = await rtcLine()
  try {
    FakePeer.last.stats = [SILENT_PACKETS]
    await wait(1400)                                       // two stats ticks, packets received, the remote level is 0
    assert.deepEqual(logged(rec, 'rtc.audio'), [])
  } finally {
    line.close()
  }
})

test('g7.call_logs on: one rtc.audio line per call, when Kairo\'s voice is first heard on this phone, with the audio element and the inbound audio', async () => {
  const { line, rec } = await rtcLine()
  try {
    FakePeer.last.stats = [{ type: 'inbound-rtp', kind: 'audio', packetsReceived: 240, packetsLost: 3, jitter: 0.004, jitterBufferDelay: 3, jitterBufferEmittedCount: 100 }]
    await wait(1300)                                       // the stats tick that learns the counters
    assert.deepEqual(logged(rec, 'rtc.audio'), [])         // still silence
    rec.remoteLevel = 0.2                                  // Kairo speaks
    await wait(300)
    const [one, ...more] = logged(rec, 'rtc.audio')
    assert.equal(more.length, 0)
    assert.equal(one.path, 'call')
    assert.equal(one.when, 'first-sound')
    assert.deepEqual(one.audio, { muted: false, paused: false, hasStream: true })   // the connection's own stream, playing
    assert.deepEqual(one.inbound, { received: 240, lost: 3, jitter_ms: 30 })
    rec.remoteLevel = 0
    await wait(100)
    rec.remoteLevel = 0.3                                  // and again: still once per call
    await wait(300)
  } finally {
    line.close()
  }
  assert.equal(logged(rec, 'rtc.audio').length, 1)
})

test('g7.call_logs on: a hang-up always writes rtc.audio_end: how long, whether Kairo was ever heard, the last packets, loss and jitter', async () => {
  const { line, rec } = await rtcLine()
  FakePeer.last.stats = [SILENT_PACKETS]
  await wait(1300)
  FakePeer.last.stats = [{ ...SILENT_PACKETS, packetsReceived: 5000, packetsLost: 120, jitterBufferDelay: 400, jitterBufferEmittedCount: 5000 }]
  await wait(1400)                                         // a long call's counters have reached the phone
  rec.remoteLevel = 0.2
  await wait(300)
  line.close()
  const [end, ...more] = logged(rec, 'rtc.audio_end')
  assert.equal(more.length, 0)
  assert.equal(end.heard, true)
  assert.equal(end.path, 'call')
  assert.ok(end.secs >= 2 && end.secs < 30, `secs ${end.secs}`)
  assert.deepEqual(end.inbound, { received: 5000, lost: 120, jitter_ms: 80 })
  assert.equal(typeof end.audio.muted, 'boolean')
  line.close()
  assert.equal(logged(rec, 'rtc.audio_end').length, 1)    // hanging up twice is one line
})

test('g7.call_logs on: a call that never heard Kairo says so at hang-up, so the silence is on record with the packets that did arrive', async () => {
  const { line, rec } = await rtcLine()
  FakePeer.last.stats = [SILENT_PACKETS]
  await wait(1300)
  line.close()
  assert.deepEqual(logged(rec, 'rtc.audio'), [])
  const [end] = logged(rec, 'rtc.audio_end')
  assert.equal(end.heard, false)
  assert.equal(end.inbound.received, 50)                   // packets came, no sound: the finding
})

test('g7.call_logs on: a call that never connected writes no rtc.audio_end (there was no audio to describe)', async () => {
  const fake = installPhone()
  const real = globalThis.fetch
  globalThis.fetch = async (url, init) => (url === '/api/rtc/offer' ? { ok: false, json: async () => ({ detail: 'busy' }) } : real(url, init))
  const { startRtc } = await import('../src/rtclink.js')
  const line = startRtc({ path: 'call', on: { fallback() {} } })
  assert.equal(await line.ready, false)
  line.close()
  assert.deepEqual(logged(fake.rec, 'rtc.audio_end'), [])
  assert.deepEqual(logged(fake.rec, 'rtc.audio'), [])
})

test('g7.call_logs off: none of those lines (the usual short log stays)', async () => {
  applyFlags([flag('g7.call_logs', false)])
  const { line, rec, micTrack } = await rtcLine()
  micTrack.fire('mute')
  rec.remoteLevel = 0.2                                    // Kairo is heard: still nothing logged
  await wait(300)
  line.close()
  for (const name of ['mic.settings', 'mic.track', 'audio.session', 'rtc.audio', 'rtc.audio_end']) assert.deepEqual(logged(rec, name), [], name)
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
// Off by default. The PC sends {"type":"barge_in"} now (the turn-taking build, g6): whenever it cuts Kairo off. The owner
// switches this on after the iPhone echo check (P12) and a few calls with the g6 switches.

/* The switch turned on (its default is off). */
const followPc = () => applyFlags([flag('g7.listen_on_barge_in', true, false)])

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

/* A PC that never sends barge_in (one from before the turn-taking build): the whole answer is written at once (text is far
   faster than speech), then you talk over it; the PC stops its voice and sends `speech` (its VAD), later `spoken`. What the
   call screen and the turn log say. */
async function talkedOverToOldPc() {
  const states = []
  let turns
  await withLine({ state: (s) => states.push(s) }, {}, async ({ dc, rec }) => {
    dc.pc({ type: 'transcript', text: 'Tell me a long story', turn: 1, server: {} })
    dc.pc({ type: 'say', text: 'Once upon a time.', turn: 1 })
    dc.pc({ type: 'timing', turn: 1, server: {} })
    dc.pc({ type: 'done', text: 'Once upon a time. And then.', interrupted: false, model: 'm', server: {} })
    rec.micLevel = 0.001
    await wait(450)
    rec.micLevel = 0.1                                // you really talk over Kairo
    await wait(350)
    dc.pc({ type: 'speech' })
    await wait(100)
    turns = rec.posted.filter(([u]) => u === '/api/voice/turns').map(([, b]) => [b.outcome, b.reason])   // before the hang-up ends the turn
  })
  return { states, turns }
}

test('g7.listen_on_barge_in is off by default, on the phone before the PC has answered and after', () => {
  assert.equal(flagOn('g7.listen_on_barge_in'), false)
  applyFlags([])
  assert.equal(flagOn('g7.listen_on_barge_in'), false)
  followPc()
  assert.equal(flagOn('g7.listen_on_barge_in'), true)
})

test('g7.listen_on_barge_in on: a loud sound on the mic does not flip the screen while Kairo goes on talking', () => {
  followPc()
  return speakingThenNoise(({ states }) => assert.equal(states.at(-1), 'speaking'))
})

test('g7.listen_on_barge_in on: the PC\'s barge_in event is what switches the screen to listening, and ends the turn as talked over', () => {
  followPc()
  return speakingThenNoise(({ dc, states }) => {
    dc.pc({ type: 'barge_in' })
    assert.equal(states.at(-1), 'listening')
    dc.pc({ type: 'barge_in' })                       // a second one changes nothing
    assert.deepEqual(states.filter((s) => s === 'listening').length, 2)   // connecting>listening at the start, and this one
  })
})

test('g7.listen_on_barge_in on: an answer the PC reports as interrupted also ends "speaking"', () => {
  followPc()
  return speakingThenNoise(({ dc, states }) => {
    dc.pc({ type: 'done', text: 'You have two', interrupted: true, model: 'm', server: {} })
    assert.equal(states.at(-1), 'listening')
  })
})

test('g7.listen_on_barge_in on: a barge_in after an answer that is already written ends the turn as talked over', async () => {
  followPc()
  const states = []
  let turns
  await withLine({ state: (s) => states.push(s) }, {}, async ({ dc, rec }) => {
    dc.pc({ type: 'transcript', text: 'Tell me a long story', turn: 1, server: {} })
    dc.pc({ type: 'say', text: 'Once upon a time.', turn: 1 })
    dc.pc({ type: 'timing', turn: 1, server: {} })
    dc.pc({ type: 'done', text: 'Once upon a time. And then.', interrupted: false, model: 'm', server: {} })
    rec.micLevel = 0.001
    await wait(450)
    rec.micLevel = 0.1                                // a cough: the mic is loud, the PC does not cut Kairo off
    await wait(350)
    assert.equal(states.at(-1), 'speaking')
    dc.pc({ type: 'barge_in' })                       // you really talk over Kairo: the PC says so
    assert.equal(states.at(-1), 'listening')
    turns = rec.posted.filter(([u]) => u === '/api/voice/turns').map(([, b]) => [b.outcome, b.reason])
  })
  assert.deepEqual(turns, [['interrupted', 'talked over']])
})

test('g7.listen_on_barge_in on: with an old PC that sends no barge_in the screen stays on "Speaking" until the PC says its voice stopped', async () => {
  followPc()
  const { states, turns } = await talkedOverToOldPc()
  assert.equal(states.at(-1), 'speaking')
  assert.deepEqual(turns, [])                         // the turn is not finished yet either: it is "played" once `spoken` comes
})

test('g7.listen_on_barge_in on: a normal end of an answer and a barge_in with nothing speaking change nothing', async () => {
  followPc()
  const states = []
  await withLine({ state: (s) => states.push(s) }, {}, ({ dc }) => {
    dc.pc({ type: 'barge_in' })                       // listening already: nothing to cut
    assert.deepEqual(states, ['listening'])
    dc.pc({ type: 'transcript', text: 'Hi', turn: 1, server: {} })
    dc.pc({ type: 'done', text: 'Hello', interrupted: false, model: 'm', server: {} })
    assert.equal(states.at(-1), 'thinking')           // not 'listening' until its voice has played (the old rule)
  })
})

test('g7.listen_on_barge_in off (the default): the old way, any loud sound on the phone flips the screen and is counted as talked over, and barge_in is not listened to', async () => {
  await speakingThenNoise(({ states }) => assert.equal(states.at(-1), 'listening'))
  const { states, turns } = await talkedOverToOldPc()
  assert.equal(states.at(-1), 'listening')            // the screen leaves "Speaking" as soon as you talk
  assert.deepEqual(turns, [['interrupted', 'talked over']])   // and Voice speed counts the barge-in
  const seen = []
  await withLine({ state: (s) => seen.push(s) }, {}, ({ dc }) => {
    dc.pc({ type: 'transcript', text: 'What is on today?', turn: 1, server: {} })
    dc.pc({ type: 'say', text: 'x', turn: 1 })
    dc.pc({ type: 'barge_in' })
    assert.equal(seen.at(-1), 'speaking')
  })
})

// ---------------------------------------------------------------- the stats the call reads once a second

test('readStats: the selected path (round trip, candidate types) and the inbound audio (packets, loss, jitter)', () => {
  const stats = new Map([
    ['t', { id: 't', type: 'transport', selectedCandidatePairId: 'p' }],
    ['p', { id: 'p', type: 'candidate-pair', currentRoundTripTime: 0.0184, localCandidateId: 'l', remoteCandidateId: 'r' }],
    ['l', { id: 'l', type: 'local-candidate', candidateType: 'host' }],
    ['r', { id: 'r', type: 'remote-candidate', candidateType: 'host' }],
    ['a', { id: 'a', type: 'inbound-rtp', kind: 'audio', packetsReceived: 500, packetsLost: 2, jitterBufferDelay: 5, jitterBufferEmittedCount: 100 }],
    ['v', { id: 'v', type: 'inbound-rtp', kind: 'video', packetsReceived: 9 }],
  ])
  assert.deepEqual(readStats(stats), {
    path: { rtt_ms: 18, local: 'host', remote: 'host', jitter_ms: 50 },
    inbound: { received: 500, lost: 2, jitter_ms: 50 },
  })
  assert.deepEqual(readStats(new Map()), { path: null, inbound: null })
  const noJitterYet = readStats(new Map([['a', { id: 'a', type: 'inbound-rtp', kind: 'audio', packetsReceived: 3 }]]))
  assert.deepEqual(noJitterYet, { path: null, inbound: { received: 3, lost: 0, jitter_ms: null } })
})

test('g7.call_logs: every line fits what the PC keeps of one (600 characters) with real iOS values', async () => {
  const { line, rec, micTrack } = await rtcLine({}, { ua: UA, settings: { echoCancellation: false, sampleRate: 48000, volume: 1 } })
  micTrack.fire('mute')
  FakePeer.last.stats = [{ type: 'inbound-rtp', kind: 'audio', packetsReceived: 240, packetsLost: 3, jitterBufferDelay: 3, jitterBufferEmittedCount: 100 }]
  await new Promise((r) => setTimeout(r, 1300))
  rec.remoteLevel = 0.2
  await new Promise((r) => setTimeout(r, 300))
  line.close()
  flushLogs()
  const mine = rec.logs.filter((e) => /^(mic|audio|rtc)\./.test(e.event))
  for (const name of ['mic.settings', 'mic.track', 'audio.session', 'rtc.warn', 'rtc.audio', 'rtc.audio_end']) assert.ok(mine.some((e) => e.event === name), `${name} among ${mine.map((e) => e.event).join(' ')}`)
  for (const e of mine) assert.ok(JSON.stringify(e.data).length <= 600, `${e.event}: ${JSON.stringify(e.data).length}`)
})
