// Realtime voice (owner's decision 2026-10-15, server/app/realtime.py). Calls and the voice button talk
// to OpenAI's Realtime model: your voice goes straight from the phone to OpenAI (WebRTC) and its voice
// comes straight back, so a reply can start about half a second after you stop. The PC creates the
// call (the key stays on the PC: /ws/realtime passes the SDP) and follows it: anything real goes to
// Kairo's brain (ask_kairo), both sides' words go in the chat, every answer counts against the cap.
//
// startLine() is what the screens use: the realtime voice when the PC says it's available, else the
// local one: over WebRTC (rtclink.js, Voice V3) when it can, else the old line (voicelink.js). If realtime fails during a call (cap reached, no internet, OpenAI down),
// the call goes on with the local voice by itself. Same interface either way.
//
// Measured like every turn (turns.js, paths call_live / chat_live): t0 is your last loud mic frame
// before OpenAI says you stopped; endpoint = it said so, first_words = its first words, audio_ready =
// its audio started, playing = Kairo's voice is heard on this phone (the incoming audio's level).
import { clientLog, device } from './clientlog.js'
import { startTurn } from './turns.js'
import { closeMic, micStream, outputContext, unlockAudio } from './voice.js'
import { rtcStatus, rtcSupported, startRtc } from './rtclink.js'
import { makeVad, startVoice } from './voicelink.js'

const FRAME_MS = 20
const PLAYING_RMS = 0.01        // Kairo's incoming voice is this loud: you hear it
const CONNECT_MS = 10000

