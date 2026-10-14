// Chat pieces (ChatBubble, TypingDots, InputBar), ApprovalCard and SuccessCheck.
import { motion } from 'motion/react'
import { memo, useEffect, useLayoutEffect, useRef } from 'react'
import { spring } from '../design/motion.js'
import { Badge, Button, Swipeable } from './basics.jsx'
import { IconMic, IconPlus, IconSend } from './icons.jsx'

export function TypingDots() {
  return <span className="k-typing" role="status" aria-label="Kairo is typing"><i /><i /><i /></span>
}

/* role: owner | kairo | system. While streaming, pass `chunks` (the pieces as
   they arrive): each new piece fades in instead of popping. */
// memo: while a reply streams in, only the streaming bubble re-renders, not the whole history.
export const ChatBubble = memo(function ChatBubble({ role, text, chunks, typing }) {
  return (
    <motion.div
      className={`k-bubble ${role} selectable`}
      initial={role === 'owner' ? { opacity: 0, scale: 0.9, y: 10 } : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={role === 'owner' ? spring.bouncy : spring.smooth}
    >
      {typing ? <TypingDots /> : chunks ? chunks.map((c, i) => <span key={i} className="k-chunk">{c}</span>) : text}
    </motion.div>
  )
})

/* Grows with the text (up to ~5 lines). Enter sends, Shift+Enter is a new line. */
export function InputBar({ value, onChange, onSend, disabled, placeholder = 'Message', mic, onPlus }) {
  const ref = useRef(null)
  useLayoutEffect(() => {
    const ta = ref.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 140)}px`
  }, [value])
  const canSend = !disabled && value.trim().length > 0
  const submit = (e) => { e.preventDefault(); if (canSend) onSend() }
  return (
    <form className="k-inputbar k-glow" onSubmit={submit}>
      {onPlus && (
        <motion.button type="button" className="k-plus" onClick={onPlus} whileTap={{ scale: 0.88 }} transition={spring.snappy} aria-label="More">
          <IconPlus />
        </motion.button>
      )}
      <textarea
        ref={ref}
        rows={1}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e) }}
        enterKeyHint="send"
        aria-label={placeholder}
      />
      {mic && (!value.trim() || mic.state !== 'idle') ? (
        <motion.button type="button" className={`k-send k-mic ${mic.state}`} disabled={mic.state === 'busy'}
          onClick={mic.onPress} whileTap={{ scale: 0.85 }} transition={spring.snappy}
          aria-label={mic.state === 'recording' ? 'Stop and send' : 'Talk'}>
          {mic.state === 'recording' ? <span className="k-mic-stop" /> : mic.state === 'busy' ? <span className="k-spin-dot" /> : <IconMic />}
        </motion.button>
      ) : (
        <motion.button type="submit" className="k-send" disabled={!canSend} whileTap={canSend ? { scale: 0.85 } : undefined}
          animate={{ scale: canSend ? 1 : 0.92 }} transition={spring.snappy} aria-label="Send">
          <IconSend />
        </motion.button>
      )}
    </form>
  )
}

/* Animated tick: circle draws, then the check, with a little overshoot. */
export function SuccessCheck({ size = 72 }) {
  return (
    <motion.svg className="k-check" viewBox="0 0 72 72" width={size} height={size}
      initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={spring.bouncy} aria-label="Done">
      <motion.circle cx="36" cy="36" r="32" fill="var(--ok-soft)" stroke="var(--ok)" strokeWidth="3"
        initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }} />
      <motion.path d="M22 37l9 9 19-20" fill="none" stroke="var(--ok)" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round"
        initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ delay: 0.3, duration: 0.35, ease: [0.22, 1, 0.36, 1] }} />
    </motion.svg>
  )
}

const TIER_LABEL = { 3: 'Sends', 4: 'Deletes' }

function who(card) {
  const a = card.args || {}
  const to = [...(a.to || []), ...(a.cc || [])]
  if (to.length) return `To ${to.join(', ')}`
  if (a.response) return `Reply to the organiser: ${a.response}`
  return card.tool?.replace('.', ' · ') || ''
}

/* An approval waiting for the owner: who, what, the full text, and two big
   buttons. Swipe right = approve, left = reject (spring + resistance). Tier 4
   never acts on a swipe alone: the caller asks for a second confirmation. */
export function ApprovalCard({ card, onApprove, onReject, onOpen, busy, hint }) {
  const tier4 = card.tier >= 4
  const a = card.args || {}
  return (
    <motion.div
      className="k-approval"
      layout
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.2 } }}
      transition={spring.smooth}
    >
      <Swipeable
        right={{ label: tier4 ? 'Confirm…' : 'Approve', onAction: onApprove }}
        left={{ label: 'Reject', onAction: onReject }}
      >
        <div className={tier4 ? 'k-approval-face tier4' : 'k-approval-face'} onClick={onOpen}>
          <div className="k-approval-head">
            <Badge tone={tier4 ? 'danger' : 'accent'} pop={false}>{TIER_LABEL[card.tier] ?? `Tier ${card.tier}`}</Badge>
            <span className="k-approval-who">{who(card)}</span>
            <span className="k-approval-num">#{card.id}</span>
          </div>
          <p className="k-approval-summary">{a.subject || card.summary}</p>
          {a.body && <p className="k-approval-preview selectable" onClick={(e) => e.stopPropagation()}>{a.body}</p>}
          {!a.body && a.subject && <p className="k-approval-reason">{card.summary}</p>}
          {card.reason && <p className="k-approval-reason">Why: {card.reason}</p>}
          <div className="k-approval-actions" onClick={(e) => e.stopPropagation()}>
            <Button variant="secondary" disabled={busy} onClick={onReject}>Reject</Button>
            <Button variant={tier4 ? 'danger' : 'primary'} breathe={!tier4} disabled={busy} onClick={onApprove}>{tier4 ? 'Delete…' : 'Approve'}</Button>
          </div>
        </div>
      </Swipeable>
      {hint && <div className="k-approval-hint">Swipe right to approve · left to reject · tap for details</div>}
    </motion.div>
  )
}

/* Keeps a scroll container pinned to the bottom while new content arrives,
   unless the owner scrolled up to read. */
export function useStickToBottom(ref, deps) {
  const pinned = useRef(true)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onScroll = () => { pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [ref])
  useLayoutEffect(() => {
    const el = ref.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
