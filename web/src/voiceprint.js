// Teach Kairo your voice (owner's approval 2026-10-20). About 30 s of the owner reading aloud, recorded on
// this phone the way a call hears it (the same mic and settings, 16 kHz PCM from voicelink.js), sent once to
// the PC. The PC keeps only the voiceprint, encrypted (server/app/speaker.py); the recording is not kept.
import { startCapture } from './voicelink.js'
import { closeMic } from './voice.js'

export const SECONDS = 30
const FRAME_S = 0.02            // voicelink.js hands out 20 ms frames
const SPEECH_RMS = 0.01         // about -40 dB: louder than a quiet room

// A text anyone can read aloud in about half a minute (Aesop; the passage phoneticians use).
export const PASSAGE = 'The North Wind and the Sun were arguing about which of them was stronger, when a traveller came ' +
  'along wrapped in a warm cloak. They agreed that whoever first made the traveller take his cloak off would count as ' +
  'the stronger. The North Wind blew as hard as he could, but the harder he blew, the closer the traveller held his ' +
  'cloak, and at last the North Wind gave up. Then the Sun shone out warmly, and at once the traveller took off his ' +
  'cloak. So the North Wind had to admit that the Sun was the stronger of the two.'

/* The frames as one 16-bit buffer (what the PC takes as audio/l16). */
export function joinFrames(frames) {
  const out = new Int16Array(frames.reduce((n, f) => n + f.length, 0))
  let at = 0
  for (const f of frames) { out.set(f, at); at += f.length }
  return out
}

/* Records until `seconds` of audio are in (or stop()). onProgress({ done 0..1, speech seconds, level 0..1 }).
   Returns { done: Promise<Int16Array>, stop() }. */
export function recordVoice({ seconds = SECONDS, capture = startCapture, release = closeMic, onProgress } = {}) {
  const frames = []
  const want = Math.round(seconds / FRAME_S)
  let speech = 0
  let mic = null
  let finished = false
  let finish
  const done = new Promise((resolve, reject) => {
    finish = (err) => {
      if (finished) return
      finished = true
      try { mic?.stop() } catch { /* already gone */ }
      release?.()
      if (err) reject(err)
      else resolve(joinFrames(frames))
    }
  })
  capture((pcm, rms) => {
    if (finished) return
    frames.push(pcm)
    if (rms >= SPEECH_RMS) speech += FRAME_S
    onProgress?.({ done: Math.min(1, frames.length / want), speech: Math.round(speech * 10) / 10, level: Math.min(1, rms * 8) })
    if (frames.length >= want) finish()
  }).then((m) => { mic = m; if (finished) m.stop() }, (err) => finish(err))
  return { done, stop: () => finish() }
}

/* Sends the recording; the PC answers with the new state (taught, date) or says what to do differently. */
export async function sendVoice(pcm, fetchImpl = fetch) {
  const res = await fetchImpl('/api/voice/speaker/enroll', {
    method: 'POST', headers: { 'Content-Type': 'audio/l16; rate=16000' }, body: pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength),
  })
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`)
  return data
}