export async function realtimeStatus() {
  try {
    const r = await fetch('/api/voice/realtime')
    return r.ok ? await r.json() : null
  } catch { return null }
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

/* One spoken turn on the realtime line, from OpenAI's events, for turns.js. start: startTurn. */
export function makeLiveMeter(path, { start = startTurn, now = () => performance.now(), silenceMs = 400 } = {}) {
  let turn = null
  let readyAt = null
  let loudest = 0
  let model = null
  const finish = (outcome, reason) => { turn?.finish(outcome, reason); turn = null }
  return {
    get turn() { return turn },
    model(m) { model = m },
    stopped(lastVoice) {            // input_audio_buffer.speech_stopped
      if (turn) finish('interrupted', 'talked on')
      const t = now()
      const endedAt = lastVoice != null && t - lastVoice < 3000 ? lastVoice : t - silenceMs
      turn = start(path, endedAt)
      turn.model(model)
      turn.mark('endpoint')
      turn.mark('sent')
      readyAt = null
      loudest = 0
    },
    words() { turn?.mark('first_words') },
    audio() { if (turn && readyAt == null) { readyAt = now(); turn.mark('audio_ready') } },
    level(rms) {                    // the incoming audio, while it plays
      loudest = Math.max(loudest, rms)
      if (turn && readyAt != null && rms >= PLAYING_RMS) turn.mark('playing')
    },
    done(how = 'rms') {             // output_audio_buffer.stopped
      if (!turn || readyAt == null) return
      if (!turn.has('playing') && loudest < 1e-4) {     // this phone's level reads nothing: the audio event counts
        turn.mark('playing', readyAt)
        how = 'event'
      }
      const played = turn.has('playing')
      finish(played ? 'played' : 'failed', played ? (how === 'event' ? 'timed by the audio event' : null) : 'nothing played')
    },
    cut(why) { if (turn) finish('interrupted', why) },
    end(why) { if (turn) finish(readyAt == null ? 'no_speech' : 'interrupted', why) },
  }
}

const lineUrl = () => `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/realtime`

function rmsOf(analyser, buf) {
  analyser.getFloatTimeDomainData(buf)
  let sum = 0
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
  return Math.sqrt(sum / buf.length)
}

/* The realtime line. Same shape as voicelink's startVoice, plus on.fallback({reason, message}),
   on.engine(name), on.budget(pct), on.asking(bool). */
export function startRealtime({ path = 'call', callId = null, on = {} } = {}) {
  const call = path === 'call'
  const meter = makeLiveMeter(call ? 'call_live' : 'chat_live')
  const lastVoice = makeLastVoice()
  let ws = null
  let pc = null
  let dc = null
  let mic = null
  let audio = null
  let remote = null           // { analyser, buf }
  let timer = null
  let state = 'connecting'
  let closed = false
  let fell = false
  let listening = true
  let asking = false
  let hearing = false
  let reply = ''
  let micLevel = 0
  let outLevel = 0
  let endAfter = false
  let disconnectedAt = null

  const set = (s) => { if (state !== s) { state = s; on.state?.(s) } }
  const sendEvent = (ev) => { if (dc?.readyState === 'open') dc.send(JSON.stringify(ev)) }

  const teardown = () => {
    clearInterval(timer)
    timer = null
    try { if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'hangup' })) } catch { /* gone */ }
    try { ws?.close() } catch { /* gone */ }
    try { pc?.close() } catch { /* gone */ }
    if (audio) { audio.pause(); audio.srcObject = null }
    mic?.stream.getAudioTracks().forEach((tr) => { tr.enabled = true })   // the local voice may take the mic over
    ws = pc = dc = null
  }

  const fallback = (reason, message) => {
    if (fell || closed) return
    fell = true
    meter.end(`fallback: ${reason}`)
    clientLog('realtime.fallback', { reason, ...device() })
    teardown()
    on.fallback?.({ reason, message })
  }

  const afterAnswer = () => {
    reply = ''
    if (closed) return
    if (!listening) { set('paused'); return }
    set(call ? 'listening' : 'idle')
    on.answered?.()
  }

  const onEvent = (raw) => {
    let ev
    try { ev = JSON.parse(raw) } catch { return }
    switch (ev.type) {
      case 'input_audio_buffer.speech_started':
        if (state === 'speaking') meter.cut('talked over')
        hearing = true
        on.hearing?.(true)
        set('listening')
        break
      case 'input_audio_buffer.speech_stopped':
        hearing = false
        on.hearing?.(false)
        meter.stopped(lastVoice.last)
        reply = ''
        set('thinking')
        break
      case 'conversation.item.input_audio_transcription.completed':
        if (ev.transcript?.trim()) on.transcript?.(ev.transcript.trim())
        break
      case 'response.output_audio_transcript.delta':
      case 'response.audio_transcript.delta':
        meter.words()
        reply += ev.delta || ''
        on.delta?.(ev.delta || '')
        break
      case 'output_audio_buffer.started':
        meter.audio()
        set('speaking')
        break
      case 'output_audio_buffer.stopped':
        meter.done()
        on.done?.({ type: 'done', text: reply.trim(), hangUp: endAfter && !asking })
        if (asking) { reply = ''; set('thinking') } else afterAnswer()
        break
      case 'output_audio_buffer.cleared':
        meter.cut('cleared')
        if (!hearing) afterAnswer()
        break
      case 'error':
        clientLog('realtime.error', { code: ev.error?.code, msg: String(ev.error?.message || '').slice(0, 160) })
        break
      default:
    }
  }

  const onFrame = (f) => {
    switch (f.type) {
      case 'asking': asking = true; on.asking?.(true); break
      case 'asked': asking = false; on.asking?.(false); break
      case 'budget': on.budget?.(f.pct); break
      case 'end': endAfter = true; break
      case 'fallback': fallback(f.reason, f.message); break
      default:
    }
  }

  const tick = () => {
    if (mic) {
      const r = rmsOf(mic.analyser, mic.buf)
      micLevel = Math.min(1, r * 8)
      lastVoice.feed(listening ? r : 0, performance.now())
    }
    if (remote) {
      const r = rmsOf(remote.analyser, remote.buf)
      outLevel = Math.min(1, r * 8)
      if (state === 'speaking') meter.level(r)
    }
    if (pc && ['disconnected', 'failed'].includes(pc.connectionState)) {
      disconnectedAt ??= performance.now()
      if (pc.connectionState === 'failed' || performance.now() - disconnectedAt > 4000) fallback('webrtc', 'The connection to OpenAI dropped.')
    } else {
      disconnectedAt = null
    }
  }

  const waitFor = (what, ms) => new Promise((resolve) => {
    const t = setTimeout(() => resolve(null), ms)
    what((v) => { clearTimeout(t); resolve(v) })
  })

  const ready = (async () => {
    try {
      unlockAudio()
      ws = new WebSocket(lineUrl())
      let answer = null
      const answered = waitFor((done) => {
        ws.onmessage = (m) => {
          let f
          try { f = JSON.parse(m.data) } catch { return }
          if (f.type === 'answer' && !answer) { answer = f; done(f); return }
          if (f.type === 'fallback' && !answer) { done(f); return }
          onFrame(f)
        }
        ws.onclose = () => { if (!answer) done(null); else if (!closed) fallback('line', 'The PC ended the realtime call.') }
      }, CONNECT_MS)
      mic = await micStream()
      if (closed) return false
      mic.buf = mic.buf || new Float32Array(mic.analyser.fftSize)
      pc = new RTCPeerConnection()
      pc.addTrack(mic.stream.getAudioTracks()[0], mic.stream)
      audio = new Audio()
      audio.autoplay = true
      audio.setAttribute('playsinline', '')
      pc.ontrack = (e) => {
        audio.srcObject = e.streams[0]
        audio.play().catch(() => on.needTap?.())
        try {
          const ctx = outputContext()
          const analyser = ctx.createAnalyser()
          analyser.fftSize = 512
          ctx.createMediaStreamSource(e.streams[0]).connect(analyser)   // only measured, not played twice
          remote = { analyser, buf: new Float32Array(analyser.fftSize) }
        } catch (err) { clientLog('realtime.level_error', { err: String(err).slice(0, 120) }) }
      }
      dc = pc.createDataChannel('oai-events')
      dc.onmessage = (m) => onEvent(m.data)
      const opened = waitFor((done) => { dc.onopen = () => done(true) }, CONNECT_MS)
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      await waitFor((done) => { if (ws.readyState === 1) done(true); else ws.onopen = () => done(true) }, CONNECT_MS)
      ws.send(JSON.stringify({ type: 'offer', sdp: offer.sdp, path, call_id: callId }))
      const got = await answered
      if (closed) return false
      if (!got || got.type !== 'answer') { fallback(got?.reason || 'line', got?.message || 'No answer from the PC.'); return false }
      meter.model(got.short || got.model)
      await pc.setRemoteDescription({ type: 'answer', sdp: got.sdp })
      if (!(await opened)) { fallback('webrtc', 'Couldn’t reach OpenAI from this phone.'); return false }
      timer = setInterval(tick, FRAME_MS)
      on.engine?.('realtime')
      clientLog('realtime.start', { path, model: got.model, ...device() })
      set('listening')
      return true
    } catch (err) {
      clientLog('realtime.start_error', { err: `${err?.name}: ${err?.message}`.slice(0, 160) })
      fallback('webrtc', err?.name === 'NotAllowedError' ? 'Microphone not allowed.' : 'Realtime voice couldn’t start.')
      return false
    }
  })()

  return {
    ready,
    engine: 'realtime',
    get state() { return state },
    level() { return state === 'speaking' ? outLevel : state === 'listening' ? micLevel : 0 },
    /* You tapped "done": your turn is over now. */
    finish() {
      if (!hearing) return false
      sendEvent({ type: 'input_audio_buffer.commit' })
      sendEvent({ type: 'response.create' })
      return true
    },
    /* Kairo says this (a greeting), word for word. */
    say(text) {
      sendEvent({ type: 'response.create', response: { instructions: `Say exactly this, nothing else: ${JSON.stringify(text)}` } })
    },
    stop() {
      sendEvent({ type: 'response.cancel' })
      sendEvent({ type: 'output_audio_buffer.clear' })
      meter.cut('tapped')
      afterAnswer()
    },
    pause() {
      listening = false
      mic?.stream.getAudioTracks().forEach((t) => { t.enabled = false })
      if (audio) audio.muted = true
      sendEvent({ type: 'response.cancel' })
      sendEvent({ type: 'output_audio_buffer.clear' })
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
      unlockAudio()
      if (!audio) return false
      audio.play().catch(() => {})
      return true
    },
    close() {
      if (closed) return
      closed = true
      meter.end('hung up')
      teardown()
      closeMic()
    },
  }
}

