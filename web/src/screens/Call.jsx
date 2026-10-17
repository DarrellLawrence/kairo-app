// Voice call with Kairo (used daily). Full screen, the orb is the interface:
// it follows your voice while listening and Kairo's while speaking. Talk over
// Kairo to interrupt, tap the orb to pause, hang up any time: whatever you
// were still saying is still sent, and Kairo answers it in chat.
// Voice V1: the whole call runs on one line to the PC (voicelink.js): your voice streams while you
// talk, Kairo's answer streams back sentence by sentence.
import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import { IconCaptions, IconMic, IconPhone } from '../ui/index.js'
import { VoiceOrb } from '../ui/Orb.jsx'
import { releaseWhileSpeaking, setReleaseWhileSpeaking, unlockAudio } from '../voice.js'
import { realtimeStatus, startLine } from '../realtime.js'

const HANG_UP = /\s*\[\[hang up\]\]\s*$/i
const HANG_UP_ANY = /\s*\[\[hang up\]\]\s*/gi   // the marker is never spoken or shown
const BYE = /^(bye|goodbye|that'?s all|thanks,? bye|tsch(ü|ue)ss|ciao|adieu|auf wiederh|das war'?s|danke,? das war'?s)\b/i
const LABEL = {
  connecting: 'Connecting', ringing: 'Kairo is calling', listening: 'Listening', thinking: 'Thinking',
  speaking: 'Speaking', paused: 'Paused', muted: 'Muted', ended: 'Call ended', missed: 'Missed call', error: 'Something went wrong',
}
const HINT = {
  listening: 'Just talk. Pause when you are done.', thinking: '', speaking: 'Talk or tap to interrupt',
  paused: 'Tap the orb to go on', muted: 'Kairo can’t hear you',
}

const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

export default function Call({ callId: initialId, onClose }) {
  const outgoing = initialId === 'new'   // "Call Kairo": no ringing, mic on at once
  const [call, setCall] = useState(null)
  const [phase, setPhase] = useState(outgoing ? 'connecting' : 'ringing')
  const [log, setLog] = useState([])
  const [err, setErr] = useState('')
  const [needTap, setNeedTap] = useState(false)   // iOS didn't play Kairo's voice: "Tap to play"
  const [engine, setEngine] = useState(null)      // 'realtime' (OpenAI), 'webrtc' (the PC's own voice over WebRTC) or 'local' (the old line)
  const rtStatus = useRef(null)
  const [secs, setSecs] = useState(0)
  const [captions, setCaptions] = useState(() => { try { return localStorage.getItem('call.captions') !== '0' } catch { return true } })
  const live = useRef({ on: false, ringing: false, voice: null, empty: 0, phase: 'connecting', started: 0, muted: false, reply: '', at: -1, endAfter: false })
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
  useEffect(() => { realtimeStatus().then((s) => { rtStatus.current = s }) }, [])   // the PC also reads the call context now
  useEffect(() => {
    if (!['listening', 'thinking', 'speaking', 'paused', 'muted'].includes(phase)) return undefined
    const t = setInterval(() => setSecs(Math.round((Date.now() - live.current.started) / 1000)), 1000)
    return () => clearInterval(t)
  }, [phase])

  const getLevel = useCallback(() => {
    const l = live.current
    if (l.phase === 'thinking') return 0.12 + Math.sin(performance.now() / 260) * 0.06
    if (l.phase === 'listening' || l.phase === 'speaking') return l.voice?.level() || 0
    return 0
  }, [])

  // Kairo's answer in the captions, growing while it is written.
  const caption = (text) => {
    const l = live.current
    setLog((x) => {
      if (l.at >= 0 && l.at < x.length) { const y = x.slice(); y[l.at] = { who: 'kairo', text }; return y }
      l.at = x.length
      return [...x, { who: 'kairo', text }]
    })
  }

  const start = async (greet) => {
    const l = live.current
    l.on = true
    l.started = Date.now()
    const voice = startLine({
      path: 'call', callId: idRef.current, status: rtStatus.current, on: {
        engine: (name, why) => {
          setEngine(name)
          if (name === 'local' && why?.message && !['off', 'no_key'].includes(why.reason)) setErr(why.message)
        },
        budget: (pct) => setErr(`Realtime voice: ${pct}% of this month’s budget used.`),
        state: (s) => {
          if (!l.on || s === 'idle') return
          if (s === 'speaking') setNeedTap(false)
          set(s === 'paused' && l.muted ? 'muted' : s)
        },
        transcript: (text) => {
          l.empty = 0
          l.reply = ''
          l.at = -1
          setErr('')
          setLog((x) => [...x, { who: 'me', text }])
          if (BYE.test(text)) l.endAfter = true          // Kairo says goodbye, then the call ends
        },
        noSpeech: () => {
          l.empty += 1
          if (l.empty >= 3) { l.endAfter = true; voice.say('I can’t hear you. Let’s talk later.') }
        },
        delta: (text) => { l.reply += text; caption(l.reply.replace(HANG_UP_ANY, ' ').trim()) },
        done: (f) => {
          caption((f.text || '').replace(HANG_UP_ANY, ' ').trim())
          if (!f.interrupted && (f.hangUp || HANG_UP.test(f.text || ''))) l.endAfter = true   // a getting-to-know-you call wraps up itself
        },
        answered: () => { if (l.endAfter && l.on) hangup(true) },
        error: (f) => { if (f.message) setErr(f.message) },
        needTap: () => setNeedTap(true),
      },
    })
    l.voice = voice
    if (greet) await api(`/api/voice/calls/${idRef.current}/answer`, { method: 'POST', body: {} }).catch(() => {})
    await voice.ready
    if (greet && l.on) voice.say(call?.opening || 'Hi, it’s Kairo.')
    else if (l.on) voice.greet()                     // a call you placed: "Hey <name>." at once
  }

  const answer = () => {
    unlockAudio()   // must happen inside the tap
    start(true)
  }

  // "No sound? Tap to play": inside the tap, through the speaker; this phone pauses the mic while
  // Kairo talks from now on (tap the orb to interrupt then).
  const tapToPlay = () => {
    setNeedTap(false)
    if (!releaseWhileSpeaking()) setReleaseWhileSpeaking(true)
    if (live.current.voice && !live.current.voice.tapToPlay()) setNeedTap(true)
  }

  // Uses only refs and props: callbacks keep an old copy of this function.
  const hangup = (goBack = true) => {
    const l = live.current
    const wasLive = l.on || l.ringing
    l.on = false
    l.ringing = false
    l.voice?.close()                     // still talking: what you said is still sent and answered in chat
    l.voice = null
    if (wasLive && idRef.current) api(`/api/voice/calls/${idRef.current}/end`, { method: 'POST', body: {} }).catch(() => {})
    if (goBack) { set('ended'); setTimeout(onClose, 700) }
  }

  const tapOrb = () => {
    unlockAudio()
    const l = live.current
    if (!l.on || !l.voice) return
    if (l.phase === 'paused') { l.voice.resume(); return }
    if (['listening', 'speaking', 'thinking'].includes(l.phase)) l.voice.pause()
  }

  const toggleMute = () => {
    const l = live.current
    if (!l.voice) return
    if (l.phase === 'muted') { l.muted = false; l.voice.resume(); return }
    l.muted = true
    l.voice.pause()
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
        {engine && !over && <p className="k-call-sub" style={{ opacity: 0.55 }}>{engine === 'realtime' ? 'Realtime voice' : engine === 'webrtc' ? 'Local voice · WebRTC' : 'Local voice'}</p>}
        {err && phase !== 'error' && <p className="k-call-sub" style={{ color: 'var(--warn)' }}>{err}</p>}
        {(needTap || phase === 'speaking') && (
          <button type="button" className={needTap ? 'k-btn primary sm' : 'k-btn ghost sm'} style={{ marginTop: 10 }} onClick={tapToPlay}>
            {needTap ? 'Tap to play Kairo' : 'No sound? Tap to play'}
          </button>
        )}
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
