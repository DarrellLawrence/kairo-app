// Voice in the browser: record (MediaRecorder), detect the end of speech
// (simple level meter), send to the PC for speech-to-text, play Kairo's voice.
// All speech processing happens on kairo-home.

let player = null

// iOS only lets a page play sound after a tap: call this inside a tap handler once.
export function unlockAudio() {
  if (!player) {
    player = new Audio()
    player.playsInline = true
  }
  player.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA='
  player.play().catch(() => {})
}

export async function speak(text, { signal } = {}) {
  const res = await fetch('/api/voice/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal,
  })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Voice failed (${res.status})`)
  const url = URL.createObjectURL(await res.blob())
  if (!player) unlockAudio()
  return new Promise((resolve) => {
    const done = () => { URL.revokeObjectURL(url); player.onended = null; resolve() }
    player.onended = done
    player.onerror = done
    signal?.addEventListener('abort', () => { player.pause(); done() })
    player.src = url
    player.play().catch(done)
  })
}

export function stopSpeaking() {
  if (player) player.pause()
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

/* Starts recording. Returns { stop(): Promise<Blob>, cancel(), level(): 0..1, heard(): speech so far? }.
   With autoStop, it stops by itself after `silenceMs` of quiet once speech was heard,
   or after maxMs, and calls onAutoStop(blob). */
export async function startRecording({ autoStop = false, silenceMs = 1300, maxMs = 45000, onAutoStop } = {}) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
  const type = pickType()
  const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined)
  const chunks = []
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data) }
  const ctx = new (window.AudioContext || window.webkitAudioContext)()
  const src = ctx.createMediaStreamSource(stream)
  const analyser = ctx.createAnalyser()
  analyser.fftSize = 1024
  src.connect(analyser)
  const buf = new Float32Array(analyser.fftSize)
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
      stream.getTracks().forEach((t) => t.stop())
      ctx.close().catch(() => {})
      resolve(new Blob(chunks, { type: rec.mimeType || type || 'audio/mp4' }))
    }
    if (rec.state !== 'inactive') rec.stop(); else rec.onstop()
  })

  const timer = setInterval(() => {
    analyser.getFloatTimeDomainData(buf)
    let sum = 0
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
    current = Math.min(1, Math.sqrt(sum / buf.length) * 8)
    if (!autoStop) return
    const now = performance.now()
    if (current > 0.12) { heard = true; quietSince = null } else if (heard && quietSince === null) quietSince = now
    if ((heard && quietSince !== null && now - quietSince > silenceMs) || now - started > maxMs) {
      finish().then((b) => b && onAutoStop?.(b))
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
