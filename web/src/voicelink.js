// Voice V1 (2026-10-15): spoken turns on one line to the PC, both ways (/ws/voice; the protocol
// is in server/app/voicelink.py).
//
// While you talk, the mic streams 16 kHz PCM to the PC. This file decides when you talk and when
// you stopped: an energy detector with a noise floor that follows the room. About 300 ms of quiet
// and the PC starts transcribing; 500 ms and your turn is over. Kairo's answer comes back as audio
// one sentence at a time and plays gaplessly (Web Audio), while the PC makes the next sentence.
// Talking over Kairo stops it at once and your words become the next turn.
//
// iPhone audio route (fix A1): with the mic open, iOS plays in its call route. A call keeps the
// mic open (you can talk over Kairo) unless this phone was found to need the mic paused while
// Kairo talks (voice.js releaseWhileSpeaking: then tap the orb to interrupt). A spoken chat message
// closes the mic at the end of speech, so the answer plays on the loudspeaker.
import { clientLog, device } from './clientlog.js'
import { startTurn } from './turns.js'
import { closeMic, micStream, outputContext, releaseWhileSpeaking, setSession, unlockAudio } from './voice.js'

export const RATE = 16000
export const FRAME = 320                // 20 ms at 16 kHz
const FRAME_MS = 20

/* Averages the input down to 16 kHz (rate / 16000 samples per output sample) and hands out 20 ms
   frames with their loudness. Self-contained: it also runs inside the audio worklet. */
export function makeDownsampler(rate) {
  const ratio = rate / 16000
  let pos = 0
  let acc = 0
  let n = 0
  let out = new Int16Array(320)
  let k = 0
  let sum = 0
  return function push(input, emit) {
    for (let i = 0; i < input.length; i++) {
      acc += input[i]
      n += 1
      pos += 1
      if (pos >= ratio) {
        pos -= ratio
        const v = Math.max(-1, Math.min(1, acc / n))
        acc = 0
        n = 0
        out[k++] = Math.round(v * 32767)
        sum += v * v
        if (k === 320) {
          emit(out, Math.sqrt(sum / 320))
          out = new Int16Array(320)
          k = 0
          sum = 0
        }
      }
    }
  }
}

const WORKLET = `const makeDownsampler = ${makeDownsampler.toString()};
class KairoCapture extends AudioWorkletProcessor {
  constructor() { super(); this.push = makeDownsampler(sampleRate) }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch) this.push(ch, (pcm, rms) => this.port.postMessage({ pcm: pcm.buffer, rms }, [pcm.buffer]))
    return true
  }
}
registerProcessor('kairo-capture', KairoCapture)
`

/* When you talk and when you stopped. feed(rms of a 20 ms frame) -> 'start' | 'pause' | 'resume' |
   'end' | null. The room's noise floor is the quiet end (10th percentile) of the last 3 seconds
   (at most -40 dB until it has heard 3 s), so a fan or traffic raises the bar instead of sounding
   like endless talk. barge: Kairo is talking, so only clearly louder speech than its echo counts. */
export function makeVad({ pauseMs = 300, endMs = 500, startMs = 60, bargeMs = 200, maxMs = 60000 } = {}) {
  const recent = []        // dB of the last 3 s
  let floor = -60
  let speaking = false
  let paused = false
  let voiced = 0
  let quiet = 0
  let length = 0
  let n = 0
  const db = (rms) => 20 * Math.log10(rms + 1e-9)
  const track = (d) => {
    recent.push(d)
    if (recent.length > 150) recent.shift()
    if ((n += 1) % 5 === 0 || recent.length < 10) {
      const sorted = recent.slice().sort((a, b) => a - b)
      floor = Math.max(-70, sorted[Math.floor(sorted.length * 0.1)])
      if (recent.length < 150) floor = Math.min(-40, floor)   // talking from the first moment still counts
    }
  }
  return {
    get speaking() { return speaking },
    get floor() { return floor },
    reset() { speaking = false; paused = false; voiced = 0; quiet = 0; length = 0 },
    feed(rms, { barge = false } = {}) {
      const d = db(rms)
      track(d)
      if (recent.length < 10) return null   // the first 200 ms: learning the room
      if (!speaking) {
        const loud = barge ? d > Math.max(floor + 20, -38) : d > Math.max(floor + 12, -52)
        voiced = loud ? voiced + 1 : 0
        if (voiced * FRAME_MS >= (barge ? bargeMs : startMs)) {
          speaking = true
          paused = false
          quiet = 0
          length = voiced * FRAME_MS
          return 'start'
        }
        return null
      }
      length += FRAME_MS
      if (d > Math.max(floor + 7, -56)) {
        quiet = 0
        if (paused) { paused = false; return 'resume' }
      } else {
        quiet += FRAME_MS
      }
      if (quiet >= endMs || length >= maxMs) {
        speaking = false
        paused = false
        voiced = 0
        return 'end'
      }
      if (!paused && quiet >= pauseMs) { paused = true; return 'pause' }
      return null
    },
  }
}

