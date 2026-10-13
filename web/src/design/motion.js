// Motion tokens. Springs, not durations: things move like objects with mass.
// Only transform and opacity are animated (cheap for the GPU, 60 fps).
// "Reduce Motion" on the iPhone: <MotionConfig reducedMotion="user"> in App
// turns transforms into fades; CSS animations stop via the media query.

export const spring = {
  snappy: { type: 'spring', stiffness: 520, damping: 38, mass: 0.8 },   // taps, toggles, presses
  smooth: { type: 'spring', stiffness: 300, damping: 34, mass: 1 },     // pages, sheets, layout
  bouncy: { type: 'spring', stiffness: 420, damping: 18, mass: 0.9 },   // arrivals, success
}

export const duration = { fast: 0.12, base: 0.22, slow: 0.38 }

// iOS-like rubber band: the further you pull, the harder it gets.
export function rubber(distance, dimension = 600, constant = 0.55) {
  const sign = Math.sign(distance)
  const d = Math.abs(distance)
  return sign * (1 - 1 / ((d * constant) / dimension + 1)) * dimension
}

// Tab switch: crossfade with a slight slide in the direction of travel.
export const tabVariants = {
  enter: (dir) => ({ opacity: 0, x: dir * 18 }),
  center: { opacity: 1, x: 0 },
  exit: (dir) => ({ opacity: 0, x: dir * -18 }),
}

// Page push / pop like iOS: the new page slides in from the right, the old one
// moves a third to the left and dims.
export const pageVariants = {
  enter: (dir) => (dir > 0 ? { x: '100%', opacity: 1 } : { x: '-30%', opacity: 0.6 }),
  center: { x: 0, opacity: 1 },
  exit: (dir) => (dir > 0 ? { x: '-30%', opacity: 0.6 } : { x: '100%', opacity: 1 }),
}

export const reducedMotionQuery = '(prefers-reduced-motion: reduce)'
export const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.(reducedMotionQuery).matches
