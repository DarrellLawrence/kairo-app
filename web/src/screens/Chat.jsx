import { AnimatePresence } from 'motion/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Button, ChatBubble, EmptyState, IconChat, IconPlus, InputBar, List, ListRow, NavBar, ScrollArea, Sheet, useStickToBottom,
} from '../ui/index.js'

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
  const wsRef = useRef(null)
  const threadRef = useRef(null)
  const scrollRef = useRef(null)

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
  useEffect(() => { if (threadId === null && threads.length) setThreadId(threads[0].id) }, [threads, threadId])
  useEffect(() => { loadMessages(threadId) }, [threadId, loadMessages])
  useStickToBottom(scrollRef, [messages, chunks])

  const busy = chunks !== null
  const send = () => {
    const text = draft.trim()
    if (!text || !connected || busy) return
    setError(null)
    setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
    setChunks([])
    wsRef.current.send(JSON.stringify({ type: 'message', text, thread_id: threadId }))
    setDraft('')
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
  return (
    <div className="k-layer k-chat">
      <NavBar
        title={
          <button className="k-back" style={{ color: 'var(--text)', fontWeight: 600 }} onClick={() => setPicker(true)}>
            <span className={connected ? 'k-dot on' : 'k-dot'} style={{ marginRight: 8 }} />
            {current?.title ?? 'Today'} <span className="k-muted" style={{ marginLeft: 4 }}>▾</span>
          </button>
        }
        right={<Button variant="ghost" size="sm" icon={<IconPlus />} onClick={newThread} aria-label="New chat" />}
      />
      <ScrollArea tabbar={false} innerRef={scrollRef} onRefresh={async () => { await loadThreads(); await loadMessages(threadId) }}>
        {error && <div className="k-banner" style={{ marginTop: 8 }}>{error}</div>}
        {messages.length === 0 && !busy && (
          <EmptyState icon={<IconChat />} title="Ask Kairo anything" text="Mail, calendar, tasks and notes. Anything that sends or deletes waits for your approval." />
        )}
        <div className="k-messages">
          <AnimatePresence initial={false}>
            {messages.map((m) => <ChatBubble key={m.id} role={m.role} text={m.text} />)}
            {busy && <ChatBubble key="pending" role="kairo" chunks={chunks.length ? chunks : null} typing={chunks.length === 0} />}
          </AnimatePresence>
        </div>
      </ScrollArea>
      <InputBar value={draft} onChange={setDraft} onSend={send} disabled={!connected || busy} placeholder="Message Kairo" />

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
