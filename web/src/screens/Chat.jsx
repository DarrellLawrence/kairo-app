import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { spring } from '../design/motion.js'
import {
  Button, ChatBubble, GlowChip, IconInbox, IconPlus, IconSpeaker, InputBar, List, ListRow, NavBar, ScrollArea, Sheet, useStickToBottom,
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

export default function Chat() {
  const [threads, setThreads] = useState([])
  const [threadId, setThreadId] = useState(null)
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [chunks, setChunks] = useState(null)   // streaming reply, piece by piece; null = idle
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const [picker, setPicker] = useState(false)
  const [plus, setPlus] = useState(false)
  const [home, setHome] = useState(null)
  const fileRef = useRef(null)
  const prefill = useRef((() => { try { const p = sessionStorage.getItem('kairo-chat-prefill'); sessionStorage.removeItem('kairo-chat-prefill'); return p } catch { return null } })())
  const wsRef = useRef(null)
  const threadRef = useRef(null)
  const scrollRef = useRef(null)
  const toast = useToast()
  const [mic, setMic] = useState('idle')   // idle | recording | busy
  const recRef = useRef(null)
  const [speakOn, setSpeakOn] = useState(() => { try { return localStorage.getItem('chat.speak') === '1' } catch { return false } })
  const speakRef = useRef(speakOn)
  speakRef.current = speakOn

  threadRef.current = threadId

  const loadThreads = useCallback(async () => {
    const res = await fetch('/api/threads')
    if (res.ok) setThreads(await res.json())
  }, [])

  const loadMessages = useCallback(async (id) => {
    if (!id) return setMessages([])
    const res = await fetch(`/api/threads/${id}/messages`)
    if (res.ok) setMessages(await res.json())
  }, [])

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
          if (msg.id !== threadRef.current) { setThreadId(msg.id); loadThreads() }
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
  }, [loadThreads])

  useEffect(() => { loadThreads() }, [loadThreads])
  useEffect(() => { api('/api/today').then(setHome).catch(() => {}) }, [])
  useEffect(() => { if (threadId === null && threads.length) setThreadId(threads[0].id) }, [threads, threadId])
  useEffect(() => { loadMessages(threadId) }, [threadId, loadMessages])
  useStickToBottom(scrollRef, [messages, chunks])

  const busy = chunks !== null
  const send = (spoken) => {
    const text = (typeof spoken === 'string' ? spoken : draft).trim()
    if (!text || !connected || busy) return
    setError(null)
    setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
    setChunks([])
    wsRef.current.send(JSON.stringify({ type: 'message', text, thread_id: threadId }))
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

  const newThread = async () => {
    const res = await fetch('/api/threads', { method: 'POST' })
    if (!res.ok) return
    const t = await res.json()
    await loadThreads()
    setThreadId(t.id)
    setPicker(false)
  }

  const current = threads.find((t) => t.id === threadId)
  const empty = messages.length === 0 && !busy
  return (
    <div className="k-layer k-chat">
      <NavBar
        bgOpacity={empty ? 0 : 1}
        title={
          <button className="k-back" style={{ color: 'var(--text)', fontWeight: 600 }} onClick={() => setPicker(true)}>
            <span className={connected ? 'k-dot on' : 'k-dot'} style={{ marginRight: 8 }} />
            {current?.title ?? 'Today'} <span className="k-muted" style={{ marginLeft: 4 }}>▾</span>
          </button>
        }
        right={<Button variant="ghost" size="sm" icon={<IconSpeaker style={{ opacity: speakOn ? 1 : 0.45 }} />} onClick={toggleSpeak} aria-label="Read replies aloud" />}
      />
      <ScrollArea tabbar={false} innerRef={scrollRef} onRefresh={async () => { await loadThreads(); await loadMessages(threadId) }}>
        {error && <div className="k-banner" style={{ marginTop: 8 }}>{error}</div>}
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
          <ListRow icon={<IconPlus />} title="New chat" onClick={() => { setPlus(false); newThread() }} />
          <ListRow icon={<IconInbox />} title="Photo or PDF" subtitle="A letter, a bill, a worksheet" onClick={() => fileRef.current?.click()} />
          <ListRow icon={<IconSpeaker />} title={speakOn ? 'Stop reading replies aloud' : 'Read replies aloud'} onClick={() => { setPlus(false); toggleSpeak() }} />
        </List>
      </Sheet>

      <Sheet open={picker} onClose={() => setPicker(false)} title="Chats">
        <Button variant="primary" block icon={<IconPlus />} onClick={newThread} style={{ marginBottom: 12 }}>New chat</Button>
        <List>
          {threads.map((t) => (
            <ListRow key={t.id} title={t.title ?? `Chat ${t.id}`} chevron
              value={t.id === threadId ? '✓' : undefined}
              onClick={() => { setThreadId(t.id); setPicker(false) }} />
          ))}
        </List>
      </Sheet>
    </div>
  )
}
