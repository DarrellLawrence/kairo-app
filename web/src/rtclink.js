// Voice V3 (owner's request 2026-10-16): calls and the voice button over WebRTC to Kairo's own PC
// (server/app/voicertc.py). Your voice goes as WebRTC audio (Opus, the iPhone's echo cancellation)
// straight to the PC over Tailscale; Kairo's voice comes back on the same connection and plays in an
// <audio> element (so echo cancellation knows what to cancel and you can talk over Kairo). The PC
// decides when you're done (Silero VAD + Smart Turn), or you tap (finish(): owner's request 2026-10-20,
// for a loud room where the PC never hears a pause), and stops its voice when you talk over it. A data
// channel carries the captions and the PC's own times.
//
// Order on iOS: the mic first (getUserMedia), then the peer connection (without the mic permission,
// Safari hides its addresses behind mDNS names, which don't work over Tailscale). No STUN/TURN: the
// phone and the PC reach each other on the tailnet.
//
// Measured per turn (turns.js, paths call / chat_voice, line webrtc): t0 is your last loud mic frame
// before the PC said it heard a turn; the PC's steps (turn decided, speech to text, first word, first
// audio sent) come on the data channel; playing = Kairo's voice is heard on this phone. RTT from the
// connection's stats; direct or relayed comes from the PC (Tailscale knows, WebRTC can't see it).
import { clientLog, device } from './clientlog.js'
import { startTurn } from './turns.js'
import { flagOn } from './flags.js'
import { closeMic, logMic, micSettings, micStream, outputContext, unlockAudio } from './voice.js'
import { makeVad } from './voicelink.js'

const FRAME_MS = 20
const PLAYING_RMS = 0.01        // Kairo's incoming voice is this loud: you hear it
const CONNECT_MS = 8000

export async function rtcStatus() {
  try {
    const r = await fetch('/api/rtc')
    return r.ok ? await r.json() : null
  } catch { return null }
}

export function rtcSupported() {
  return typeof RTCPeerConnection !== 'undefined' && Boolean(navigator.mediaDevices?.getUserMedia)
}

/* When you last made a sound that counts as speech: feed(rms, t) per 20 ms mic frame. */
export function makeLastVoice() {
  const vad = makeVad({ endMs: 2000 })
  let last = null
  return {
    feed(rms, t) {
      vad.feed(rms)
      if (vad.speaking && 20 * Math.log10(rms + 1e-9) > Math.max(vad.floor + 7, -56)) last = t
      return last
    },
    get last() { return last },
  }
}

/* One turn on the WebRTC line, from the PC's events, for turns.js. */
export function makeRtcMeter(path, { start = startTurn, now = () => performance.now() } = {}) {
  let turn = null
  let number = null
  let net = {}
  const finish = (outcome, reason) => { turn?.finish(outcome, reason); turn = null; number = null }
  return {
    get turn() { return turn },
    get number() { return number },
    network(values) { net = { ...net, ...values } },
    heard(f, lastVoice, tappedAt = null) {   // "transcript": the PC heard a whole turn
      if (turn) finish(turn.has('playing') ? 'played' : 'interrupted', turn.has('playing') ? null : 'talked on')
      const t = now()
      const tapped = f.ended === 'tap' && tappedAt != null      // you tapped "done": the turn ended then
      const endedAt = tapped ? tappedAt : lastVoice != null && t - lastVoice < 5000 ? lastVoice : t - 300
      turn = start(path, endedAt, { line: 'webrtc', ...net, ...(tapped ? { ended: 'tap' } : {}) })
      number = f.turn
      turn.server(f.server)
      turn.mark('endpoint', endedAt + (f.server?.turn_at_ms ?? 0))
      turn.mark('transcribed')
      turn.mark('sent')
    },
    words(f) { if (turn && f.turn === number) turn.mark('first_words') },
    timing(f) {                    // the PC sent the answer's first audio
      if (!turn || f.turn !== number) return
      turn.server(f.server)
      turn.mark('audio_ready')
    },
    model(m) { turn?.model(m) },
    server(values) { turn?.server(values) },
    level(rms) {
      if (turn && turn.has('audio_ready') && rms >= PLAYING_RMS) turn.mark('playing')
    },
    rtt(ms) { if (turn && Number.isFinite(ms)) turn.server({ rtt_ms: ms }) },
    done() { if (turn) finish(turn.has('playing') ? 'played' : 'failed', turn.has('playing') ? null : 'nothing played') },
    cut(why) { if (turn) finish('interrupted', why) },
    end(why) { if (turn) finish(turn.has('audio_ready') ? 'interrupted' : 'no_speech', why) },
  }
}

