// "+" (Today and Chat): capture, voice first (fix A2). A big mic: hold to talk, let go = sent as a
// voice note (no reply needed; Kairo does it and says what it did in one line). Tapping instead of
// holding works too: tap to start, tap to send. Then attach (Files, Photos, camera); text last.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { clientLog } from '../clientlog.js'
import { Button, IconCamera, IconFile, IconImage, IconMic, Sheet, useToast } from '../ui/index.js'
import { startRecording, stopSpeaking, unlockAudio } from '../voice.js'

const TAP_MS = 350   // shorter than this = a tap: keep recording until the next tap

async function upload(body, type, name) {
  const headers = { 'Content-Type': type || 'application/octet-stream' }
  if (name) headers['X-Kairo-Filename'] = encodeURIComponent(name)
  const res = await fetch('/api/captures', { method: 'POST', headers, body })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  return data
}

/* Waits (max ~45 s) for Kairo's one line about a capture, while the sheet may already be closed. */
async function oneLine(id) {
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    const c = await api(`/api/captures/${id}`).catch(() => null)
    if (!c) continue
    if (c.status === 'filed' || c.status === 'asking') return c.line || c.result?.reply || `Filed: ${c.title || 'done'}`
  }
  return 'Got it. Kairo is still on it; the answer lands in Chat.'
}

export function CaptureSheet({ open, onClose }) {
  const toast = useToast()
  const [state, setState] = useState('idle')   // idle | recording | sending
  const [level, setLevel] = useState(0)
  const [text, setText] = useState('')
  const [tapMode, setTapMode] = useState(false)   // started with a quick tap: the next tap sends
  const rec = useRef(null)
  const pressed = useRef(0)
  const files = useRef(null)
  const photos = useRef(null)
  const camera = useRef(null)

  useEffect(() => {
    if (state !== 'recording') return undefined
    const t = setInterval(() => setLevel(rec.current?.level?.() || 0), 80)
    return () => clearInterval(t)
  }, [state])
  useEffect(() => { if (!open && rec.current) { rec.current.cancel(); rec.current = null; setState('idle'); setTapMode(false) } }, [open])

  const begin = async () => {
    unlockAudio()
    stopSpeaking()
    pressed.current = performance.now()
    try {
      rec.current = await startRecording()
      setState('recording')
    } catch {
      toast.show({ text: 'Microphone not allowed. iPhone Settings → Apps → Safari → Microphone.' })
      setState('idle')
    }
  }

  const send = async () => {
    const r = rec.current
    rec.current = null
    setTapMode(false)
    if (!r) return
    setState('sending')
    const long = performance.now() - pressed.current
    const blob = await r.stop()
    if (!blob || blob.size < 1000 || long < 600) {   // a stray touch; quiet speech still goes (the PC drops empty notes)
      setState('idle')
      toast.show({ text: 'Didn’t hear anything.' })
      return
    }
    try {
      const res = await fetch('/api/voice-notes', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/mp4' }, body: blob })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      setState('idle')
      onClose()
      toast.show({ text: 'Sent. Kairo is on it…' })
      const line = await oneLine(data.id)
      toast.show({ text: line, duration: 6000 })
      clientLog('capture.voice', { ok: true })
    } catch (e) {
      setState('idle')
      toast.show({ text: e.message })
    }
  }

  // Hold to talk; a quick tap starts a recording that the next tap sends.
  const down = (e) => {
    e.preventDefault()
    if (state === 'idle') begin()
    else if (state === 'recording' && performance.now() - pressed.current > TAP_MS) send()
  }
  const up = (e) => {
    e.preventDefault()
    if (state === 'recording' || rec.current) {
      if (performance.now() - pressed.current >= TAP_MS) send()   // was held: let go = sent
      else setTapMode(true)
    } else if (state === 'idle') setTapMode(true)                  // let go before the mic opened
  }

  const attach = async (file) => {
    if (!file) return
    onClose()
    try {
      const r = await upload(file, file.type, file.name)
      toast.show({ text: r.message?.startsWith('Already') ? r.message : `Sent “${file.name}”…` })
      if (!r.message?.startsWith('Already')) toast.show({ text: await oneLine(r.id), duration: 6000 })
    } catch (e) { toast.show({ text: e.message }) }
  }

  const sendText = async () => {
    const t = text.trim()
    if (!t) return
    try {
      const r = await api('/api/captures', { method: 'POST', body: /^https?:\/\/\S+$/.test(t) ? { url: t } : { text: t } })
      setText('')
      onClose()
      toast.show({ text: 'Captured…' })
      toast.show({ text: await oneLine(r.id), duration: 6000 })
    } catch (e) { toast.show({ text: e.message }) }
  }

  const rec_ = state === 'recording'
  return (
    <Sheet open={open} onClose={onClose} title="Capture">
      <div className="k-capture">
        <button type="button" className={`k-capture-mic ${state}`} onPointerDown={down} onPointerUp={up}
          onPointerCancel={() => { rec.current?.cancel(); rec.current = null; setState('idle') }}
          onContextMenu={(e) => e.preventDefault()} aria-label={rec_ ? 'Send' : 'Hold to talk'}
          style={{ '--level': rec_ ? Math.min(1, level * 1.6) : 0 }} disabled={state === 'sending'}>
          {state === 'sending' ? <span className="k-spin-dot" /> : <IconMic />}
        </button>
        <p className="k-capture-hint">
          {state === 'recording' ? (tapMode ? 'Listening… tap to send' : 'Listening… let go to send') : state === 'sending' ? 'Sending…' : 'Hold to talk. Kairo does it, no reply needed.'}
        </p>
        <div className="k-capture-attach">
          <Button onClick={() => files.current?.click()} icon={<IconFile />}>Files</Button>
          <Button onClick={() => photos.current?.click()} icon={<IconImage />}>Photos</Button>
          <Button onClick={() => camera.current?.click()} icon={<IconCamera />}>Camera</Button>
        </div>
        <div className="k-capture-text">
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Or type it…" enterKeyHint="send"
            onKeyDown={(e) => { if (e.key === 'Enter') sendText() }} aria-label="Type a capture" />
          <Button size="sm" variant="primary" disabled={!text.trim()} onClick={sendText}>Send</Button>
        </div>
      </div>
      <input ref={files} type="file" hidden onChange={(e) => attach(e.target.files?.[0])} />
      <input ref={photos} type="file" accept="image/*,video/*" hidden onChange={(e) => attach(e.target.files?.[0])} />
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={(e) => attach(e.target.files?.[0])} />
    </Sheet>
  )
}
