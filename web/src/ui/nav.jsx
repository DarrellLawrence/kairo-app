// Navigation pieces: TabBar, Segmented, ScrollArea (rubber band + pull to
// refresh), Page (large title + nav bar), Stack (iOS push / pop) and
// Expandable (a card that grows into its detail view, shared element).
import { AnimatePresence, animate, motion, useDragControls, useMotionValue, useTransform } from 'motion/react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { pageVariants, rubber, spring } from '../design/motion.js'
import { IconArrowDown, IconBack, IconRefresh } from './icons.jsx'

// ---------------------------------------------------------------- tab bar

export function TabBar({ tabs, active, onSelect }) {
  return (
    <nav className="k-tabbar" aria-label="Tabs">
      {tabs.map((t) => (
        <motion.button
          key={t.id}
          type="button"
          className={t.id === active ? 'k-tab active' : 'k-tab'}
          onClick={() => onSelect(t.id)}
          whileTap={{ scale: 0.9 }}
          transition={spring.snappy}
          aria-current={t.id === active ? 'page' : undefined}
        >
          {t.id === active && <motion.span layoutId="k-tab-pill" className="k-tab-pill" transition={spring.smooth} />}
          {t.icon}
          {t.badge}
          <span className="k-tab-label">{t.label}</span>
        </motion.button>
      ))}
    </nav>
  )
}