function rmsOf(analyser, buf) {
  analyser.getFloatTimeDomainData(buf)
  let sum = 0
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
  return Math.sqrt(sum / buf.length)
}

/* The selected path's round trip (ms) and its candidate types, and the inbound audio (packets, jitter), from a
   connection's stats. Either may be null. */
export function readStats(stats) {
  let pair = null
  const byId = new Map()
  stats.forEach((s) => {
    byId.set(s.id, s)
    if (s.type === 'transport' && s.selectedCandidatePairId) pair = s.selectedCandidatePairId
  })
  let p = pair ? byId.get(pair) : null
  if (!p) stats.forEach((s) => { if (s.type === 'candidate-pair' && (s.selected || s.nominated) && s.state === 'succeeded') p = s })
  let jitterMs = null
  let inbound = null
  stats.forEach((s) => {
    if (s.type === 'inbound-rtp' && s.kind === 'audio') {
      if (s.jitterBufferEmittedCount > 0) jitterMs = Math.round((s.jitterBufferDelay / s.jitterBufferEmittedCount) * 1000)
      inbound = { received: s.packetsReceived ?? 0, lost: s.packetsLost ?? 0, jitter_ms: jitterMs }
    }
  })
  let path = null
  if (p) {
    const local = byId.get(p.localCandidateId)
    const remote = byId.get(p.remoteCandidateId)
    path = {
      rtt_ms: Number.isFinite(p.currentRoundTripTime) ? Math.round(p.currentRoundTripTime * 1000) : null,
      local: local?.candidateType || null,
      remote: remote?.candidateType || null,
      jitter_ms: jitterMs,
    }
  }
  return { path, inbound }
}

export async function pathStats(pc) {
  return readStats(await pc.getStats()).path
}

