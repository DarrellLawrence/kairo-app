// Voice in the browser: record (MediaRecorder), detect the end of speech
// (simple level meter), send to the PC for speech-to-text, play Kairo's voice.
// All speech processing happens on kairo-home.
//
// Why a reply could be silent on the iPhone (fix A1, 2026-10-14): while the mic is open, iOS puts
// the page in its "call" audio route (play-and-record), which plays through the earpiece at call
// volume. So: chat replies close the mic first and play in the "playback" session (loudspeaker or
// AirPods, also with the silent switch on); calls set the session explicitly; and when nothing is
// heard, "No sound? Tap to play" replays through the speaker inside the tap and remembers, for
// this phone, to pause the mic while Kairo talks on calls. Every step is logged to the PC.
import { clientLog, device } from './clientlog.js'

let outCtx = null      // Kairo's voice: one AudioContext, started inside a tap
let element = null     // <audio> fallback, primed inside a tap
let current = null     // the reply playing now: { stop() }
let last = null        // the last reply, for "tap to play": { buf, call }
const RELEASE_KEY = 'voice.release-mic-while-speaking'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const errName = (e) => `${e?.name || 'Error'}: ${e?.message || e}`.slice(0, 160)

export class NeedTap extends Error {
  constructor(why) { super('No sound? Tap to play'); this.name = 'NeedTap'; this.why = why }
}

/* Calls on this phone: pause the mic while Kairo talks (set by "tap to play" or the sound check). */
export function releaseWhileSpeaking() {
  try { return localStorage.getItem(RELEASE_KEY) === '1' } catch { return false }
}
export function setReleaseWhileSpeaking(on) {
  try { localStorage.setItem(RELEASE_KEY, on ? '1' : '0') } catch { /* private mode */ }
  clientLog('audio.release_mode', { on })
}

export function setSession(type) {
  if (!('audioSession' in navigator)) return
  try { if (navigator.audioSession.type !== type) navigator.audioSession.type = type } catch (e) { clientLog('audio.session_error', { type, err: errName(e) }) }
}

function silentWav(seconds = 0.05, rate = 8000) {
  const n = Math.round(seconds * rate)
  const b = new DataView(new ArrayBuffer(44 + n * 2))
  const str = (o, t) => { for (let i = 0; i < t.length; i++) b.setUint8(o + i, t.charCodeAt(i)) }
  str(0, 'RIFF'); b.setUint32(4, 36 + n * 2, true); str(8, 'WAVEfmt '); b.setUint32(16, 16, true)
  b.setUint16(20, 1, true); b.setUint16(22, 1, true); b.setUint32(24, rate, true); b.setUint32(28, rate * 2, true)
  b.setUint16(32, 2, true); b.setUint16(34, 16, true); str(36, 'data'); b.setUint32(40, n * 2, true)
  return URL.createObjectURL(new Blob([b.buffer], { type: 'audio/wav' }))
}
let SILENCE = null

/* iOS lets a page start sound only inside a tap: call this in every tap that can lead to Kairo
   talking. Starts Kairo's AudioContext and primes the <audio> fallback with real (silent) samples. */
export function unlockAudio() {
  setSession(mic ? 'play-and-record' : 'playback')
  try {
    if (!outCtx || outCtx.state === 'closed') outCtx = new (window.AudioContext || window.webkitAudioContext)()
    if (outCtx.state !== 'running') outCtx.resume().catch(() => {})
    const src = outCtx.createBufferSource()
    src.buffer = outCtx.createBuffer(1, 1, outCtx.sampleRate)
    src.connect(outCtx.destination)
    src.start(0)
  } catch (e) { clientLog('audio.unlock_error', { err: errName(e) }) }
  if (!element) { element = new Audio(); element.playsInline = true; element.preload = 'auto' }
  if (!current) {
    SILENCE = SILENCE || silentWav()
    element.src = SILENCE
    element.play().catch(() => {})
  }
}

