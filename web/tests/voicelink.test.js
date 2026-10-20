// node --test: the voice line (voicelink.js). The browser parts are stand-ins: a fake mic that
// we feed loud or quiet 20 ms frames, a fake AudioContext whose sources "play" until we end them,
// a fake WebSocket that records what the phone sends and lets the test answer as the PC.
import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'

// ---------------------------------------------------------------- browser stand-ins
const sentTurns = []
let ctxState = 'running'
const contexts = []

class FakeNode {
  constructor(ctx) { this.ctx = ctx }
  connect() {}
  disconnect() {}
}
class FakeSource extends FakeNode {
  constructor(ctx) { super(ctx); this.started = null; this.stopped = false }
  start(at = 0) { this.started = at; this.ctx.sources.push(this) }
  stop() { this.stopped = true }
  end() { this.onended?.() }   // the test says: this one finished playing
}
class FakeContext {
  constructor() {
    this.state = ctxState
    this.sampleRate = 48000
    this.currentTime = 0
    this.sources = []
    this.destination = {}
    contexts.push(this)
  }
  resume() { this.state = ctxState; return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
  createAnalyser() { const n = new FakeNode(this); n.fftSize = 512; n.getFloatTimeDomainData = (b) => b.fill(0.1); return n }
  createMediaStreamSource() { return new FakeNode(this) }
  createGain() { const n = new FakeNode(this); n.gain = {}; return n }
  createScriptProcessor() { const n = new FakeNode(this); this.processor = n; return n }
  createBuffer(_ch, length, rate) {
    const data = new Float32Array(length)
    return { duration: length / rate, sampleRate: rate, getChannelData: () => data }
  }
  createBufferSource() { return new FakeSource(this) }
}

class FakeSocket {
  constructor(url) {
    this.url = url
    this.sent = []
    this.readyState = 1
    FakeSocket.last = this
    setTimeout(() => this.onopen?.(), 0)
  }
  send(data) { this.sent.push(typeof data === 'string' ? JSON.parse(data) : { pcm: data.byteLength }) }
  close() { this.readyState = 3 }
  audio(rate, index, samples, turn = this.turn()) {
    const buf = new ArrayBuffer(12 + samples * 2)
    const v = new DataView(buf)
    v.setUint32(0, rate, true)
    v.setUint32(4, turn, true)
    v.setUint32(8, index, true)
    this.onmessage?.({ data: buf })
  }
  turn() { return this.sent.filter((m) => m.turn !== undefined).at(-1)?.turn ?? 0 }
  json(frame) { this.onmessage?.({ data: JSON.stringify({ turn: this.turn(), ...frame }) }) }
  types() { return this.sent.map((m) => m.type || 'pcm') }
}

const tracks = []
globalThis.window = globalThis
globalThis.location = { protocol: 'https:', host: 'kairo.example.ts.net' }
Object.defineProperty(globalThis, 'navigator', {
  value: {
    userAgent: 'test',
    mediaDevices: { getUserMedia: async () => ({ getTracks: () => tracks, getAudioTracks: () => tracks }) },
  },
  configurable: true,
})
globalThis.AudioContext = FakeContext
globalThis.WebSocket = FakeSocket
globalThis.Audio = class { play() { return Promise.resolve() } pause() {} }
URL.createObjectURL = () => 'blob:x'
URL.revokeObjectURL = () => {}
globalThis.fetch = async (url, opts = {}) => {
  if (url === '/api/voice/turns') sentTurns.push(JSON.parse(opts.body))
  return { ok: true, json: async () => ({}) }
}
globalThis.performance = globalThis.performance || { now: () => Date.now() }

const { makeDownsampler, makeVad, Player, decodeAudio, startVoice } = await import('../src/voicelink.js')
const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms))
const { outputContext } = await import('../src/voice.js')
const output = () => outputContext()   // Kairo's voice

beforeEach(() => {
  sentTurns.length = 0
  ctxState = 'running'
  tracks.length = 0
  tracks.push({ readyState: 'live', stop() { this.readyState = 'ended' } })
})

