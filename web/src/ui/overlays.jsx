// Sheet (bottom sheet, drag down to dismiss) and Toast (top banner, optional Undo).
import { AnimatePresence, animate, motion, useDragControls, useMotionValue, useTransform } from 'motion/react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { spring } from '../design/motion.js'

export function Sheet({ open, onClose, title, children }) {
  const y = useMotionValue(0)
  const controls = useDragControls()
  const backdrop = useTransform(y, [0, 400], [1, 0])
  const panel = useRef(null)

  useEffect(() => { if (open) y.set(0) }, [open, y])

  const onDragEnd = (_, info) => {
    const h = panel.current?.offsetHeight || 400
    if (info.velocity.y > 600 || info.offset.y > h * 0.3) onClose()
    else animate(y, 0, spring.bouncy)
  }

  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="backdrop"
            className="k-backdrop"
            style={{ opacity: backdrop }}
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.div
            key="sheet"
            ref={panel}
            className="k-sheet"
            role="dialog"
            aria-modal="true"
            aria-label={title}
            style={{ y }}
            initial={{ y: '100%' }}
            animate={{ y: 0, transition: spring.smooth }}
            exit={{ y: '100%', transition: { ...spring.smooth, stiffness: 380 } }}
            drag="y"
            dragListener={false}
            dragControls={controls}
            dragConstraints={{ top: 0, bottom: 0 }}
            dragElastic={{ top: 0.08, bottom: 1 }}
            onDragEnd={onDragEnd}
          >
            <div className="k-sheet-grab" onPointerDown={(e) => controls.start(e)}><span /></div>
            {title && <div className="k-sheet-title" onPointerDown={(e) => controls.start(e)}>{title}</div>}
            <div className="k-sheet-body">{children}</div>
          </motion.div>
        </>
      )}
    </AnimatePresence>,
    document.body,
  )
}

// ---------------------------------------------------------------- toasts

const ToastCtx = createContext(null)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const timers = useRef({})

  const dismiss = useCallback((id) => {
    clearTimeout(timers.current[id])
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  // show({ text, icon, action: { label, onAction }, duration, onTimeout })
  const show = useCallback((toast) => {
    const id = Math.random().toString(36).slice(2)
    const duration = toast.duration ?? 3000
    setToasts((t) => [...t.slice(-2), { ...toast, id, duration }])
    timers.current[id] = setTimeout(() => { dismiss(id); toast.onTimeout?.() }, duration)
    return id
  }, [dismiss])

  const value = useMemo(() => ({ show, dismiss }), [show, dismiss])
  return (
    <ToastCtx.Provider value={value}>
      {children}
      {createPortal(
        <div className="k-toasts" aria-live="polite">
          <AnimatePresence initial={false}>
            {toasts.map((t) => (
              <motion.div
                key={t.id}
                layout
                className="k-toast"
                initial={{ opacity: 0, y: -40, scale: 0.92 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -30, scale: 0.94 }}
                transition={spring.bouncy}
                drag="y"
                dragConstraints={{ top: 0, bottom: 0 }}
                dragElastic={{ top: 0.6, bottom: 0.1 }}
                onDragEnd={(_, info) => { if (info.offset.y < -24) { dismiss(t.id); t.onTimeout?.() } }}
                style={{ position: 'relative' }}
              >
                {t.icon && <span className="k-toast-icon">{t.icon}</span>}
                <span className="k-toast-text">{t.text}</span>
                {t.action && (
                  <button className="k-btn ghost sm" onClick={() => { dismiss(t.id); t.action.onAction() }}>{t.action.label}</button>
                )}
                {t.action && (
                  <motion.span className="k-toast-progress" initial={{ scaleX: 1 }} animate={{ scaleX: 0 }} transition={{ duration: t.duration / 1000, ease: 'linear' }} />
                )}
              </motion.div>
            ))}
          </AnimatePresence>
        </div>,
        document.body,
      )}
    </ToastCtx.Provider>
  )
}

export function useToast() {
  return useContext(ToastCtx)
}