async function play(buf, { signal, call = false, keepMic = false, viaTap = false, fetchMs = null, onPlaying } = {}) {
  const micWasOpen = Boolean(mic)
  if (mic && !keepMic && (!call || viaTap || releaseWhileSpeaking())) {
    closeMic()
    await sleep(350)            // iOS keeps the call route for a moment after the mic stops
  }
  setSession(mic ? 'play-and-record' : 'playback')
  const info = { ...device(), call, viaTap, micWasOpen, micOpen: Boolean(mic), bytes: buf.byteLength, fetchMs }

  if (outCtx && outCtx.state !== 'running') await Promise.race([outCtx.resume().catch(() => {}), sleep(400)])
  if (outCtx && outCtx.state === 'running') {
    try {
      const audio = await outCtx.decodeAudioData(buf.slice(0))
      clientLog('audio.play', { ...info, path: 'webaudio', rate: audio.sampleRate, secs: Number(audio.duration.toFixed(2)) })
      await new Promise((resolve) => {
        const src = outCtx.createBufferSource()
        src.buffer = audio
        src.connect(outCtx.destination)
        const done = () => { if (current?.src === src) current = null; resolve() }
        current = { src, stop: () => { try { src.stop() } catch { /* already stopped */ } done() } }
        src.onended = done
        signal?.addEventListener('abort', () => current?.src === src && current.stop())
        src.start()
        onPlaying?.()
      })
      return
    } catch (e) { clientLog('audio.webaudio_error', { ...info, err: errName(e) }) }
  }

  // Web Audio not running (no tap yet, or iOS interrupted it): the <audio> element, watched.
  if (!element) { element = new Audio(); element.playsInline = true }
  const el = element
  const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }))
  try {
    await new Promise((resolve, reject) => {
      let progressed = false
      const finish = (err) => {
        clearTimeout(watchdog)
        el.onended = el.onerror = el.ontimeupdate = null
        URL.revokeObjectURL(url)
        if (current?.el === el) current = null
        if (err) { el.pause(); reject(err) } else resolve()
      }
      const watchdog = setTimeout(() => { if (!progressed) finish(new NeedTap('no progress')) }, 2500)
      el.ontimeupdate = () => { if (el.currentTime > 0) progressed = true }
      el.onended = () => finish()
      el.onerror = () => finish(new NeedTap(`media error ${el.error?.code}`))
      signal?.addEventListener('abort', () => finish())
      current = { el, stop: () => finish() }
      el.src = url
      el.play().then(() => { onPlaying?.(); clientLog('audio.play', { ...info, path: 'element', ctx: outCtx?.state ?? 'none' }) })
        .catch((e) => finish(new NeedTap(errName(e))))
    })
  } catch (e) {
    clientLog('audio.blocked', { ...info, ctx: outCtx?.state ?? 'none', why: e.why || errName(e) })
    throw e
  }
}

/* Speak a reply. call=true keeps the call's mic open (unless this phone needs it paused).
   keepMic forces the mic to stay open (the sound check's call test).
   Throws NeedTap when iOS didn't let it play: show "No sound? Tap to play" -> playLastAgain(). */
export async function speak(text, { signal, call = false, keepMic = false, onFetched, onPlaying } = {}) {
  const t0 = performance.now()
  const buf = await fetchSpeech(text, signal)
  onFetched?.()
  last = { buf: buf.slice(0), call }
  return play(buf, { signal, call, keepMic, fetchMs: Math.round(performance.now() - t0), onPlaying })
}

async function fetchSpeech(text, signal) {
  const res = await fetch('/api/voice/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal,
  })
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || `Voice failed (${res.status})`
    clientLog('audio.speak_failed', { status: res.status, detail: String(detail).slice(0, 160) })
    throw new Error(detail)
  }
  return res.arrayBuffer()
}

/* "No sound? Tap to play": inside the tap, through the speaker, without the mic. On a call that
   means the call route was the problem, so this phone pauses the mic while Kairo talks from now on. */
export async function playLastAgain({ signal } = {}) {
  if (!last) return
  stopSpeaking()
  if (mic) closeMic()
  setSession('playback')
  unlockAudio()
  clientLog('audio.tap_to_play', { ...device(), call: last.call })
  if (last.call && !releaseWhileSpeaking()) setReleaseWhileSpeaking(true)
  await play(last.buf.slice(0), { signal, call: last.call, viaTap: true })
}

export function stopSpeaking() {
  current?.stop()
}

