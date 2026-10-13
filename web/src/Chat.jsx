import { useCallback, useEffect, useRef, useState } from 'react'

const STATUS_TEXT = {
  usage_limited: 'Usage limit reached. Kairo pauses until it resets.',
  logged_out: 'The Claude CLI on kairo-home is logged out. Log in again (see README).',
  unavailable: 'The brain is unavailable right now.',
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
  const [pending, setPending] = useState(null) // streaming reply text
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const wsRef = useRef(null)
  const threadRef = useRef(null)
  const endRef = useRef(null)

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
        setPending(null)
        if (!closed) retry = setTimeout(connect, 2000)
      }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'thread') {
          if (msg.id !== threadRef.current) {
            setThreadId(msg.id)
            loadThreads()
          }
        } else if (msg.type === 'delta') {
          setPending((p) => (p ?? '') + msg.text)
        } else if (msg.type === 'done') {
          setPending(null)
          setMessages((m) => [...m, { id: msg.message_id, role: 'kairo', text: msg.text }])
        } else if (msg.type === 'error') {
          setPending(null)
          setError(STATUS_TEXT[msg.status] ?? msg.message)
          setMessages((m) => [...m, { id: `e${Date.now()}`, role: 'system', text: msg.message }])
        }
      }
    }
    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      wsRef.current?.close()
    }
  }, [loadThreads])

  useEffect(() => {
    loadThreads().then(() => {})
  }, [loadThreads])

  useEffect(() => {
    if (threadId === null && threads.length) setThreadId(threads[0].id)
  }, [threads, threadId])

  useEffect(() => {
    loadMessages(threadId)
  }, [threadId, loadMessages])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, pending])

  const send = (e) => {
    e.preventDefault()
    const text = draft.trim()
    if (!text || !connected || pending !== null) return
    setError(null)
    setMessages((m) => [...m, { id: `o${Date.now()}`, role: 'owner', text }])
    setPending('')
    wsRef.current.send(JSON.stringify({ type: 'message', text, thread_id: threadId }))
    setDraft('')
  }

  const newThread = async () => {
    const res = await fetch('/api/threads', { method: 'POST' })
    if (!res.ok) return
    const t = await res.json()
    await loadThreads()
    setThreadId(t.id)
  }

  return (
    <div className="chat">
      <div className="chat-bar">
        <select value={threadId ?? ''} onChange={(e) => setThreadId(Number(e.target.value))} aria-label="Thread">
          {threads.length === 0 && <option value="">Today</option>}
          {threads.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title ?? `Thread ${t.id}`}
            </option>
          ))}
        </select>
        <button type="button" onClick={newThread}>New</button>
        <span className={connected ? 'dot on' : 'dot'} title={connected ? 'Connected' : 'Reconnecting'} />
      </div>
      {error && <div className="banner">{error}</div>}
      <div className="messages">
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>{m.text}</div>
        ))}
        {pending !== null && <div className="msg kairo pending">{pending || '…'}</div>}
        <div ref={endRef} />
      </div>
      <form className="composer" onSubmit={send}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) send(e)
          }}
          placeholder="Message Kairo"
          rows={1}
        />
        <button type="submit" disabled={!connected || pending !== null || !draft.trim()}>Send</button>
      </form>
    </div>
  )
}
