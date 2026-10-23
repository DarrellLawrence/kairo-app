// node --test: the WebRTC line's measuring (rtclink.js). WebRTC itself only runs on the phone; what is
// tested here is when a turn starts (your last sound) and what it marks from the PC's events.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { applyFlags, resetFlags } from '../src/flags.js'
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
// Browser stand-ins, just enough for startRtc to connect: a mic, Kairo's AudioContext, the peer connection
// with its data channel (what the phone sends is kept; the test plays the PC's events).

class FakeChannel {
  constructor() { this.readyState = 'open'; this.sent = [] }
  send(data) { this.sent.push(data[0] === '{' ? JSON.parse(data) : data) }
  pc(event) { this.onmessage?.({ data: JSON.stringify(event) }) }   // an event from the PC
  done() { return this.sent.filter((m) => m?.type === 'done').length }
}
class FakePeer {
  constructor() { FakePeer.last = this; this.connectionState = 'connected'; this.iceGatheringState = 'complete' }
  addTransceiver() {}
  createDataChannel() { this.dc = new FakeChannel(); return this.dc }
  async createOffer() { return { type: 'offer', sdp: 'v=0\r\n' } }
  async setLocalDescription(d) { this.localDescription = d }
  async setRemoteDescription() { setTimeout(() => this.dc.onopen?.(), 0) }
  async getStats() { return new Map() }
  close() { this.connectionState = 'closed' }
}
class FakeAudioContext {
  constructor() { this.state = 'running'; this.sampleRate = 48000; this.destination = {} }
  resume() { return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
  createAnalyser() { return { fftSize: 512, connect() {}, getFloatTimeDomainData: (b) => b.fill(0) } }
  createMediaStreamSource() { return { connect() {} } }
  createBuffer() { return {} }
  createBufferSource() { return { connect() {}, start() {} } }
}
class FakeAudio {
  constructor() { FakeAudio.last = this; this.muted = false }
  play() { return Promise.resolve() }
  pause() {}
  setAttribute() {}
}

// What iOS really reports for a call's mic (WebKit bug 326286, comment 8): the one setting it honours.
const IOS_SETTINGS = { echoCancellation: true, sampleRate: 48000, volume: 1 }

/* options: settings = what the mic track's getSettings() says; gum = collects the getUserMedia constraints. */
async function rtcLine(on = {}, { settings = IOS_SETTINGS, gum = null, path = 'call' } = {}) {
  const micTrack = { enabled: true, readyState: 'live', stop() { this.readyState = 'ended' }, getSettings: () => settings }
  globalThis.window = globalThis
  Object.defineProperty(globalThis, 'navigator', {
    value: {
      userAgent: 'test',
      mediaDevices: {
        getUserMedia: async (constraints) => { gum?.push(constraints); return { getTracks: () => [micTrack], getAudioTracks: () => [micTrack] } },
      },
    },
    configurable: true,
  })
  globalThis.AudioContext = FakeAudioContext
  globalThis.RTCPeerConnection = FakePeer
  globalThis.Audio = FakeAudio
  globalThis.fetch = async (url) => ({ ok: true, json: async () => (url === '/api/rtc/offer' ? { sdp: 'v=0\r\n', type: 'answer' } : {}) })
  const { startRtc } = await import('../src/rtclink.js')
  const line = startRtc({ path, on })
  assert.equal(await line.ready, true)
  return { line, dc: FakePeer.last.dc, audio: FakeAudio.last }
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

test('g7.mic_constraints on: the call asks for exactly echo cancelling, noise suppression and automatic gain', async () => {
  const gum = []
  const { line } = await rtcLine({}, { gum })
  line.close()
  assert.deepEqual(gum, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }])
})

test('g7.mic_constraints off: the old request, echo cancelling and noise suppression only', async () => {
  applyFlags([flag('g7.mic_constraints', false)])
  try {
    const gum = []
    const { line } = await rtcLine({}, { gum })
    line.close()
    assert.deepEqual(gum, [{ audio: { echoCancellation: true, noiseSuppression: true } }])
  } finally {
    resetFlags()
  }
})