export async function transcribe(blob, language) {
  const res = await fetch(`/api/voice/transcribe${language ? `?language=${language}` : ''}`, {
    method: 'POST', headers: { 'Content-Type': blob.type || 'audio/mp4' }, body: blob,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `Speech recognition failed (${res.status})`)
  return data
}

function pickType() {
  for (const t of ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']) {
    if (window.MediaRecorder?.isTypeSupported?.(t)) return t
  }
  return ''
}

/* One microphone, opened once and reused. iPhone asks for permission each time a
   page opens the mic anew, so a call keeps it open (holdMic) until hang-up, and
   the chat mic keeps it for a minute after you stop talking. */
let mic = null
let held = false
let releaseTimer = null
const KEEP_MIC_MS = 60000

async function openMic() {
  clearTimeout(releaseTimer)
  if (mic && mic.stream.getAudioTracks().some((t) => t.readyState === 'live')) {
    if (mic.ctx.state === 'suspended') await mic.ctx.resume().catch(() => {})
    return mic
  }
  closeMic()
  setSession('play-and-record')
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
  const ctx = new (window.AudioContext || window.webkitAudioContext)()
  const analyser = ctx.createAnalyser()
  analyser.fftSize = 1024
  ctx.createMediaStreamSource(stream).connect(analyser)
  mic = { stream, ctx, analyser, buf: new Float32Array(analyser.fftSize) }
  return mic
}

export function closeMic() {
  clearTimeout(releaseTimer)
  if (!mic) return
  mic.stream.getTracks().forEach((t) => t.stop())
  mic.ctx.close().catch(() => {})
  mic = null
  setSession('playback')
}

function idleMic() {
  clearTimeout(releaseTimer)
  if (!held) releaseTimer = setTimeout(closeMic, KEEP_MIC_MS)
}

/* The open mic ({ stream, ctx }) for streaming it (voicelink.js). */
export async function micStream() { return openMic() }

/* Kairo's AudioContext (started by unlockAudio inside a tap). */
export function outputContext() {
  if (!outCtx || outCtx.state === 'closed') unlockAudio()
  return outCtx
}

/* A call holds the mic for its whole length; releaseMic() at hang-up turns it off. */
export async function holdMic() { held = true; await openMic() }
export function releaseMic() { held = false; closeMic() }

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => { if (document.hidden && !held) closeMic() })
}

function levelOf(m) {
  m.analyser.getFloatTimeDomainData(m.buf)
  let sum = 0
  for (let i = 0; i < m.buf.length; i++) sum += m.buf[i] * m.buf[i]
  return Math.min(1, Math.sqrt(sum / m.buf.length) * 8)
}

/* Starts recording. Returns { stop(): Promise<Blob>, cancel(), level(): 0..1, heard(): speech so far? }.
   With autoStop, it stops by itself after `silenceMs` of quiet once speech was heard,
   or after maxMs, and calls onAutoStop(blob). */
export async function startRecording({ autoStop = false, silenceMs = 1300, maxMs = 45000, onAutoStop } = {}) {
  const m = await openMic()
  const type = pickType()
  const rec = new MediaRecorder(m.stream, type ? { mimeType: type } : undefined)
  const chunks = []
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data) }
  let current = 0
  let heard = false
  let quietSince = null
  const started = performance.now()
  let finished = false

  const finish = () => new Promise((resolve) => {
    if (finished) return resolve(null)
    finished = true
    clearInterval(timer)
    rec.onstop = () => {
      idleMic()
      resolve(new Blob(chunks, { type: rec.mimeType || type || 'audio/mp4' }))
    }
    if (rec.state !== 'inactive') rec.stop(); else rec.onstop()
  })

  const timer = setInterval(() => {
    current = mic === m ? levelOf(m) : 0
    if (!autoStop) return
    const now = performance.now()
    if (current > 0.12) { heard = true; quietSince = null } else if (heard && quietSince === null) quietSince = now
    if ((heard && quietSince !== null && now - quietSince > silenceMs) || now - started > maxMs) {
      const speechEndedAt = quietSince ?? now   // fix plan 31: the wait starts when you stop talking
      finish().then((b) => b && onAutoStop?.(b, { speechEndedAt }))
    }
  }, 80)

  rec.start(250)
  return {
    stop: finish,
    cancel: () => { finish() },
    level: () => current,
    heard: () => heard,
  }
}