export function Segmented({ options, value, onChange, id = 'seg' }) {
  return (
    <div className="k-segmented" role="tablist">
      {options.map((o) => (
        <button key={o.value} type="button" role="tab" aria-selected={o.value === value}
          className={o.value === value ? 'k-seg active' : 'k-seg'} onClick={() => onChange(o.value)}>
          {o.value === value && <motion.span layoutId={`k-seg-${id}`} className="k-seg-thumb" transition={spring.snappy} />}
          {o.label}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- scroll area

const PULL_TRIGGER = 72
const PULL_HOLD = 52

/* Lists stretch like iOS at both ends (only inside the list, never the page).
   With onRefresh, pulling down past the trigger refreshes, with a stretchy
   indicator that follows the finger and springs back. */
export function ScrollArea({ onRefresh, onScrollY, tabbar = true, children, innerRef }) {
  const el = useRef(null)
  const pull = useMotionValue(0)
  const [refreshing, setRefreshing] = useState(false)
  const touch = useRef(null)

  const ptrOpacity = useTransform(pull, [8, 40], [0, 1])
  const ptrScale = useTransform(pull, [0, PULL_TRIGGER, PULL_TRIGGER * 2.2], [0.6, 1, 1])
  const ptrStretch = useTransform(pull, [0, PULL_TRIGGER, PULL_TRIGGER * 2.2], [1, 1, 1.5])
  const ptrY = useTransform(pull, (v) => Math.max(0, v - 34))
  const arrowRot = useTransform(pull, [PULL_TRIGGER - 10, PULL_TRIGGER + 6], [0, 180])

  useEffect(() => {
    const node = el.current
    if (!node) return
    const start = (e) => {
      if (refreshing) return
      const t = e.touches[0]
      touch.current = {
        y: t.clientY, x: t.clientX, top: node.scrollTop <= 0,
        bottom: node.scrollTop + node.clientHeight >= node.scrollHeight - 1, active: false,
      }
    }
    const move = (e) => {
      const s = touch.current
      if (!s) return
      const t = e.touches[0]
      const dy = t.clientY - s.y
      if (!s.active && Math.abs(t.clientX - s.x) > Math.abs(dy)) { touch.current = null; return }   // horizontal: a swipe row
      if ((s.top && dy > 0 && node.scrollTop <= 0) || (s.bottom && dy < 0)) {
        s.active = true
        e.preventDefault()
        pull.set(rubber(dy, node.clientHeight))
      } else if (s.active) {
        pull.set(0)
      }
    }
    const end = async () => {
      const s = touch.current
      touch.current = null
      if (!s?.active) return
      const v = pull.get()
      if (onRefresh && v >= PULL_TRIGGER) {
        setRefreshing(true)
        animate(pull, PULL_HOLD, spring.smooth)
        try { await onRefresh() } finally {
          setRefreshing(false)
          animate(pull, 0, spring.smooth)
        }
      } else {
        animate(pull, 0, { ...spring.smooth, velocity: 0 })
      }
    }
    node.addEventListener('touchstart', start, { passive: true })
    node.addEventListener('touchmove', move, { passive: false })
    node.addEventListener('touchend', end)
    node.addEventListener('touchcancel', end)
    return () => {
      node.removeEventListener('touchstart', start)
      node.removeEventListener('touchmove', move)
      node.removeEventListener('touchend', end)
      node.removeEventListener('touchcancel', end)
    }
  }, [onRefresh, pull, refreshing])

  return (
    <>
      {onRefresh && (
        <motion.div className="k-ptr" style={{ opacity: refreshing ? 1 : ptrOpacity, y: ptrY, scale: ptrScale }} aria-hidden="true">
          <motion.div className="k-ptr-blob" style={{ scaleY: refreshing ? 1 : ptrStretch }}>
            {refreshing
              ? <IconRefresh className="k-ptr-arrow k-spin" />
              : <motion.span style={{ rotate: arrowRot, display: 'grid' }}><IconArrowDown className="k-ptr-arrow" /></motion.span>}
          </motion.div>
        </motion.div>
      )}
      <div
        className={tabbar ? 'k-scroll' : 'k-scroll no-tabbar'}
        ref={(n) => { el.current = n; if (innerRef) innerRef.current = n }}
        onScroll={onScrollY ? (e) => onScrollY(e.currentTarget.scrollTop) : undefined}
      >
        <motion.div className="k-scroll-inner" style={{ y: pull }}>{children}</motion.div>
      </div>
    </>
  )
}

// ---------------------------------------------------------------- page

export function NavBar({ title, titleOpacity = 1, bgOpacity = 1, left, right }) {
  return (
    <header className="k-navbar">
      <motion.div className="k-navbar-bg" style={{ opacity: bgOpacity }} />
      {left && <div className="k-navbar-side left">{left}</div>}
      <motion.div className="k-navbar-title" style={{ opacity: titleOpacity }}>{title}</motion.div>
      {right && <div className="k-navbar-side right">{right}</div>}
    </header>
  )
}

export function BackButton({ label = 'Back', onClick }) {
  return (
    <motion.button className="k-back" onClick={onClick} whileTap={{ opacity: 0.4 }}>
      <IconBack width={22} height={22} />{label}
    </motion.button>
  )
}

/* A screen: nav bar on top, large title that hands over to the small one as
   it scrolls away, rubber-band list, optional pull to refresh. */
export function Page({ title, large = true, left, right, onRefresh, tabbar = true, children }) {
  const scrollY = useMotionValue(0)
  const titleOpacity = useTransform(scrollY, large ? [24, 44] : [0, 1], [0, 1])
  const largeOpacity = useTransform(scrollY, [0, 30], [1, 0])
  const bgOpacity = useTransform(scrollY, [0, 16], [0, 1])   // iOS: the bar is clear until content scrolls under it
  const stack = useContext(StackCtx)
  const leftSide = left ?? (stack?.depth > 0 ? <BackButton label={stack.backLabel} onClick={stack.pop} /> : null)
  return (
    <div className="k-layer">
      <NavBar title={title} titleOpacity={titleOpacity} bgOpacity={large ? bgOpacity : 1} left={leftSide} right={right} />
      <ScrollArea onRefresh={onRefresh} onScrollY={(v) => scrollY.set(v)} tabbar={tabbar}>
        {large && <motion.h1 className="k-largetitle" style={{ opacity: largeOpacity }}>{title}</motion.h1>}
        {children}
      </ScrollArea>
    </div>
  )
}

// ---------------------------------------------------------------- stack (push / pop)

const StackCtx = createContext(null)
export const useStack = () => useContext(StackCtx)

/* Pages pushed on top of a root page, iOS style. Swipe from the left edge to go back. */
export function Stack({ root, rootTitle }) {
  const [pages, setPages] = useState([])   // [{ key, title, render }]
  const [dir, setDir] = useState(1)

  const push = useCallback((title, render) => {
    setDir(1)
    setPages((p) => [...p, { key: `${p.length}-${title}`, title, render }])
  }, [])
  const pop = useCallback(() => {
    setDir(-1)
    setPages((p) => p.slice(0, -1))
  }, [])

  const depth = pages.length
  const top = depth ? pages[depth - 1] : null
  const backLabel = depth > 1 ? pages[depth - 2].title : rootTitle

  const value = useMemo(() => ({ push, pop, depth, backLabel }), [push, pop, depth, backLabel])
  return (
    <StackCtx.Provider value={value}>
      <AnimatePresence initial={false} custom={dir} mode="popLayout">
        <EdgeSwipe key={top ? top.key : 'root'} dir={dir} canGoBack={depth > 0} onBack={pop}>
          {top ? top.render() : root}
        </EdgeSwipe>
      </AnimatePresence>
    </StackCtx.Provider>
  )
}

function EdgeSwipe({ dir, canGoBack, onBack, children }) {
  const controls = useDragControls()
  const x = useMotionValue(0)
  return (
    <motion.div
      className="k-layer"
      custom={dir}
      variants={pageVariants}
      initial="enter"
      animate="center"
      exit="exit"
      transition={spring.smooth}
      style={{ x, background: 'var(--bg)', boxShadow: '-12px 0 32px rgba(0,0,0,0.35)' }}
      drag={canGoBack ? 'x' : false}
      dragListener={false}
      dragControls={controls}
      dragConstraints={{ left: 0, right: 0 }}
      dragElastic={{ left: 0, right: 1 }}
      onDragEnd={(_, info) => {
        if (info.offset.x > window.innerWidth * 0.35 || info.velocity.x > 500) onBack()
        else animate(x, 0, spring.smooth)
      }}
    >
      {children}
      {canGoBack && (
        <div onPointerDown={(e) => controls.start(e)} style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 22, zIndex: 8, touchAction: 'none' }} />
      )}
    </motion.div>
  )
}

// ---------------------------------------------------------------- expandable (shared element)

/* Renders `preview` in the list. The detail lives in a portal: a fixed
   element inside the (transformed) list would scroll with it.
   Renders `preview` in the list. Tapping it grows the same element into a
   full-screen detail view (shared layout animation). `detail` is a function
   ({ startDrag, close }) => node; dragging from startDrag's element pulls the
   view down, release closes it with velocity. */
export function Expandable({ id, preview, detail, open, onOpen, onClose }) {
  const y = useMotionValue(0)
  const controls = useDragControls()
  const scale = useTransform(y, [0, 400], [1, 0.82])
  useEffect(() => { if (open) y.set(0) }, [open, y])
  return (
    <>
      <motion.div layoutId={`exp-${id}`} onClick={onOpen} transition={spring.smooth} style={{ position: 'relative', zIndex: open ? 0 : 1 }}>
        {preview}
      </motion.div>
      {createPortal(<AnimatePresence>
        {open && (
          <motion.div
            key="detail"
            layoutId={`exp-${id}`}
            transition={spring.smooth}
            style={{ y, scale, position: 'fixed', inset: 0, zIndex: 30, background: 'var(--bg)', overflow: 'hidden', borderRadius: 24 }}
            drag="y"
            dragListener={false}
            dragControls={controls}
            dragConstraints={{ top: 0, bottom: 0 }}
            dragElastic={{ top: 0, bottom: 0.8 }}
            onDragEnd={(_, info) => { if (info.offset.y > 120 || info.velocity.y > 600) onClose(); else animate(y, 0, spring.smooth) }}
          >
            {detail({ startDrag: (e) => controls.start(e), close: onClose })}
          </motion.div>
        )}
      </AnimatePresence>, document.body)}
    </>
  )
}
