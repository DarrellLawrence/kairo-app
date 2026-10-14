import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Button, ChatBubble, GlowChip, IconInbox, IconSpeaker, InputBar, List, ListRow, NavBar, ScrollArea, Sheet, useStickToBottom,
  useToast,
} from '../ui/index.js'
import { speak, startRecording, stopSpeaking, transcribe, unlockAudio } from '../voice.js'

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
  const fileRef = useRef(null)
  const prefill = useRef((() => { try { const p = sessionStorage.getItem('kairo-chat-prefill'); sessionStorage.removeItem('kairo-chat-prefill'); return p } catch { return null } })())
  const wsRef = useRef(null)
  const scrollRef = useRef(null)
  const toast = useToast()
  const [mic, setMic] = useState('idle')   // idle | recording | busy
  const recRef = useRef(null)
  const [speakOn, setSpeakOn] = useState(() => { try { return localStorage.getItem('chat.speak') === '1' } catch { return false } })
  const speakRef = useRef(speakOn)
  speakRef.current = speakOn

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
          setChunks((c) => [...(c ?? []), msg.text])
        } else if (msg.type === 'done') {
          setChunks(null)
          setMessages((m) => [...m, { id: msg.message_id, role: 'kairo', text: msg.text }])
          if (speakRef.current) speak(msg.text).catch(() => {})
        } else if (msg.type === 'error') {
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
  const send = (spoken) => {
    const text = (typeof spoken === 'string' ? spoken : draft).trim()
    if (!text || !connected || busy) return
    setError(null)
    setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
    setChunks([])
    wsRef.current.send(JSON.stringify({ type: 'message', text }))
    if (typeof spoken !== 'string') setDraft('')
  }

  // A chip tapped on Today: ask it as soon as the line is open.
  useEffect(() => {
    if (connected && prefill.current) {
      const p = prefill.current
      prefill.current = null
      send(p)
    }
  })   // eslint-disable-line react-hooks/exhaustive-deps

  const sendFile = async (file) => {
    if (!file) return
    setPlus(false)
    try {
      const res = await fetch('/api/captures', { method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream' }, body: file })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
      toast.show({ text: 'Sent. Kairo reads it within 15 minutes.' })
    } catch (e) { toast.show({ text: e.message }) }
  }

  const pressMic = async () => {
    if (mic === 'idle') {
      unlockAudio()
      stopSpeaking()
      try {
        recRef.current = await startRecording()
        setMic('recording')
      } catch { toast.show({ text: 'Microphone not allowed. iPhone Settings → Safari → Microphone.' }) }
      return
    }
    if (mic === 'recording') {
      setMic('busy')
      try {
        const blob = await recRef.current.stop()
        const { text } = await transcribe(blob)
        if (text) send(text); else toast.show({ text: 'Did not catch that.' })
      } catch (e) { toast.show({ text: e.message, duration: 5000 }) } finally { setMic('idle') }
    }
  }

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
      <input ref={fileRef} type="file" accept="image/*,application/pdf" hidden onChange={(e) => sendFile(e.target.files?.[0])} />

      <Sheet open={plus} onClose={() => setPlus(false)} title="Kairo">
        <List>
          <ListRow icon={<IconInbox />} title="Photo or PDF" subtitle="A letter, a bill, a worksheet" onClick={() => fileRef.current?.click()} />
          <ListRow icon={<IconSpeaker />} title={speakOn ? 'Stop reading replies aloud' : 'Read replies aloud'} onClick={() => { setPlus(false); toggleSpeak() }} />
        </List>
      </Sheet>

    </div>
  )
}
