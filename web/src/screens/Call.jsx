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
import { takeSentences } from '../sentences.js'
import {
  holdMic, listenLevel, playLastAgain, releaseMic, releaseWhileSpeaking, speak, speakingLevel, speechQueue, startRecording, stopSpeaking,
  transcribe, unlockAudio,
} from '../voice.js'

const BYE = /^(bye|goodbye|that'?s all|thanks,? bye|tsch(ü|ue)ss|ciao|adieu|auf wiederh|das war'?s|danke,? das war'?s)\b/i
const LABEL = {
  connecting: 'Connecting', ringing: 'Kairo is calling', listening: 'Listening', thinking: 'Thinking',
  speaking: 'Speaking', paused: 'Paused', muted: 'Muted', ended: 'Call ended', missed: 'Missed call', error: 'Something went wrong',
}
const HINT = {
  listening: 'Just talk. Pause when you are done.', thinking: '', speaking: 'Talk or tap to interrupt',
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
  const [needTap, setNeedTap] = useState(false)   // iOS didn't play Kairo's voice: "Tap to play"
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
    if (!releaseWhileSpeaking()) watchForBargeIn(abort)   // mic paused while Kairo talks: tap the orb instead
    try {
      await speak(text, { signal: abort.signal, call: true })
    } catch (e) {
      if (e.name === 'NeedTap') {
        stopMonitor()
        setNeedTap(true)
        await new Promise((resolve) => { live.current.afterTap = resolve })   // tapToPlay() plays it, then the call goes on
      } else if (e.name !== 'AbortError') setErr(e.message)
    }
    while (live.current.replay) await live.current.replay   // tapped "No sound?": let the replay finish first
    stopMonitor()
  }

  // "No sound? Tap to play": inside the tap, through the speaker (voice.js remembers it for calls).
  const tapToPlay = () => {
    setNeedTap(false)
    const l = live.current
    l.monitor?.cancel()
    const abort = new AbortController()
    const answer = l.abort   // pausing or hanging up still stops the rest of the answer too
    l.abort = { abort: () => { abort.abort(); answer?.abort() } }
    const goOn = () => { const go = l.afterTap; l.afterTap = null; go?.() }
    l.replay = playLastAgain({ signal: abort.signal })
      .then(goOn, (e) => { if (e.name === 'NeedTap') setNeedTap(true); else goOn() })
      .finally(() => { l.replay = null })
  }

  // One turn at a time on the call's line: a turn that is still being written (you talked over
  // Kairo) finishes before the next is sent, so each answer's words reach its own listener.
  const ask = (text, onDelta) => {
    const l = live.current
    const mine = (l.turn || Promise.resolve()).then(() => new Promise((resolve) => {
      const ws = l.ws
      const onMsg = (ev) => {
        const m = JSON.parse(ev.data)
        if (m.type === 'delta') onDelta?.(m.text)
        else if (m.type === 'done' || m.type === 'error') {
          ws.removeEventListener('message', onMsg)
          resolve(m.type === 'done' ? { text: m.text } : { text: m.message || 'Sorry, something went wrong.', error: true })
        }
      }
      ws.addEventListener('message', onMsg)
      ws.send(JSON.stringify({ type: 'message', text: `(spoken, on a call) ${text}` }))
    }))
    l.turn = mine.catch(() => {})
    return mine
  }

  // Fix plan 29: Kairo starts talking after its first sentence while the rest is still being written.
  // Fix plan 31: you stop talking -> Kairo's voice starts, for the p50/p95 in More -> System.
  const timeTurn = (since) => {
    const ms = Math.round(performance.now() - since)
    fetch('/api/latency', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'call_turn', ms }) })
      .catch(() => {})
  }

  const reply = async (heard, speechEndedAt) => {
    const l = live.current
    const abort = new AbortController()
    l.abort = abort
    l.interrupted = false
    let spoken = ''
    let buf = ''
    let at = -1
    const caption = (text) => setLog((x) => {
      if (at >= 0 && at < x.length) { const y = x.slice(); y[at] = { who: 'kairo', text }; return y }
      at = x.length
      return [...x, { who: 'kairo', text }]
    })
    const q = speechQueue({
      signal: abort.signal, call: true,
      onNeedTap: () => { stopMonitor(); setNeedTap(true); return new Promise((resolve) => { l.afterTap = resolve }) },
      onStart: () => { if (speechEndedAt) timeTurn(speechEndedAt) },
    })
    const out = (sentence) => {
      if (!sentence || abort.signal.aborted || !l.on || l.phase === 'paused') return
      if (!spoken) {
        set('speaking')
        if (!releaseWhileSpeaking()) watchForBargeIn(abort)   // mic paused while Kairo talks: tap the orb instead
      }
      spoken = spoken ? `${spoken} ${sentence}` : sentence
      caption(spoken)
      q.push(sentence)
    }
    const writing = ask(heard, (delta) => {
      buf += delta
      const r = takeSentences(buf, { first: !spoken })
      buf = r.rest
      r.sentences.forEach(out)
    })
    // Talked over, paused or hung up: stop now, not when the answer is finished.
    const stopped = new Promise((resolve) => abort.signal.addEventListener('abort', () => resolve(null), { once: true }))
    const answer = await Promise.race([writing, stopped])
    if (answer) {
      if (answer.error) out(answer.text)
      else if (!spoken) out(answer.text.trim() || buf.trim())   // nothing came in pieces: the whole answer
      else out(buf.trim())
      if (!spoken && l.on) caption(answer.text)   // paused meanwhile: the answer waits in the captions
    } else {
      writing.then((a) => { if (!spoken && l.on && l.phase === 'paused') caption(a.text) })
    }
    q.end()
    try {
      await q.done
    } catch (e) {
      if (e.name !== 'AbortError') setErr(e.message)
    }
    while (l.replay) await l.replay   // tapped "No sound?": let the replay finish first
    stopMonitor()
  }

  const listen = useCallback(async () => {
    const l = live.current
    if (!l.on || l.phase === 'paused' || l.phase === 'muted') return
    set('listening')
    try {
      l.rec = await startRecording({
        autoStop: true,
        onAutoStop: async (blob, { speechEndedAt } = {}) => {
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
          await reply(heard, speechEndedAt)
          if (['speaking', 'thinking'].includes(live.current.phase) || live.current.interrupted) listen()
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
    l.afterTap?.()
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