/* Kairo's voice: PCM sentences scheduled back to back on one AudioContext. */
export class Player {
  constructor(ctx) {
    this.ctx = ctx
    this.next = 0
    this.sources = new Set()
    this.kept = []          // this answer's audio, for "Tap to play"
    this.analyser = null
    this.onPlaying = null   // the first sound of the answer actually started
    this.onIdle = null      // everything scheduled has played
    this._played = false
  }

  get busy() { return this.sources.size > 0 }

  newAnswer() { this.stop(); this.kept = []; this._played = false }

  push(rate, pcm) {
    this.kept.push({ rate, pcm })
    return this._schedule(rate, pcm)
  }

  // force: inside a tap that just resumed the context (it may still say 'suspended' for a moment)
  _schedule(rate, pcm, force = false) {
    const ctx = this.ctx
    if (!ctx || ctx.state === 'closed' || (ctx.state !== 'running' && !force)) return false
    if (!this.analyser) {
      this.analyser = ctx.createAnalyser()
      this.analyser.fftSize = 512
      this.analyser.connect(ctx.destination)
      this.buf = new Float32Array(this.analyser.fftSize)
    }
    const audio = ctx.createBuffer(1, pcm.length, rate)
    const ch = audio.getChannelData(0)
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768
    const src = ctx.createBufferSource()
    src.buffer = audio
    src.connect(this.analyser)
    const at = Math.max(ctx.currentTime + 0.03, this.next)
    this.next = at + audio.duration
    this.sources.add(src)
    src.onended = () => {
      this.sources.delete(src)
      if (!this.sources.size) this.onIdle?.()
    }
    src.start(at)
    if (!this._played) {
      this._played = true
      const wait = Math.max(0, (at - ctx.currentTime) * 1000)
      setTimeout(() => { if (this.sources.has(src)) this.onPlaying?.() }, wait)
    }
    return true
  }

  /* Inside a tap: the answer so far, again (iOS had blocked it). */
  replay() {
    this.stop()
    this._played = false
    return this.kept.map(({ rate, pcm }) => this._schedule(rate, pcm, true)).every(Boolean)
  }

  stop() {
    for (const s of this.sources) { s.onended = null; try { s.stop() } catch { /* not started */ } }
    this.sources.clear()
    this.next = 0
  }

  level() {
    if (!this.analyser || !this.sources.size) return 0
    this.analyser.getFloatTimeDomainData(this.buf)
    let sum = 0
    for (let i = 0; i < this.buf.length; i++) sum += this.buf[i] * this.buf[i]
    return Math.min(1, Math.sqrt(sum / this.buf.length) * 5)
  }
}

export function decodeAudio(data) {
  const v = new DataView(data)
  return { rate: v.getUint32(0, true), turn: v.getUint32(4, true), index: v.getUint32(8, true), pcm: new Int16Array(data.slice(12)) }
}

