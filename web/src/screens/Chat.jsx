import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Button, ChatBubble, GlowChip, IconSpeaker, InputBar, NavBar, ScrollArea, useStickToBottom,
  useToast,
} from '../ui/index.js'
import { CaptureSheet } from './CaptureSheet.jsx'
import { playLastAgain, speak, stopSpeaking, unlockAudio } from '../voice.js'
import { QUIET_REASONS, realtimeStatus, startLine } from '../realtime.js'
import { audioOutcome, startTurn } from '../turns.js'

const STATUS_TEXT = {
  usage_limited: 'Usage limit reached. Kairo pauses until it resets.',
  logged_out: 'Claude on the PC is logged out. Run Kairo Logins on the PC.',
  unavailable: 'Claude is not available right now.',
  updating: 'Kairo is installing an update. Try again in a minute.',
  paused: 'Kairo is paused (kill switch). Resume it under More.',
}

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws/chat`
}

// One conversation with Kairo, forever: chat, calls and voice notes. No threads, no "new chat";
// what's older lives in Kairo's conversation memory (server/app/memory.py).
export default function Chat() {
  const [messages, setMessages] = useState([])
  const [hasMore, setHasMore] = useState(false)
  const [draft, setDraft] = useState('')
  const [chunks, setChunks] = useState(null)   // streaming reply, piece by piece; null = idle
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const [plus, setPlus] = useState(false)
  const [home, setHome] = useState(null)
  const prefill = useRef((() => { try { const p = sessionStorage.getItem('kairo-chat-prefill'); sessionStorage.removeItem('kairo-chat-prefill'); return p } catch { return null } })())
  const wsRef = useRef(null)
  const scrollRef = useRef(null)
  const toast = useToast()
  const [mic, setMic] = useState('idle')   // idle | recording | busy
  const voiceRef = useRef(null)            // a spoken message on the voice line (realtime.js or voicelink.js)
  const rtStatus = useRef(null)            // realtime voice available? (asked when the chat opens)
  const [speakOn, setSpeakOn] = useState(() => { try { return localStorage.getItem('chat.speak') === '1' } catch { return false } })
  const speakRef = useRef(speakOn)
  speakRef.current = speakOn
  const turnRef = useRef(null)       // the turn being measured (turns.js -> More -> System -> Voice speed)

  const loadMessages = useCallback(async () => {
    const res = await fetch('/api/chat/messages?limit=60')
    if (!res.ok) return
    const r = await res.json()
    setMessages(r.messages)
    setHasMore(r.has_more)
  }, [])

  const loadEarlier = async () => {
    const first = messages.find((m) => typeof m.id === 'number')
    if (!first) return
    const res = await fetch(`/api/chat/messages?limit=60&before=${first.id}`)
    if (!res.ok) return
    const r = await res.json()
    setMessages((m) => [...r.messages, ...m])
    setHasMore(r.has_more)
  }

  useEffect(() => {
    let closed = false
    let retry
    const connect = () => {
      const ws = new WebSocket(wsUrl())
      wsRef.current = ws
      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        setChunks(null)
        if (!closed) retry = setTimeout(connect, 2000)
      }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'thread') {
          // one conversation: nothing to switch
        } else if (msg.type === 'delta') {
          turnRef.current?.mark('first_words')
          setChunks((c) => [...(c ?? []), msg.text])
        } else if (msg.type === 'done') {
          setChunks(null)
          setMessages((m) => [...m, { id: msg.message_id, role: 'kairo', text: msg.text }])
          const turn = turnRef.current
          turnRef.current = null
          turn?.model(msg.model)
          turn?.server(msg.server)
          if (speakRef.current) say(msg.text)
          turn?.finish('text')
        } else if (msg.type === 'error') {
          turnRef.current?.finish('failed', msg.status || msg.message)
          turnRef.current = null
          setChunks(null)
          setError(STATUS_TEXT[msg.status] ?? msg.message)
          setMessages((m) => [...m, { id: `e${Date.now()}`, role: 'system', text: msg.message }])
        }
      }
    }
    connect()
    return () => { closed = true; clearTimeout(retry); wsRef.current?.close() }
  }, [])

  useEffect(() => { api('/api/today').then(setHome).catch(() => {}) }, [])
  useEffect(() => { loadMessages() }, [loadMessages])
  useStickToBottom(scrollRef, [messages, chunks])

  const busy = chunks !== null
  // "Read replies aloud" for typed messages (a spoken message is answered on the voice line).
  const say = (text) => speak(text).catch((e) => {
    if (e.name === 'NeedTap') toast.show({ text: 'No sound?', duration: 8000, action: { label: 'Tap to play', onAction: () => playLastAgain().catch(() => {}) } })
    else toast.show({ text: audioOutcome(e)[1] || e.message })
  })
  const send = (chip) => {
    const text = (typeof chip === 'string' ? chip : draft).trim()
    if (!text || !connected || busy) return
    setError(null)
    setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
    setChunks([])
    turnRef.current = startTurn('chat_text')
    wsRef.current.send(JSON.stringify({ type: 'message', text }))
    turnRef.current.mark('sent')
    if (typeof chip !== 'string') setDraft('')
  }

  // A chip tapped on Today: ask it as soon as the line is open.
  useEffect(() => {
    if (connected && prefill.current) {
      const p = prefill.current
      prefill.current = null
      send(p)
    }
  })   // eslint-disable-line react-hooks/exhaustive-deps

  // Voice V1: tap the mic and talk; it stops by itself when you do (or tap again). The answer is
  // written here and spoken while it is written. Tap while Kairo talks to stop it.
  const endVoice = () => { voiceRef.current?.close(); voiceRef.current = null; setMic('idle') }
  const pressMic = () => {
    const v = voiceRef.current
    if (v) {
      if (v.state === 'speaking') { v.stop(); endVoice(); return }
      if (mic === 'recording' && !v.finish()) { endVoice(); toast.show({ text: 'Did not catch that.' }) }
      return
    }
    if (busy) return
    unlockAudio({ call: true })        // the voice button is a call line too: its audio mode comes before the mic
    stopSpeaking()
    setError(null)
    setMic('busy')                           // connecting; talk once the mic shows recording
    voiceRef.current = startLine({
      path: 'chat_voice', status: rtStatus.current, on: {
        engine: (name, why) => {
          if (name === 'local' && why?.message && !QUIET_REASONS.includes(why.reason)) toast.show({ text: why.message })
        },
        transcript: (text) => {
          setMic('busy')
          setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
          setChunks([])
        },
        state: (s) => { if (s === 'thinking') setMic('busy'); else if (s === 'speaking') setMic('speaking') },
        delta: (text) => setChunks((c) => [...(c ?? []), text]),
        done: (f) => {
          setChunks(null)
          if (f.text) setMessages((m) => [...m, { id: f.message_id ?? `k${Date.now()}`, role: 'kairo', text: f.text }])
        },
        noSpeech: () => { endVoice(); toast.show({ text: 'Did not catch that.' }) },
        answered: endVoice,
        error: (f) => {
          if (f.status === 'voice') { toast.show({ text: f.message }); return }
          setChunks(null)
          setError(STATUS_TEXT[f.status] ?? f.message)
          endVoice()
        },
        needTap: () => toast.show({
          text: 'No sound?', duration: 8000,
          action: { label: 'Tap to play', onAction: () => voiceRef.current?.tapToPlay() },
        }),
      },
    })
    const line = voiceRef.current
    line.ready.then(() => { if (voiceRef.current === line) setMic((m) => (m === 'busy' ? 'recording' : m)) })
  }
  useEffect(() => () => voiceRef.current?.close(), [])
  useEffect(() => { realtimeStatus().then((s) => { rtStatus.current = s }) }, [])

  const toggleSpeak = () => {
    const next = !speakOn
    setSpeakOn(next)
    try { localStorage.setItem('chat.speak', next ? '1' : '0') } catch { /* private mode */ }
    if (next) unlockAudio(); else stopSpeaking()
    toast.show({ text: next ? 'Kairo reads replies out loud.' : 'Replies are silent.' })
  }

  const empty = messages.length === 0 && !busy
  return (
    <div className="k-layer k-chat">
      <NavBar
        bgOpacity={empty ? 0 : 1}
        title={
          <span style={{ color: 'var(--text)', fontWeight: 600, display: 'inline-flex', alignItems: 'center' }}>
            <span className={connected ? 'k-dot on' : 'k-dot'} style={{ marginRight: 8 }} />Kairo
          </span>
        }
        right={<Button variant="ghost" size="sm" icon={<IconSpeaker style={{ opacity: speakOn ? 1 : 0.45 }} />} onClick={toggleSpeak} aria-label="Read replies aloud" />}
      />
      <ScrollArea tabbar={false} innerRef={scrollRef} onRefresh={hasMore ? loadEarlier : loadMessages}>
        {error && <div className="k-banner" style={{ marginTop: 8 }}>{error}</div>}
        {hasMore && (
          <Button variant="ghost" size="sm" block onClick={loadEarlier} style={{ margin: '8px 0' }}>Earlier messages</Button>
        )}
        {empty && (
          <>
            <motion.div className="k-chat-hello" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={spring.smooth}>
              <h1>Hello{home?.name ? ` ${home.name}` : ''}</h1>
              <p>Ask anything. Things only for you, Kairo just does. Anything for other people waits for your approval.</p>
            </motion.div>
            {home?.suggestions?.length > 0 && (
              <>
                <p className="k-section-title" style={{ marginTop: 8 }}>Try asking</p>
                <div className="k-chips">
                  {home.suggestions.map((c, i) => (
                    <motion.div key={c.text} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.smooth, delay: 0.06 * i }}>
                      <GlowChip glow={c.glow} onClick={() => send(c.prompt)} disabled={!connected}>{c.text}</GlowChip>
                    </motion.div>
                  ))}
                </div>
              </>
            )}
          </>
        )}
        <div className="k-messages">
          <AnimatePresence initial={false}>
            {messages.map((m) => <ChatBubble key={m.id} role={m.role} text={m.text} />)}
            {busy && <ChatBubble key="pending" role="kairo" chunks={chunks.length ? chunks : null} typing={chunks.length === 0} />}
          </AnimatePresence>
        </div>
      </ScrollArea>
      <InputBar value={draft} onChange={setDraft} onSend={send} disabled={!connected || busy} placeholder="Ask me anything…"
        onPlus={() => setPlus(true)}
        mic={{ state: mic === 'idle' && (!connected || busy) ? 'busy' : mic, onPress: pressMic }} />
      <CaptureSheet open={plus} onClose={() => setPlus(false)} />

    </div>
  )
}