// ---------------------------------------------------------------- pure parts

test('48 kHz becomes 20 ms frames at 16 kHz with their loudness', () => {
  const frames = []
  const push = makeDownsampler(48000)
  push(new Float32Array(960 * 2).fill(0.5), (pcm, rms) => frames.push([pcm.length, pcm[0], Number(rms.toFixed(2))]))
  assert.deepEqual(frames, [[320, 16384, 0.5], [320, 16384, 0.5]])
  const odd = []
  makeDownsampler(44100)(new Float32Array(900), (pcm) => odd.push(pcm.length))
  assert.deepEqual(odd, [320])
})

test('voice detection: start, a short pause, talking on, then the end after half a second', () => {
  const vad = makeVad()
  const loud = 0.2
  const quiet = 0.001
  const run = (rms, n) => Array.from({ length: n }, () => vad.feed(rms)).filter(Boolean)
  assert.deepEqual(run(quiet, 50), [])                  // the room
  assert.deepEqual(run(loud, 5), ['start'])             // 60 ms of voice
  assert.deepEqual(run(quiet, 16), ['pause'])           // 300 ms quiet: the PC starts transcribing
  assert.deepEqual(run(loud, 3), ['resume'])
  assert.deepEqual(run(quiet, 25), ['pause', 'end'])    // 500 ms quiet: your turn is over
  assert.equal(vad.speaking, false)
})

test('a loud room raises the bar, and over Kairo only clear speech counts', () => {
  const vad = makeVad()
  for (let i = 0; i < 400; i++) vad.feed(0.005)         // steady noise at about -46 dB
  assert.ok(vad.floor > -48 && vad.floor <= -40)
  assert.equal(vad.speaking, false)
  assert.equal([0.01, 0.01, 0.01, 0.01].map((r) => vad.feed(r)).filter(Boolean).length, 0)   // a bit louder: still the room
  const v3 = makeVad()
  for (let i = 0; i < 400; i++) v3.feed(0.05)           // very loud room (-26 dB): never endless talk
  assert.equal(v3.speaking, false)
  const v2 = makeVad()
  const echo = Array.from({ length: 20 }, () => v2.feed(0.008, { barge: true })).filter(Boolean)
  assert.deepEqual(echo, [])                            // Kairo's own echo doesn't stop it
  const over = Array.from({ length: 12 }, () => v2.feed(0.3, { barge: true })).filter(Boolean)
  assert.deepEqual(over, ['start'])                     // 200 ms of you talking does
})

test('audio frames: rate, turn, sentence number, samples', () => {
  const buf = new ArrayBuffer(12 + 6)
  new DataView(buf).setUint32(0, 24000, true)
  new DataView(buf).setUint32(4, 7, true)
  new DataView(buf).setUint32(8, 2, true)
  const a = decodeAudio(buf)
  assert.deepEqual([a.rate, a.turn, a.index, a.pcm.length], [24000, 7, 2, 3])
})

test('sentences play back to back, stop ends them all, replay plays them again', () => {
  const ctx = new FakeContext()
  const p = new Player(ctx)
  assert.equal(p.push(24000, new Int16Array(24000)), true)   // 1 s
  assert.equal(p.push(24000, new Int16Array(12000)), true)
  assert.deepEqual(ctx.sources.map((s) => s.started), [0.03, 1.03])   // no gap
  assert.equal(p.busy, true)
  p.stop()
  assert.equal(p.busy, false)
  assert.ok(ctx.sources.every((s) => s.stopped))
  assert.equal(p.replay(), true)
  assert.equal(ctx.sources.length, 4)
  const blocked = new Player({ ...new FakeContext(), state: 'suspended' })
  assert.equal(blocked.push(24000, new Int16Array(10)), false)        // iOS didn't let it play
})

// ---------------------------------------------------------------- a whole turn