function lineUrl() {
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/voice`
}

/* The mic as 20 ms frames of 16 kHz PCM: onFrame(Int16Array, rms). */
async function startCapture(onFrame) {
  const m = await micStream()
  const ctx = m.ctx
  const source = ctx.createMediaStreamSource(m.stream)
  let node = null
  let how = 'worklet'
  if (ctx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
    const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }))
    try {
      if (!ctx.kairoCapture) { await ctx.audioWorklet.addModule(url); ctx.kairoCapture = true }
      node = new AudioWorkletNode(ctx, 'kairo-capture')
      node.port.onmessage = (e) => onFrame(new Int16Array(e.data.pcm), e.data.rms)
    } catch (e) {   // the module was refused: the main-thread way below still works
      clientLog('voice.worklet_error', { err: `${e?.name}: ${e?.message}`.slice(0, 160) })
      node = null
    } finally { URL.revokeObjectURL(url) }
  }
  if (!node) {   // no worklet (old iOS, or refused): the same on the main thread
    how = 'script'
    const push = makeDownsampler(ctx.sampleRate)
    node = ctx.createScriptProcessor(2048, 1, 1)
    node.onaudioprocess = (e) => push(e.inputBuffer.getChannelData(0), (pcm, rms) => onFrame(pcm, rms))
  }
  const sink = ctx.createGain()
  sink.gain.value = 0
  source.connect(node)
  node.connect(sink)
  sink.connect(ctx.destination)
  if (ctx.state === 'suspended') await ctx.resume().catch(() => {})
  clientLog('voice.mic_start', { ...device(), rate: ctx.sampleRate, how, ctx: ctx.state })
  return {
    stop() {
      try { source.disconnect(); node.disconnect(); sink.disconnect() } catch { /* already gone */ }
      if (node.port) node.port.onmessage = null
    },
  }
}

/* A voice conversation on the line. path: 'call' (the mic stays on, talk over Kairo) or
   'chat_voice' (one message: the mic closes at the end of speech). on: callbacks
   state(s) listening|thinking|speaking|paused, transcript(text), delta(text), done(frame),
   error(frame), needTap(), noSpeech(), level is read with level(). */
export function startVoice({ path = 'call', callId = null, on = {} } = {}) {
  const call = path === 'call'
  const vad = makeVad()
  const pending = []      // quiet frames after a pause, sent if you talk on
  const preroll = []      // the last 300 ms before you start talking
  let ws = null
  let capture = null
  let player = null
  let state = 'idle'
  let micLevel = 0
  let turn = null
  let answering = false   // a turn is out: transcript, answer or audio still to come
  let spokenDone = false
  let closed = false
  let listening = true
  let turnNo = 0          // every frame of a turn carries it: a talked-over turn's late frames are dropped

  const set = (s) => { if (state !== s) { state = s; on.state?.(s) } }
  const send = (obj) => { if (ws?.readyState === 1) ws.send(JSON.stringify(obj)) }
  const sendPcm = (pcm) => { if (ws?.readyState === 1) ws.send(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength)) }

  const finishTurn = (outcome, reason) => { turn?.finish(outcome, reason); turn = null }

  const afterAnswer = () => {
    answering = false
    if (closed) return
    if (call) {
      if (!listening) set('paused')
      else {
        set('listening')
        if (!capture) openCapture()   // the mic was paused while Kairo talked
      }
    } else {
      set('idle')
    }
    on.answered?.()
  }

  const ended = () => {        // the answer's audio is all out and played (or there was none)
    if (!spokenDone || player?.busy) return
    if (turn) finishTurn(turn.has('playing') ? 'played' : 'failed', turn.has('playing') ? null : 'nothing played')
    afterAnswer()
  }

  const onFrame = (pcm, rms) => {
    micLevel = Math.min(1, rms * 8)
    if (closed || !listening) return
    const kairoTalking = state === 'speaking' && player?.busy
    const ev = vad.feed(rms, { barge: kairoTalking })
    if (!vad.speaking && ev !== 'end') {
      preroll.push(pcm)
      if (preroll.length > 15) preroll.shift()
      return
    }
    if (ev === 'start') {
      if (kairoTalking || answering) interrupt('talked over')
      send({ type: 'start', rate: RATE })
      for (const p of preroll) sendPcm(p)
      preroll.length = 0
      sendPcm(pcm)
      clientLog('voice.speech_start', { path, floor: Math.round(vad.floor) })
      set('listening')
      on.hearing?.(true)
      return
    }
    if (ev === 'pause') { send({ type: 'pause' }); pending.push(pcm); return }
    if (ev === 'resume') { for (const p of pending) sendPcm(p); pending.length = 0; sendPcm(pcm); return }
    if (ev === 'end') { endSpeech(performance.now() - 500); return }
    if (pending.length) pending.push(pcm); else sendPcm(pcm)
  }

  // endedAt: when you stopped talking (the quiet before the end counts as Kairo's time).
  const endSpeech = (endedAt) => {
    pending.length = 0
    turn = startTurn(path, endedAt)
    turn.mark('endpoint')
    turnNo += 1
    send({ type: 'end', path, call_id: callId, turn: turnNo })
    clientLog('voice.speech_end', { path })
    on.hearing?.(false)
    answering = true
    spokenDone = false
    player?.newAnswer()
    set('thinking')
    if (!call || releaseWhileSpeaking()) pauseCapture()   // the answer plays on the loudspeaker
  }

  const interrupt = (why) => {
    send({ type: 'stop', turn: turnNo })   // words not answered yet go with your next ones (one message)
    player?.stop()
    if (turn) finishTurn('interrupted', why)
    answering = false
  }

  const openCapture = async () => {
    try {
      capture = await startCapture(onFrame)
    } catch (e) {
      clientLog('voice.mic_error', { err: `${e?.name}: ${e?.message}`.slice(0, 160) })
      on.error?.({ type: 'error', status: 'mic', message: 'Microphone not allowed. iPhone Settings → Apps → Safari → Microphone.' })
    }
  }

  const pauseCapture = () => {
    capture?.stop()
    capture = null
    vad.reset()
    closeMic()
    setSession('playback')
  }

  const onAudio = (data) => {
    const { rate, turn: of, pcm } = decodeAudio(data)
    if (of !== turnNo || !answering) return   // a turn you talked over
    if (!listening && call) return            // paused: the answer waits in the captions, unspoken
    turn?.mark('audio_ready')
    if (state !== 'speaking') set('speaking')
    if (!player.push(rate, pcm)) {
      clientLog('voice.blocked', { ...device(), ctx: player.ctx?.state ?? 'none' })
      if (turn) finishTurn('blocked', `audio context ${player.ctx?.state ?? 'none'}`)
      on.needTap?.()
    }
  }

  const onMessage = (ev) => {
    if (typeof ev.data !== 'string') { onAudio(ev.data); return }
    const f = JSON.parse(ev.data)
    if (f.turn !== undefined && (f.turn !== turnNo || !answering) && f.type !== 'done') return   // a turn you talked over
    if (f.type === 'done' && f.turn !== undefined && f.turn !== turnNo) { on.done?.(f); return }   // its text is still kept
    switch (f.type) {
      case 'transcript':
        turn?.server({ stt_ms: f.stt_ms })
        turn?.mark('transcribed')
        turn?.mark('sent')
        if (!f.text) {
          finishTurn('no_speech')
          on.noSpeech?.()
          afterAnswer()
        } else {
          on.transcript?.(f.text, f.language)
        }
        break
      case 'delta':
        turn?.mark('first_words')
        on.delta?.(f.text)
        break
      case 'model':
        turn?.model(f.model)
        break
      case 'sentence':
        turn?.mark('first_sentence')
        break
      case 'done':
        turn?.model(f.model)
        turn?.server(f.server)
        on.done?.(f)
        break
      case 'spoken':
        spokenDone = true
        ended()
        break
      case 'error':
        if (f.status !== 'voice') { finishTurn('failed', f.status || f.message); spokenDone = true }
        on.error?.(f)
        if (f.status !== 'voice') afterAnswer()
        break
      default:
    }
  }

  const connect = () => new Promise((resolve) => {
    ws = new WebSocket(lineUrl())
    ws.binaryType = 'arraybuffer'
    ws.onmessage = onMessage
    ws.onopen = () => resolve(true)
    ws.onerror = () => resolve(false)
    ws.onclose = () => {
      if (closed) return
      if (answering) { finishTurn('failed', 'line closed'); afterAnswer() }
      setTimeout(() => { if (!closed) connect() }, 1000)   // the phone slept or the PC restarted: back on
    }
  })

  const ready = (async () => {
    unlockAudio()
    player = new Player(outputContext())
    player.onPlaying = () => turn?.mark('playing')
    player.onIdle = () => ended()
    const [ok] = await Promise.all([connect(), openCapture()])
    if (!closed) set('listening')
    return ok
  })()

  return {
    ready,
    get state() { return state },
    level() { return state === 'speaking' ? player?.level() || 0 : state === 'listening' ? micLevel : 0 },
    /* You tapped "done": the end of your speech is now. False when nothing was heard. */
    finish() {
      if (!vad.speaking) return false
      vad.reset()
      endSpeech(performance.now())
      return true
    },
    /* Kairo says this (a greeting), no model involved. */
    say(text) {
      turnNo += 1
      answering = true
      spokenDone = false
      player?.newAnswer()
      set('speaking')
      send({ type: 'say', text, turn: turnNo })
    },
    /* Tap on the orb while Kairo talks: stop it (the answer so far is kept). */
    stop() { interrupt('tapped'); afterAnswer() },
    /* Paused or muted: Kairo goes quiet (the answer is still written, into the captions) and
       hears nothing until resume(). */
    pause() {
      listening = false
      if (vad.speaking) send({ type: 'cancel' })
      vad.reset()
      player?.stop()
      if (turn && answering) finishTurn('interrupted', 'paused')
      set('paused')
    },
    resume() { listening = true; if (!capture) openCapture(); set('listening') },
    /* "No sound? Tap to play", inside the tap: through the loudspeaker, without the mic (the call
       route was the problem; voice.js remembers it for this phone). */
    tapToPlay() {
      if (capture) pauseCapture()
      unlockAudio()
      player.ctx = outputContext()
      const ok = player.replay()
      clientLog('voice.tap_to_play', { ...device(), ok })
      return ok
    },
    /* Hang up. Words still being said are sent: Kairo answers them in chat. */
    close() {
      if (vad.speaking) { send({ type: 'end', path, call_id: callId }); vad.reset() }
      closed = true
      capture?.stop()
      capture = null
      closeMic()
      player?.stop()
      if (turn) finishTurn(answering ? 'interrupted' : 'no_speech', 'hung up')
      const line = ws
      setTimeout(() => line?.close(), 300)
    },
  }
}
