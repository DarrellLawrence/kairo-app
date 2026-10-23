// Browser stand-ins for node --test (not a test itself: the runner only takes *.test.js). Just enough of an
// iPhone's WebKit for the call lines to connect, and a record of what the page did with the audio:
//   rec.session  every navigator.audioSession.type the page set, with whether the mic was open
//   rec.buffers  AudioBufferSource.start() calls (Web Audio playback)
//   rec.plays    <audio>.play() calls on a file/blob src (anything that is not the call's MediaStream)
//   rec.gum      the constraints getUserMedia was given
//   rec.micLevel what every analyser reads (the mic's level; set it to make a sound)
//   rec.logs     what the page told the PC's log (/api/client-log), after flushLogs(rec)
// WebRTC itself only runs on the phone; what is tested here is what the page asks for and when.

import { flushClientLog } from '../src/clientlog.js'

export class FakeChannel {
  constructor() { this.readyState = 'open'; this.sent = [] }
  send(data) { this.sent.push(typeof data === 'string' && data[0] === '{' ? JSON.parse(data) : data) }
  pc(event) { this.onmessage?.({ data: JSON.stringify(event) }) }   // an event from the PC
  count(type) { return this.sent.filter((m) => m?.type === type).length }
  done() { return this.count('done') }
}

export class FakePeer {
  constructor() { FakePeer.last = this; this.connectionState = 'connected'; this.iceGatheringState = 'complete' }
  addTransceiver() {}
  addTrack() {}
  createDataChannel() { this.dc = new FakeChannel(); return this.dc }
  async createOffer() { return { type: 'offer', sdp: 'v=0\r\n' } }
  async setLocalDescription(d) { this.localDescription = d }
  async setRemoteDescription() {
    setTimeout(() => {
      this.ontrack?.({ streams: [{ id: 'kairo-voice' }] })         // Kairo's voice arrives as the connection's own stream
      this.dc.onopen?.()
    }, 0)
  }
  async getStats() { return new Map((this.stats || []).map((s, i) => [s.id ?? `s${i}`, s])) }
  close() { this.connectionState = 'closed' }
}

/* The realtime line's WebSocket to the PC: answers the phone's offer at once. */
export class FakeSocket {
  constructor() { FakeSocket.last = this; this.readyState = 1; this.sent = [] }
  send(data) {
    const m = JSON.parse(data)
    this.sent.push(m)
    if (m.type === 'offer') queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'answer', sdp: 'v=0\r\n', model: 'rt-test' }) }))
  }
  close() { this.readyState = 3 }
}

// The page keeps its AudioContext and priming <audio> between calls, so the stand-ins write to whichever record is
// current (the newest installPhone), not to the one that was current when they were made.
let current = null

export class FakeAudioContext {
  constructor() { this.state = 'running'; this.sampleRate = 48000; this.destination = {} }
  resume() { return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
  createAnalyser() { return { fftSize: 512, connect() {}, getFloatTimeDomainData: (b) => b.fill(current.micLevel) } }
  createMediaStreamSource() { return { connect() {} } }
  createBuffer() { return {} }
  createBufferSource() { return { connect() {}, start: () => { current.buffers += 1 } } }
}

export class FakeAudio {
  constructor() { FakeAudio.last = this; current.audios.push(this); this.muted = false; this.paused = true; this.srcObject = null; this.src = '' }
  play() { if (this.src && !this.srcObject) current.plays.push(this.src); this.paused = false; return Promise.resolve() }
  pause() { this.paused = true }
  setAttribute() {}
}

// What iOS really reports for a call's mic (WebKit bug 326286, comment 8): the one setting it honours.
export const IOS_SETTINGS = { echoCancellation: true, sampleRate: 48000, volume: 1 }

/* Put the stand-ins in place (a fresh phone each call). Returns the record and the mic track.
   settings: what the mic track's getSettings() says (null: a track without getSettings, like a mock).
   ua: the user agent. audioSession: false for a browser without navigator.audioSession. */
export function installPhone({ settings = IOS_SETTINGS, ua = 'test', audioSession = true, rtcStatus = { available: true } } = {}) {
  const rec = { session: [], states: [], buffers: 0, plays: [], gum: [], audios: [], micOpen: false, fetched: [], posted: [], logs: [], micLevel: 0 }
  const session = {
    _t: 'auto',
    get type() { return this._t },
    set type(v) { rec.session.push({ type: v, mic: rec.micOpen }); this._t = v },
    addEventListener(name, fn) { rec.states.push([name, fn]) },
  }
  const listeners = {}
  const micTrack = {
    enabled: true, readyState: 'live',
    stop() { this.readyState = 'ended'; rec.micOpen = false },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn) },
    fire(name) { (listeners[name] || []).forEach((fn) => fn({ type: name })) },
  }
  if (settings) micTrack.getSettings = () => settings
  globalThis.window = globalThis
  globalThis.location = { protocol: 'https:', host: 'kairo.test' }
  Object.defineProperty(globalThis, 'navigator', {
    value: {
      userAgent: ua,
      ...(audioSession ? { audioSession: session } : {}),
      mediaDevices: {
        getUserMedia: async (constraints) => {
          rec.gum.push(constraints)
          rec.micOpen = true
          return { getTracks: () => [micTrack], getAudioTracks: () => [micTrack] }
        },
        getSupportedConstraints: () => ({ echoCancellation: true, noiseSuppression: true, autoGainControl: true }),
      },
    },
    configurable: true,
  })
  current = rec
  globalThis.AudioContext = FakeAudioContext
  globalThis.Audio = FakeAudio
  globalThis.RTCPeerConnection = FakePeer
  globalThis.WebSocket = FakeSocket
  globalThis.fetch = async (url, init) => {
    if (url === '/api/client-log') { rec.logs.push(...JSON.parse(init.body).events); return { ok: true } }
    rec.fetched.push(url)
    if (init?.method === 'POST') rec.posted.push([url, init.body ? JSON.parse(init.body) : null])
    if (url === '/api/rtc') return { ok: true, json: async () => rtcStatus }
    return { ok: true, json: async () => (url === '/api/rtc/offer' ? { sdp: 'v=0\r\n', type: 'answer' } : {}) }
  }
  flushClientLog()      // what an earlier test left in the page's log queue
  rec.logs.length = 0
  return { rec, micTrack, session }
}

/* What the page told the PC's log so far (the newest first call empties the queue), by event name. */
export function logged(rec, name) {
  flushClientLog()
  return rec.logs.filter((e) => e.event === name).map((e) => e.data)
}

/* Everything the page logged so far has reached rec.logs. */
export function flushLogs() { flushClientLog() }

/* The page may never show 'playback' before 'play-and-record': that flip is what puts the audio outside the
   call route (research §7, refuted claim 4). A 'playback' after the call's mic closed is fine. */
export function playbackBeforeCall(sessionLog) {
  const types = sessionLog.map((s) => s.type)
  const first = types.indexOf('playback')
  return first !== -1 && (types.indexOf('play-and-record') === -1 || first < types.indexOf('play-and-record'))
}