async function talk(path = 'call', on = {}) {
  const v = startVoice({ path, callId: 7, on })
  await v.ready
  const mic = contexts.filter((c) => c.processor).at(-1)
  const ws = FakeSocket.last
  const frame = (rms) => {
    const input = new Float32Array(960).fill(rms)
    mic.processor.onaudioprocess({ inputBuffer: { getChannelData: () => input } })
  }
  const say = (n, rms = 0.2) => { for (let i = 0; i < n; i++) frame(rms) }
  const hush = (n) => say(n, 0.001)
  return { v, ws, say, hush, mic }
}

test('a spoken turn: audio streams while you talk, the answer plays, every step is measured', async () => {
  const states = []
  const { v, ws, say, hush } = await talk('call', { state: (s) => states.push(s) })
  hush(20)
  say(10)
  hush(16)                    // a pause: transcribe what there is
  hush(10)                    // the end
  const types = ws.types()
  assert.equal(types[0], 'start')
  assert.ok(types.indexOf('pause') > types.indexOf('start') && types.includes('end'))
  const afterPause = types.slice(types.indexOf('pause') + 1)
  assert.deepEqual(afterPause, ['end'])                              // the quiet after a pause stays on the phone
  assert.deepEqual(ws.sent.find((m) => m.type === 'end'), { type: 'end', path: 'call', call_id: 7, turn: 1 })
  assert.equal(v.state, 'thinking')

  ws.json({ type: 'transcript', text: 'hello', language: 'en', stt_ms: 120 })
  ws.json({ type: 'delta', text: 'Hi there.' })
  ws.json({ type: 'sentence', i: 0 })
  ws.json({ type: 'say', i: 0, text: 'Hi there.' })
  ws.audio(24000, 0, 2400)
  assert.equal(v.state, 'speaking')
  ws.json({ type: 'done', text: 'Hi there.', model: 'haiku', server: { queue_ms: 3, first_words_ms: 900, stt_ms: 120 } })
  ws.json({ type: 'spoken', n: 1 })
  await tick(80)                                                     // it starts playing
  const out = output()
  out.sources.forEach((s) => s.end())                                // ... and finishes
  await tick()
  assert.equal(v.state, 'listening')
  assert.equal(sentTurns.length, 1)
  const t = sentTurns[0]
  assert.equal(t.outcome, 'played')
  assert.equal(t.model, 'haiku')
  assert.deepEqual(Object.keys(t.marks).sort(),
    ['audio_ready', 'endpoint', 'first_sentence', 'first_words', 'playing', 'sent', 'transcribed'])
  assert.equal(t.server.stt_ms, 120)
  assert.ok(states.includes('thinking') && states.includes('speaking'))
  v.close()
})

test('talking over Kairo stops it at once and starts your next turn', async () => {
  const { v, ws, say, hush } = await talk()
  hush(10); say(10); hush(26)
  ws.json({ type: 'transcript', text: 'tell me a story', language: 'en', stt_ms: 100 })
  const before = output().sources.length
  ws.audio(24000, 0, 48000)                                           // 2 s of Kairo
  const mine = output().sources.slice(before)
  say(12, 0.4)                                                        // you talk over it
  const types = ws.types()
  assert.ok(types.lastIndexOf('stop') > types.lastIndexOf('end'))
  assert.deepEqual(ws.sent.findLast((m) => m.type === 'stop'), { type: 'stop', turn: 1 })
  assert.ok(types.lastIndexOf('start') > types.lastIndexOf('stop'))
  assert.ok(mine.length === 1 && mine.every((s) => s.stopped))
  assert.equal(sentTurns.at(-1).outcome, 'interrupted')
  // the talked-over turn's late frames don't touch the next one
  const old = ws.turn()
  hush(26)
  assert.equal(ws.turn(), old + 1)
  const n = output().sources.length
  ws.audio(24000, 1, 2400, old)
  ws.json({ type: 'spoken', n: 2, turn: old })
  assert.equal(output().sources.length, n)
  assert.equal(v.state, 'thinking')
  v.close()
})

test('a spoken chat message closes the mic at the end of speech (the answer plays on the loudspeaker)', async () => {
  const { v, say, hush } = await talk('chat_voice')
  hush(5); say(10); hush(26)
  assert.equal(tracks[0].readyState, 'ended')
  v.close()
})

