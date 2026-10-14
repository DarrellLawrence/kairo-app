// Voice call with Kairo (used daily). Full screen, the orb is the interface:
// it follows your voice while listening and Kairo's while speaking. Talk over
// Kairo to interrupt, tap the orb to pause, hang up any time: whatever you
// were still saying is sent as a voice note and handled anyway.
import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import { IconCaptions, IconMic, IconPhone } from '../ui/index.js'
import { VoiceOrb } from '../ui/Orb.jsx'
import { holdMic, listenLevel, releaseMic, speak, speakingLevel, startRecording, stopSpeaking, transcribe, unlockAudio } from '../voice.js'

const BYE = /^(bye|goodbye|that'?s all|thanks,? bye|tsch(ü|ue)ss|ciao|adieu|auf wiederh|das war'?s|danke,? das war'?s)\b/i
const LABEL = {
  connecting: 'Connecting', ringing: 'Kairo is calling', listening: 'Listening', thinking: 'Thinking',
  speaking: 'Speaking', paused: 'Paused', muted: 'Muted', ended: 'Call ended', missed: 'Missed call', error: 'Something went wrong',
}
const HINT = {
  listening: 'Just talk. Pause when you are done.', thinking: '', speaking: 'Talk to interrupt',
  paused: 'Tap the orb to go on', muted: 'Kairo can’t hear you',
}
const BARGE_LEVEL = 0.35   // louder than Kairo's echo through the speaker
const BARGE_TICKS = 4      // x 80 ms of talking before Kairo stops

function wsUrl() {
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/chat`
}

// Hung up mid-sentence: Kairo still gets it, as a voice note (v7).
function sendAsVoiceNote(blob) {
  if (!blob || !blob.size) return
  fetch('/api/voice-notes', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/mp4' }, body: blob, keepalive: blob.size < 60000 })
    .catch(() => {})
}

const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

export default function Call({ callId: initialId, onClose }) {
  const outgoing = initialId === 'new'   // "Call Kairo": no ringing, mic on at once
  const [call, setCall] = useState(null)
  const [phase, setPhase] = useState(outgoing ? 'connecting' : 'ringing')
  const [log, setLog] = useState([])
  const [err, setErr] = useState('')
  const [secs, setSecs] = useState(0)
  const [captions, setCaptions] = useState(() => { try { return localStorage.getItem('call.captions') !== '0' } catch { return true } })
  const live = useRef({ on: false, ringing: false, rec: null, monitor: null, ws: null, empty: 0, abort: null, phase: 'connecting', started: 0 })
  const idRef = useRef(outgoing ? null : initialId)
  const set = (p) => { live.current.phase = p; setPhase(p) }

  useEffect(() => {
    if (!outgoing) return
    api('/api/voice/calls', { method: 'POST', body: { outgoing: true } }).then((c) => {
      setCall(c)
      idRef.current = c.id
      start(false)
    }).catch((e) => { setErr(e.message); set('error') })
    // iPhone plays sound only after a tap: the first touch anywhere unlocks Kairo's voice.
    const unlock = () => unlockAudio()
    document.addEventListener('touchend', unlock, { once: true })
    return () => document.removeEventListener('touchend', unlock)
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (outgoing) return
    api(`/api/voice/calls/${initialId}`).then((c) => {
      setCall(c)
      if (c.status === 'missed' || c.status === 'ended') set(c.status); else live.current.ringing = true
    }).catch((e) => { setErr(e.message); set('error') })
  }, [initialId])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => hangup(false), [])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!['listening', 'thinking', 'speaking', 'paused', 'muted'].includes(phase)) return undefined
    const t = setInterval(() => setSecs(Math.round((Date.now() - live.current.started) / 1000)), 1000)
    return () => clearInterval(t)
  }, [phase])

  const getLevel = useCallback(() => {
    const l = live.current
    if (l.phase === 'listening') return l.rec?.level?.() || 0
    if (l.phase === 'speaking') return speakingLevel()
    if (l.phase === 'thinking') return 0.12 + Math.sin(performance.now() / 260) * 0.06
    return 0
  }, [])

  const stopMonitor = () => { live.current.monitor?.cancel(); live.current.monitor = null }

  // While Kairo speaks, a second ear listens: talking over it stops the speech.
  const watchForBargeIn = async (abort) => {
    try {
      const mon = await listenLevel()
      if (live.current.phase !== 'speaking') { mon.cancel(); return }
      live.current.monitor = mon
      let ticks = 0
      const tick = setInterval(() => {
        if (live.current.monitor !== mon) { clearInterval(tick); return }
        ticks = mon.level() > BARGE_LEVEL ? ticks + 1 : 0
        if (ticks >= BARGE_TICKS) { clearInterval(tick); live.current.interrupted = true; abort.abort() }
      }, 80)
    } catch { /* no second ear: you can still tap the orb */ }
  }

  const say = async (text) => {
    if (!live.current.on) return
    setLog((l) => [...l, { who: 'kairo', text }])
    if (live.current.phase === 'paused') return   // paused while Kairo was thinking: the answer waits in the captions
    set('speaking')
    const abort = new AbortController()
    live.current.abort = abort
    live.current.interrupted = false
    watchForBargeIn(abort)
    try { await speak(text, { signal: abort.signal }) } catch (e) { if (e.name !== 'AbortError') setErr(e.message) }
    stopMonitor()
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
    const l = live.current
    if (!l.on || l.phase === 'paused' || l.phase === 'muted') return
    set('listening')
    try {
      l.rec = await startRecording({
        autoStop: true,
        onAutoStop: async (blob) => {
          if (!live.current.on) { sendAsVoiceNote(blob); return }
          if (live.current.phase !== 'listening') return   // paused or muted meanwhile
          set('thinking')
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
          setLog((x) => [...x, { who: 'me', text: heard }])
          if (BYE.test(heard)) { await say('Bye!'); hangup(true); return }
          const reply = await ask(heard)
          await say(reply)
          if (live.current.phase === 'speaking' || live.current.interrupted) listen()
        },
      })
    } catch {
      setErr('Microphone not allowed. iPhone Settings → Apps → Safari → Microphone.')
      set('error')
    }
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  const start = async (greet) => {
    live.current.on = true
    live.current.started = Date.now()
    const ws = new WebSocket(wsUrl())
    live.current.ws = ws
    // Everything at once: the mic (asked once for the whole call), the chat line and the answer.
    const micReady = holdMic().catch(() => {})
    const wsReady = new Promise((r) => { ws.onopen = r; ws.onerror = r; setTimeout(r, 4000) })
    if (greet) await api(`/api/voice/calls/${idRef.current}/answer`, { method: 'POST', body: {} }).catch(() => {})
    await Promise.all([micReady, wsReady])
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
    if (l.rec?.heard?.()) l.rec.stop().then((b) => { releaseMic(); sendAsVoiceNote(b) })   // still talking: keep what was said
    else { l.rec?.cancel(); releaseMic() }
    l.monitor?.cancel()
    l.abort?.abort()
    stopSpeaking()
    l.ws?.close()
    if (wasLive && idRef.current) api(`/api/voice/calls/${idRef.current}/end`, { method: 'POST', body: {} }).catch(() => {})
    if (goBack) { set('ended'); setTimeout(onClose, 700) }
  }

  const tapOrb = () => {
    unlockAudio()
    const l = live.current
    if (!l.on) return
    if (l.phase === 'paused') { set('listening'); listen(); return }
    if (['listening', 'speaking', 'thinking'].includes(l.phase)) {
      l.rec?.cancel()
      l.rec = null
      stopMonitor()
      l.abort?.abort()
      stopSpeaking()
      set('paused')
    }
  }

  const toggleMute = () => {
    const l = live.current
    if (l.phase === 'muted') { set('listening'); listen(); return }
    l.rec?.cancel()
    l.rec = null
    set('muted')
  }

  const toggleCaptions = () => {
    setCaptions((c) => { try { localStorage.setItem('call.captions', c ? '0' : '1') } catch { /* private */ } return !c })
  }

  const ringing = phase === 'ringing'
  const over = ['missed', 'ended', 'error'].includes(phase)
  const orbState = ['listening', 'thinking', 'speaking', 'paused'].includes(phase) ? phase : phase === 'muted' ? 'paused' : 'idle'
  return (
    <motion.div className="k-call" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.25 }}>
      <div className="k-call-top">
        <span className="k-call-timer" style={{ margin: '0 auto' }}>{over || ringing || phase === 'connecting' ? 'Kairo' : mmss(secs)}</span>
      </div>

      <div className="k-call-stage">
        <motion.button type="button" className="k-call-orb" onClick={tapOrb} aria-label={phase === 'paused' ? 'Go on' : 'Pause'}
          initial={{ scale: 0.7, opacity: 0 }} animate={{ scale: 1, opacity: over ? 0.35 : 1 }} whileTap={{ scale: 0.96 }} transition={spring.smooth}>
          <VoiceOrb state={orbState} getLevel={getLevel} />
        </motion.button>
        <AnimatePresence mode="wait">
          <motion.p key={phase} className="k-call-state" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.2 }}>
            {LABEL[phase]}
          </motion.p>
        </AnimatePresence>
        <p className="k-call-sub">{phase === 'error' ? err : ringing && call ? call.reason : HINT[phase] || ''}</p>
        {err && phase !== 'error' && <p className="k-call-sub" style={{ color: 'var(--warn)' }}>{err}</p>}
        {captions && log.length > 0 && (
          <div className="k-call-caption selectable" aria-live="polite">
            {log.slice(-3).map((m, i) => <p key={log.length - 3 + i} className={m.who === 'me' ? 'me' : ''}>{m.text}</p>)}
          </div>
        )}
      </div>

      <div className="k-call-buttons">
        {ringing ? (
          <>
            <motion.button className="k-call-btn hangup" whileTap={{ scale: 0.9 }} onClick={() => hangup(true)} aria-label="Decline"><IconPhone /></motion.button>
            <motion.button className="k-call-btn answer" whileTap={{ scale: 0.9 }} onClick={answer} aria-label="Answer"><IconPhone /></motion.button>
          </>
        ) : over ? (
          <motion.button className="k-call-btn side" whileTap={{ scale: 0.9 }} onClick={onClose} aria-label="Close" style={{ width: 'auto', padding: '0 28px', borderRadius: 36 }}>Close</motion.button>
        ) : (
          <>
            <motion.button className={phase === 'muted' ? 'k-call-btn mute on' : 'k-call-btn mute'} whileTap={{ scale: 0.9 }} onClick={toggleMute} aria-label={phase === 'muted' ? 'Unmute' : 'Mute'} aria-pressed={phase === 'muted'}><IconMic /></motion.button>
            <motion.button className="k-call-btn hangup" whileTap={{ scale: 0.9 }} onClick={() => hangup(true)} aria-label="Hang up"><IconPhone /></motion.button>
            <motion.button className={captions ? 'k-call-btn mute on' : 'k-call-btn mute'} aria-pressed={captions} whileTap={{ scale: 0.9 }} onClick={toggleCaptions} aria-label="Captions"><IconCaptions /></motion.button>
          </>
        )}
      </div>
    </motion.div>
  )
}
