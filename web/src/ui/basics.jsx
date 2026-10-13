// Button, Card, Badge, EmptyState, Skeleton, ListRow (+ Swipeable).
import { animate, motion, useMotionValue, useTransform } from 'motion/react'
import { useRef } from 'react'
import { spring } from '../design/motion.js'
import { IconChevron } from './icons.jsx'

export function Button({ variant = 'secondary', size, block, icon, loading, children, className = '', ...rest }) {
  const cls = ['k-btn', variant, size, block && 'block', className].filter(Boolean).join(' ')
  return (
    <motion.button
      className={cls}
      whileTap={rest.disabled ? undefined : { scale: 0.96 }}
      transition={spring.snappy}
      {...rest}
    >
      {loading ? <span className="k-typing" aria-label="Working"><i /><i /><i /></span> : <>{icon}{children}</>}
    </motion.button>
  )
}

export function Card({ glass, accent, flush, interactive, className = '', children, ...rest }) {
  const cls = ['k-card', glass && 'glass', accent && 'accent', flush && 'flush', className].filter(Boolean).join(' ')
  return (
    <motion.div
      className={cls}
      whileTap={interactive ? { scale: 0.98 } : undefined}
      transition={spring.smooth}
      {...rest}
    >
      {children}
    </motion.div>
  )
}

export function Badge({ tone = 'neutral', children, pop = true }) {
  return (
    <motion.span
      key={pop ? String(children) : undefined}
      className={`k-badge ${tone}`}
      initial={pop ? { scale: 0.4, opacity: 0 } : false}
      animate={{ scale: 1, opacity: 1 }}
      transition={spring.bouncy}
    >
      {children}
    </motion.span>
  )
}

export function EmptyState({ icon, title, text, action }) {
  return (
    <motion.div className="k-empty" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={spring.smooth}>
      {icon && <div className="k-empty-icon">{icon}</div>}
      {title && <p className="k-empty-title">{title}</p>}
      {text && <p className="k-empty-text">{text}</p>}
      {action}
    </motion.div>
  )
}

export function Skeleton({ w = '100%', h = 14, r, style }) {
  return <div className="k-skel" style={{ width: w, height: h, borderRadius: r, ...style }} aria-hidden="true" />
}

export function SkeletonCard({ lines = 3 }) {
  return (
    <div className="k-card" aria-busy="true">
      <Skeleton w="40%" h={12} style={{ marginBottom: 12 }} />
      {Array.from({ length: lines }, (_, i) => <Skeleton key={i} w={i === lines - 1 ? '65%' : '100%'} h={14} style={{ marginBottom: 8 }} />)}
    </div>
  )
}

/* Horizontal swipe with resistance and snap-back. Past the threshold, release
   triggers the side's action. right = swipe to the right (reveals the left edge). */
export function Swipeable({ left, right, threshold = 0.45, children }) {
  const ref = useRef(null)
  const x = useMotionValue(0)
  const rightOpacity = useTransform(x, [0, 60], [0, 1])
  const leftOpacity = useTransform(x, [-60, 0], [1, 0])
  const onDragEnd = (_, info) => {
    const width = ref.current?.offsetWidth || 320
    const past = Math.abs(info.offset.x) > width * threshold || Math.abs(info.velocity.x) > 900
    const action = info.offset.x > 0 ? right : left
    if (past && action) {
      animate(x, Math.sign(info.offset.x) * width * 1.1, { ...spring.smooth, onComplete: () => { action.onAction(); animate(x, 0, { duration: 0 }) } })
    } else {
      animate(x, 0, spring.bouncy)
    }
  }
  const dragged = useRef(false)
  return (
    <div className="k-swipe" ref={ref} onClickCapture={(e) => { if (dragged.current) { e.stopPropagation(); e.preventDefault() } }}>
      {right && <motion.div className="k-swipe-bg right" style={{ opacity: rightOpacity }}>{right.label}</motion.div>}
      {left && <motion.div className="k-swipe-bg left" style={{ opacity: leftOpacity }}>{left.label}</motion.div>}
      <motion.div
        style={{ x, touchAction: 'pan-y' }}
        drag="x"
        dragDirectionLock
        dragConstraints={{ left: left ? -10000 : 0, right: right ? 10000 : 0 }}
        dragElastic={0.18}
        dragMomentum={false}
        onDragStart={() => { dragged.current = true }}
        onDragEnd={(e, info) => { onDragEnd(e, info); setTimeout(() => { dragged.current = false }, 50) }}
      >
        {children}
      </motion.div>
    </div>
  )
}

export function ListRow({ icon, title, subtitle, value, chevron, onClick, trailing, wrap, swipe, ...rest }) {
  const row = (
    <motion.button
      type="button"
      className="k-row"
      onClick={onClick}
      whileTap={onClick ? { backgroundColor: 'var(--surface-pressed)' } : undefined}
      transition={{ duration: 0.12 }}
      disabled={!onClick && !swipe}
      {...rest}
    >
      {icon && <span className="k-row-icon">{icon}</span>}
      <span className="k-row-main">
        <span className="k-row-title" style={{ display: 'block' }}>{title}</span>
        {subtitle && <span className={wrap ? 'k-row-sub wrap' : 'k-row-sub'} style={{ display: 'block' }}>{subtitle}</span>}
      </span>
      {value !== undefined && <span className="k-row-value">{value}</span>}
      {trailing}
      {chevron && <IconChevron className="k-chevron" />}
    </motion.button>
  )
  return swipe ? <Swipeable {...swipe}>{row}</Swipeable> : row
}

export function List({ children }) {
  return <div className="k-list">{children}</div>
}

export function SectionTitle({ children }) {
  return <h2 className="k-section-title">{children}</h2>
}