test('when iOS blocks the audio it says so, and a tap plays it', async () => {
  let taps = 0
  const { v, ws, say, hush } = await talk('call', { needTap: () => { taps += 1 } })
  const out = output()
  out.state = 'suspended'
  hush(5); say(10); hush(26)
  ws.json({ type: 'transcript', text: 'hi', language: 'en', stt_ms: 90 })
  ws.audio(24000, 0, 2400)
  assert.equal(taps, 1)
  assert.equal(sentTurns.at(-1).outcome, 'blocked')
  ctxState = 'running'
  out.state = 'running'
  assert.equal(v.tapToPlay(), true)
  assert.ok(out.sources.length >= 1)
  v.close()
})

test('nothing understood: no answer, the call listens again', async () => {
  let none = 0
  const { v, ws, say, hush } = await talk('call', { noSpeech: () => { none += 1 } })
  hush(5); say(10); hush(26)
  ws.json({ type: 'transcript', text: '', language: null, stt_ms: 80 })
  assert.equal(none, 1)
  assert.equal(v.state, 'listening')
  assert.equal(sentTurns.at(-1).outcome, 'no_speech')
  v.close()
})

test('a turn the PC ignored (someone else, or too far away): no answer, not "nothing heard", listening again', async () => {
  let none = 0
  let ignored = null
  const { v, ws, say, hush } = await talk('call', { noSpeech: () => { none += 1 }, ignored: (f) => { ignored = f.reason } })
  hush(5); say(10); hush(26)
  const before = sentTurns.length
  ws.json({ type: 'ignored', reason: 'not_you', score: 0.08 })
  assert.equal(ignored, 'not_you')
  assert.equal(none, 0)                       // three of these never end the call as "I can't hear you"
  assert.equal(v.state, 'listening')
  assert.equal(sentTurns.length, before)      // not one of the owner's turns: Voice speed lists it from the PC
  v.close()
})

test('hanging up mid-sentence still sends what you said', async () => {
  const { v, ws, say, hush } = await talk()
  hush(12)
  say(10)
  v.close()
  assert.equal(ws.types().at(-1), 'end')
})

test('smart endpointing: the PC ends the turn at a short pause; the phone sends no end of its own', async () => {
  const { v, ws, say, hush } = await talk('call')
  ws.onmessage({ data: JSON.stringify({ type: 'hello', endpoint: 'smart', pause_ms: 200, end_ms: 1500 }) })
  hush(20)
  say(10)
  hush(11)                    // 220 ms: a pause already (smart: 200 ms), not yet an end
  const types = ws.types()
  assert.ok(types.includes('pause') && !types.includes('end'))
  assert.deepEqual(ws.sent.find((m) => m.type === 'start'), { type: 'start', rate: 16000, path: 'call', call_id: 7 })
  ws.onmessage({ data: JSON.stringify({ type: 'endpoint', turn: 1, p: 0.9, ms: 25 }) })
  assert.equal(v.state, 'thinking')
  hush(80)                    // 1.6 s more quiet: the phone doesn't end it again
  assert.equal(ws.types().filter((t) => t === 'end').length, 0)
  ws.json({ type: 'transcript', text: 'hello', language: 'en', stt_ms: 90, turn: 1 })
  ws.json({ type: 'delta', text: 'Hi.', turn: 1 })
  ws.json({ type: 'done', text: 'Hi.', model: 'lfm2.5-2.6b', server: { llm_ms: 60, stt_ms: 90 }, turn: 1 })
  ws.audio(24000, 0, 2400, 1)
  ws.json({ type: 'spoken', n: 1, turn: 1 })
  await tick(80)
  output().sources.forEach((s) => s.end())
  await tick()
  const t = sentTurns.at(-1)
  assert.equal(t.model, 'lfm2.5-2.6b')
  assert.ok(t.marks.endpoint >= 200 && t.marks.endpoint < 600)        // t0 = when you went quiet
  assert.equal(t.server.endpoint_ms, 25)
  v.close()
})