/* The WebRTC line. Same shape as voicelink's startVoice, plus on.fallback({reason, message}). */
export function startRtc({ path = 'call', callId = null, on = {} } = {}) {
  const call = path === 'call'
  const logs = flagOn('g7.call_logs')     // read once, when the call starts
  const warnAec = flagOn('g7.no_aec_warning')
  const followPc = flagOn('g7.listen_on_barge_in')   // the screen goes to 'listening' when the PC says you talked over Kairo
  const meter = makeRtcMeter(path)
  const lastVoice = makeLastVoice()
  let pc = null
  let dc = null
  let mic = null
  let audio = null
  let remote = null
  let timer = null
  let statsTimer = null
  let state = 'connecting'
  let closed = false
  let fell = false
  let listening = true
  let micLevel = 0
  let outLevel = 0
  let written = false         // "done" came: the answer is written (its voice may still play)
  let spoken = false          // the PC's voice went quiet after it
  let disconnectedAt = null
  let reply = ''
  let talking = false         // the PC hears a turn of yours (speech, or words not answered yet): a tap ends it
  let tappedAt = null
  let audioLogged = false     // g7.call_logs: 'rtc.audio' is written once per call
  let inbound = null          // the latest inbound audio stats

  const set = (s) => { if (state !== s) { state = s; on.state?.(s) } }
  const hear = (v) => { if (talking !== v) { talking = v; on.hearing?.(v) } }
  const send = (obj) => { if (dc?.readyState === 'open') dc.send(typeof obj === 'string' ? obj : JSON.stringify(obj)) }

  const teardown = () => {
    clearInterval(timer)
    clearInterval(statsTimer)
    timer = statsTimer = null
    try { pc?.close() } catch { /* gone */ }
    if (audio) { audio.pause(); audio.srcObject = null }
    mic?.stream.getAudioTracks().forEach((tr) => { tr.enabled = true })   // the old line may take the mic over
    pc = dc = null
  }

  const fallback = (reason, message) => {
    if (fell || closed) return
    fell = true
    meter.end(`fallback: ${reason}`)
    clientLog('rtc.fallback', { reason, ...device() })
    teardown()
    on.fallback?.({ reason, message })
  }

  /* You talked over Kairo and the PC stopped it (its barge_in event, or its answer ending as interrupted). */
  const bargeIn = () => {
    if (state !== 'speaking') return
    meter.cut('talked over')
    set('listening')
  }

  const afterAnswer = () => {
    written = spoken = false
    reply = ''
    if (closed) return
    if (!listening) { set('paused'); return }
    set(call ? 'listening' : 'idle')
    on.answered?.()
  }

  let settleTimer = null
  const settle = (ms) => {                   // the answer is over once its last audio has played here
    clearTimeout(settleTimer)
    settleTimer = setTimeout(() => {
      if (!written || closed) return
      meter.done()
      afterAnswer()
    }, ms)
  }

  const onEvent = (raw) => {
    let f
    try { f = JSON.parse(raw) } catch { return }
    switch (f.type) {
      case 'speech':                         // the PC's VAD hears speech: a turn starts or goes on
        hear(true)
        break
      case 'heard':                          // words at a pause (the turn may go on)
        if (f.text) hear(true)
        break
      case 'quiet':                          // only noise since the last turn: nothing to end
      case 'ignored':                        // not you, or too far away (speaker check): not your turn
        hear(false)
        break
      case 'transcript':
        hear(false)
        meter.heard(f, lastVoice.last, tappedAt)
        tappedAt = null
        reply = ''
        written = spoken = false
        set('thinking')
        on.transcript?.(f.text, f.language)
        break
      case 'delta':
        meter.words(f)
        reply += f.text || ''
        on.delta?.(f.text || '')
        break
      case 'say':
        if (f.turn === meter.number && state !== 'speaking') set('speaking')
        break
      case 'barge_in':                       // the PC cut Kairo off because you really talked over it
        if (followPc) bargeIn()
        break
      case 'timing':
        meter.timing(f)
        break
      case 'done':
        meter.model(f.model)
        meter.server(f.server)
        if (followPc && f.interrupted) bargeIn()
        on.done?.(f)
        written = true
        settle(spoken ? 250 : (f.text || '').trim() ? 8000 : 250)   // over when its voice has played (or never came)
        break
      case 'spoken':                         // Kairo's voice went quiet
        spoken = true
        if (written) settle(250)
        break
      case 'hello':                          // the PC's view of the path: direct or relayed by Tailscale
        meter.network({ net: f.net || 'unknown', via: f.via || null })
        on.network?.(f)
        break
      case 'error':
        clientLog('rtc.error', { status: f.status, msg: String(f.message || '').slice(0, 160) })
        on.error?.(f)
        break
      default:
    }
  }

  const tick = () => {
    const t = performance.now()
    if (mic) {
      const r = rmsOf(mic.analyser, mic.buf)
      micLevel = Math.min(1, r * 8)
      const before = lastVoice.last
      lastVoice.feed(listening ? r : 0, t)
      if (!followPc && lastVoice.last !== before && state === 'speaking') {   // the old way: any sound of yours on this phone
        meter.cut('talked over')                                // (and the PC says "speech": your turn goes on)
        set('listening')
      }
    }
    if (remote) {
      const r = rmsOf(remote.analyser, remote.buf)
      outLevel = Math.min(1, r * 8)
      meter.level(r)
    }
    if (pc && ['disconnected', 'failed'].includes(pc.connectionState)) {
      disconnectedAt ??= t
      if (pc.connectionState === 'failed' || t - disconnectedAt > 4000) fallback('webrtc', 'The connection to the PC dropped.')
    } else {
      disconnectedAt = null
    }
  }

  /* g7.call_logs: how the call's audio stands, once per call: when the first sound from Kairo arrives, or at
     hang-up when none ever did (that silence is the finding). */
  const logAudio = (when) => {
    if (!logs || audioLogged) return
    audioLogged = true
    clientLog('rtc.audio', {
      when, path, inbound,
      audio: audio ? { muted: audio.muted, paused: audio.paused, hasStream: Boolean(audio.srcObject) } : null,
      ...device(),
    })
  }

  const measure = async () => {
    if (!pc) return
    try {
      const { path: net, inbound: heard } = readStats(await pc.getStats())
      if (net?.rtt_ms != null) meter.rtt(net.rtt_ms)
      if (net) on.path?.(net)
      inbound = heard
      if (heard?.received > 0) logAudio('first-audio')
    } catch { /* stats are a nice-to-have */ }
    send('ping')        // the PC's transport counts the line as alive while pings come
  }

  const waitFor = (what, ms) => new Promise((resolve) => {
    const t = setTimeout(() => resolve(null), ms)
    what((v) => { clearTimeout(t); resolve(v) })
  })

  const ready = (async () => {
    try {
      unlockAudio({ call: true })
      mic = await micStream()                  // the mic first: then Safari shows its tailnet address
      if (closed) return false
      mic.buf = mic.buf || new Float32Array(mic.analyser.fftSize)
      const track = mic.stream.getAudioTracks()[0]
      logMic(track, path)
      // g7.no_aec_warning: the phone says it does not cancel the echo (iOS reports it in getSettings), so Kairo's
      // own voice can come back in through the mic and cut it off. Only a plain `false` counts.
      if (warnAec && micSettings(track)?.echoCancellation === false) {
        clientLog('rtc.warn', { reason: 'no-aec', path, ...device() })
        on.warn?.({ reason: 'no-aec', message: 'Echo cancelling is off for this call, so Kairo may hear itself and stop talking. Try AirPods, or turn the volume down.' })
      }
      pc = new RTCPeerConnection({ iceServers: [] })
      pc.addTransceiver(mic.stream.getAudioTracks()[0], { direction: 'sendrecv', streams: [mic.stream] })
      audio = new Audio()
      audio.autoplay = true
      audio.setAttribute('playsinline', '')
      pc.ontrack = (e) => {
        const stream = e.streams[0] || new MediaStream([e.track])
        audio.srcObject = stream
        audio.play().catch(() => on.needTap?.())
        try {
          const ctx = outputContext({ call: true })
          const analyser = ctx.createAnalyser()
          analyser.fftSize = 512
          ctx.createMediaStreamSource(stream).connect(analyser)   // only measured, not played twice
          remote = { analyser, buf: new Float32Array(analyser.fftSize) }
        } catch (err) { clientLog('rtc.level_error', { err: String(err).slice(0, 120) }) }
      }
      dc = pc.createDataChannel('kairo', { ordered: true })
      dc.onmessage = (m) => { if (typeof m.data === 'string' && m.data[0] === '{') onEvent(m.data) }
      const opened = waitFor((done) => { dc.onopen = () => done(true) }, CONNECT_MS)
      await pc.setLocalDescription(await pc.createOffer())
      // all candidates in the offer (no trickle): the PC answers once
      await waitFor((done) => {
        if (pc.iceGatheringState === 'complete') done(true)
        else pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') done(true) }
      }, 2000)
      const r = await fetch('/api/rtc/offer', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: 'offer', path, call_id: callId }),
      })
      if (closed) return false
      if (!r.ok) {
        let why = 'line'
        try { why = (await r.json()).detail || why } catch { /* not json */ }
        fallback(why, 'Kairo’s PC can’t take a WebRTC call right now.')
        return false
      }
      const got = await r.json()
      await pc.setRemoteDescription({ type: 'answer', sdp: got.sdp })
      if (!(await opened)) { fallback('webrtc', 'Couldn’t reach the PC over WebRTC from this phone.'); return false }
      timer = setInterval(tick, FRAME_MS)
      statsTimer = setInterval(measure, 1000)
      measure()
      on.engine?.('webrtc')
      clientLog('rtc.start', { path, ...device() })
      set('listening')
      return true
    } catch (err) {
      clientLog('rtc.start_error', { err: `${err?.name}: ${err?.message}`.slice(0, 160) })
      fallback('webrtc', err?.name === 'NotAllowedError' ? 'Microphone not allowed.' : 'The WebRTC call couldn’t start.')
      return false
    }
  })()

  const onHidden = () => { if (document.hidden && !closed && !call) api.close() }   // the voice button: off when hidden
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onHidden)

  const api = {
    ready,
    engine: 'webrtc',
    get state() { return state },
    level() { return state === 'speaking' ? outLevel : state === 'listening' ? micLevel : 0 },
    /* You tapped "done": the PC ends your turn now, without waiting for a pause (it still checks it's
       you). False when the PC hears no turn of yours (nothing said yet): the tap is then a pause. */
    finish() {
      if (!talking || !listening || closed) return false
      tappedAt = performance.now()
      send({ type: 'done' })
      return true
    },
    /* "Hey <name>." when a call you placed connects: from the PC's cache, no model involved. */
    greet() {
      written = true
      spoken = false
      set('speaking')
      send({ type: 'greet' })
    },
    /* Kairo says this (a greeting), word for word, no model involved. */
    say(text) {
      written = true
      spoken = false
      set('speaking')
      send({ type: 'say', text: String(text || '').slice(0, 600) })
    },
    stop() {
      if (audio) { audio.muted = true; setTimeout(() => { if (audio && listening) audio.muted = false }, 400) }
      meter.cut('tapped')
      afterAnswer()
    },
    pause() {
      listening = false
      hear(false)
      mic?.stream.getAudioTracks().forEach((t) => { t.enabled = false })
      if (audio) audio.muted = true
      meter.cut('paused')
      set('paused')
    },
    resume() {
      listening = true
      mic?.stream.getAudioTracks().forEach((t) => { t.enabled = true })
      if (audio) audio.muted = false
      set('listening')
    },
    tapToPlay() {
      unlockAudio({ call: true })
      if (!audio) return false
      audio.play().catch(() => {})
      return true
    },
    close() {
      if (closed) return
      closed = true
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onHidden)
      meter.end('hung up')
      clearTimeout(settleTimer)
      logAudio('close')
      teardown()
      closeMic()                               // iOS: the mic indicator goes off with the call
    },
  }
  return api
}
