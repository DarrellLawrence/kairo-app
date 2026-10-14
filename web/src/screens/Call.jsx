import { motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { IconMic, IconPhone } from '../ui/index.js'
import { speak, startRecording, stopSpeaking, transcribe, unlockAudio } from '../voice.js'

const BYE = /^(bye|goodbye|that'?s all|thanks,? bye|tsch(ü|ue)ss|ciao|adieu|auf wiederh|das war'?s|danke,? das war'?s)\b/i
const LABEL = { connecting: 'Connecting…', ringing: 'is calling…', speaking: 'Speaking', listening: 'Listening…', thinking: 'Thinking…', muted: 'Muted', ended: 'Call ended', missed: 'Missed call', error: '' }

function wsUrl() {
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/chat`
}

// Hung up mid-sentence: Kairo still gets it, as a voice note (v7).
function sendAsVoiceNote(blob) {
  if (!blob || !blob.size) return
  fetch('/api/voice-notes', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/mp4' }, body: blob, keepalive: blob.size < 60000 })
    .catch(() => {})
}

export default function Call({ callId: initialId, onClose }) {
  const outgoing = initialId === 'new'   // "Call Kairo" Shortcut: no ringing, mic on at once
  const [call, setCall] = useState(null)
  const [phase, setPhase] = useState(initialId === 'new' ? 'connecting' : 'ringing')
  const [log, setLog] = useState([])
  const [err, setErr] = useState('')
  const [level, setLevel] = useState(0)
  const live = useRef({ on: false, ringing: false, rec: null, ws: null, empty: 0, abort: null })
  const idRef = useRef(outgoing ? null : initialId)
  const logRef = useRef(null)

  useEffect(() => {
    if (!outgoing) return
    api('/api/voice/calls', { method: 'POST', body: { outgoing: true } }).then((c) => {
      setCall(c)
      idRef.current = c.id
      start(false)
    }).catch((e) => { setErr(e.message); setPhase('error') })
    // iPhone plays sound only after a tap: the first touch anywhere unlocks Kairo's voice.
    const unlock = () => unlockAudio()
    document.addEventListener('touchend', unlock, { once: true })
    return () => document.removeEventListener('touchend', unlock)
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (outgoing) return
    api(`/api/voice/calls/${initialId}`).then((c) => {
      setCall(c)
      if (c.status === 'missed' || c.status === 'ended') setPhase(c.status); else live.current.ringing = true
    })
      .catch((e) => { setErr(e.message); setPhase('error') })
  }, [initialId])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { logRef.current?.scrollTo(0, 1e6) }, [log])
  useEffect(() => () => hangup(false), [])   // eslint-disable-line react-hooks/exhaustive-deps

  const say = async (text) => {
    if (!live.current.on) return
    setPhase('speaking')
    setLog((l) => [...l, { who: 'kairo', text }])
    const abort = new AbortController()
    live.current.abort = abort
    try { await speak(text, { signal: abort.signal }) } catch (e) { setErr(e.message) }
  }

  const ask = (text) => new Promise((resolve) => {
    const ws = live.current.ws
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data)
      if (m.type === 'done' || m.type === 'error') {
        ws.removeEventListener('message', onMsg)
        resolve(m.type === 'done' ? m.text : (m.message || 'Sorry, something went wrong.'))
      }
    }
    ws.addEventListener('message', onMsg)
    ws.send(JSON.stringify({ type: 'message', text: `(spoken, on a call) ${text}` }))
  })

  const listen = useCallback(async () => {
    if (!live.current.on) return
    setPhase('listening')
    try {
      live.current.rec = await startRecording({
        autoStop: true,
        onAutoStop: async (blob) => {
          if (!live.current.on) { sendAsVoiceNote(blob); return }
          setPhase('thinking')
          let heard = ''
          try { heard = (await transcribe(blob)).text?.trim() || '' } catch (e) { setErr(e.message) }
          if (!live.current.on) { if (heard) sendAsVoiceNote(blob); return }   // hung up while Kairo was listening
          if (!heard) {
            live.current.empty += 1
            if (live.current.empty >= 3) { await say('I can’t hear you. Let’s talk later.'); hangup(true); return }
            listen()
            return
          }
          live.current.empty = 0
          setLog((l) => [...l, { who: 'me', text: heard }])
          if (BYE.test(heard)) { await say('Bye!'); hangup(true); return }
          const reply = await ask(heard)
          await say(reply)
          listen()
        },
      })
      const tick = () => { if (live.current.rec && live.current.on) { setLevel(live.current.rec.level()); requestAnimationFrame(tick) } }
      tick()
    } catch {
      setErr('Microphone not allowed. iPhone Settings → Safari → Microphone.')
      setPhase('error')
    }
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  const start = async (greet) => {
    live.current.on = true
    const ws = new WebSocket(wsUrl())
    live.current.ws = ws
    if (greet) await api(`/api/voice/calls/${idRef.current}/answer`, { method: 'POST', body: {} }).catch(() => {})
    await new Promise((r) => { ws.onopen = r; setTimeout(r, 4000) })
    if (greet) await say(call?.opening || 'Hi, it’s Kairo.')
    listen()
  }

  const answer = () => {
    unlockAudio()   // must happen inside the tap
    start(true)
  }

  // Uses only refs and props: listen() keeps an old copy of this function.
  const hangup = (goBack = true) => {
    const l = live.current
    const wasLive = l.on || l.ringing
    l.on = false
    l.ringing = false
    if (l.rec?.heard?.()) l.rec.stop().then(sendAsVoiceNote)   // still talking: keep what was said
    else l.rec?.cancel()
    l.abort?.abort()
    stopSpeaking()
    l.ws?.close()
    if (wasLive && idRef.current) api(`/api/voice/calls/${idRef.current}/end`, { method: 'POST', body: {} }).catch(() => {})
    if (goBack) { setPhase('ended'); setTimeout(onClose, 700) }
  }

  const toggleMute = () => {
    if (phase === 'muted') { listen(); return }
    live.current.rec?.cancel()
    live.current.rec = null
    setPhase('muted')
  }

  const ringing = phase === 'ringing'
  return (
    <div className="k-call">
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <motion.div className="k-call-orb"
          animate={ringing ? { scale: [1, 1.08, 1] } : phase === 'speaking' ? { scale: [1, 1.05, 0.98, 1.04, 1] } : { scale: 1 + level * 0.25 }}
          transition={ringing || phase === 'speaking' ? { duration: 1.4, repeat: Infinity } : { type: 'spring', stiffness: 300, damping: 20 }} />
        <p className="k-call-name">Kairo</p>
        <p className="k-call-sub">{phase === 'error' ? err : LABEL[phase]}{ringing && call ? ` · ${call.reason}` : ''}</p>
        {err && phase !== 'error' && <p className="k-call-sub" style={{ color: 'var(--warn)' }}>{err}</p>}
      </div>
      <div className="k-call-log" ref={logRef}>
        {log.map((m, i) => <p key={i} className={m.who === 'me' ? 'me' : ''}>{m.text}</p>)}
      </div>
      <div className="k-call-buttons">
        {ringing ? (
          <>
            <motion.button className="k-call-btn hangup" whileTap={{ scale: 0.9 }} onClick={() => hangup(true)} aria-label="Decline"><IconPhone /></motion.button>
            <motion.button className="k-call-btn answer" whileTap={{ scale: 0.9 }} onClick={answer} aria-label="Answer"><IconPhone /></motion.button>
          </>
        ) : ['missed', 'ended', 'error'].includes(phase) ? (
          <motion.button className="k-call-btn mute" whileTap={{ scale: 0.9 }} onClick={onClose} aria-label="Close" style={{ width: 'auto', padding: '0 24px', borderRadius: 36 }}>Close</motion.button>
        ) : (
          <>
            <motion.button className="k-call-btn mute" whileTap={{ scale: 0.9 }} onClick={toggleMute} aria-label="Mute"
              style={{ opacity: phase === 'muted' ? 1 : 0.8, background: phase === 'muted' ? 'var(--warn)' : undefined }}><IconMic /></motion.button>
            <motion.button className="k-call-btn hangup" whileTap={{ scale: 0.9 }} onClick={() => hangup(true)} aria-label="Hang up"><IconPhone /></motion.button>
          </>
        )}
      </div>
    </div>
  )
}