/* The best line there is: realtime when the PC says so, else (or as soon as realtime fails) local. */
export function startLine({ path = 'call', callId = null, on = {}, status = null } = {}) {
  let cur = null
  let closed = false
  unlockAudio()                 // still inside the tap: iOS lets Kairo's voice play later
  const oldLine = () => {
    if (closed) return null
    cur = startVoice({ path, callId, on })
    return cur.ready
  }
  // Kairo's own voice: over WebRTC when the PC and this phone can (Voice V3), else the old line
  const local = (why) => {
    if (closed) return null
    on.engine?.('local', why)
    if (!rtcSupported()) return oldLine()
    return (async () => {
      const st = await rtcStatus()
      if (closed) return false
      if (!st?.available) return oldLine()
      let fallen = null
      const rtc = startRtc({ path, callId, on: { ...on, fallback: (w) => { clientLog('rtc.old_line', { reason: w?.reason }); fallen = oldLine() } } })
      cur = rtc
      const ok = await rtc.ready
      return ok ? true : (fallen ?? oldLine())
    })()
  }
  const ready = (async () => {
    const st = status ?? await realtimeStatus()
    if (closed) return false
    if (!st?.available) return local(st ? { reason: st.reason, message: st.message } : null)
    let fallen = null
    const rt = startRealtime({
      path, callId, on: {
        ...on,
        fallback: (why) => { fallen = local(why) },
      },
    })
    cur = rt
    const ok = await rt.ready
    return ok ? true : (fallen ?? local({ reason: 'webrtc' }))
  })()
  return {
    ready,
    get engine() { return cur?.engine || 'local' },
    get state() { return cur?.state || 'idle' },
    level() { return cur?.level() || 0 },
    finish() { return cur?.finish() || false },
    say(text) { cur?.say(text) },
    stop() { cur?.stop() },
    pause() { cur?.pause() },
    resume() { cur?.resume() },
    tapToPlay() { return cur?.tapToPlay() || false },
    close() { closed = true; cur?.close() },
  }
}
