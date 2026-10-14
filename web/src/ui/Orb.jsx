// The voice orb: a soft morphing blob of light that reacts to sound in real
// time (the owner's voice while listening, Kairo's while speaking). Canvas 2D,
// one layer, redrawn per frame; only the orb itself animates.
import { useEffect, useRef } from 'react'
import { prefersReducedMotion } from '../design/motion.js'

// Per state: core colours (inner -> outer) and the halo.
const LOOK = {
  listening: { core: ['#ffffff', '#bff7ee', '#2dd4bf', '#0f766e'], halo: [45, 212, 191], wobble: 1 },
  thinking: { core: ['#fff4e6', '#ffb067', '#ec4899', '#5b1a3a'], halo: [236, 72, 153], wobble: 0.5 },
  speaking: { core: ['#fff1dc', '#ffb067', '#ff7a2f', '#a3221a'], halo: [255, 110, 40], wobble: 1.2 },
  paused: { core: ['#e7e5e4', '#a8a29e', '#57534e', '#1c1917'], halo: [168, 162, 158], wobble: 0.15 },
  idle: { core: ['#fff1dc', '#ffb067', '#ff7a2f', '#7a1410'], halo: [255, 110, 40], wobble: 0.4 },
}

export function VoiceOrb({ state = 'idle', getLevel }) {
  const ref = useRef(null)
  const live = useRef({ state, getLevel, level: 0, t: 0, spin: 0 })
  live.current.state = state
  live.current.getLevel = getLevel

  useEffect(() => {
    const canvas = ref.current
    const ctx = canvas.getContext('2d')
    const reduced = prefersReducedMotion()
    let raf
    let last = performance.now()
    const resize = () => {
      const r = canvas.getBoundingClientRect()
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      canvas.width = Math.round(r.width * dpr)
      canvas.height = Math.round(r.height * dpr)
    }
    resize()
    window.addEventListener('resize', resize)

    const frame = (now) => {
      const L = live.current
      const dt = Math.min(0.05, (now - last) / 1000)
      last = now
      const look = LOOK[L.state] || LOOK.idle
      const target = Math.max(0, Math.min(1, (L.getLevel?.() || 0)))
      // fast attack, slow release: feels like it breathes with the voice
      L.level += (target - L.level) * (target > L.level ? 0.45 : 0.08)
      L.t += dt * (reduced ? 0.25 : 1)
      L.spin += dt * (L.state === 'thinking' ? 1.6 : 0.35) * (reduced ? 0.2 : 1)

      const w = canvas.width
      const h = canvas.height
      const cx = w / 2
      const cy = h / 2
      const base = Math.min(w, h) * 0.27
      const amp = (reduced ? 0.3 : 1) * look.wobble
      const lvl = L.level
      ctx.clearRect(0, 0, w, h)

      // halo
      const halo = ctx.createRadialGradient(cx, cy, base * 0.6, cx, cy, Math.min(w, h) * 0.5)   // fades out inside the canvas: no square edge
      const [hr, hg, hb] = look.halo
      halo.addColorStop(0, `rgba(${hr},${hg},${hb},${0.35 + lvl * 0.35})`)
      halo.addColorStop(0.55, `rgba(${hr},${hg},${hb},${0.08 + lvl * 0.12})`)
      halo.addColorStop(1, `rgba(${hr},${hg},${hb},0)`)
      ctx.fillStyle = halo
      ctx.fillRect(0, 0, w, h)

      // morphing blob: radius = base + sum of slow sines, swollen by the voice level
      const pts = 96
      const r0 = base * (1 + lvl * 0.28 + Math.sin(L.t * 1.3) * 0.015 * amp)
      ctx.beginPath()
      for (let i = 0; i <= pts; i++) {
        const a = (i / pts) * Math.PI * 2
        const n = Math.sin(a * 3 + L.t * 1.7 + L.spin) * 0.035
          + Math.sin(a * 5 - L.t * 2.3) * 0.022
          + Math.sin(a * 2 + L.t * 0.9 - L.spin * 0.5) * 0.03
        const r = r0 * (1 + n * amp * (0.6 + lvl * 2.2))
        const x = cx + Math.cos(a) * r
        const y = cy + Math.sin(a) * r
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
      }
      ctx.closePath()
      const gx = cx + Math.cos(L.spin) * base * 0.35
      const gy = cy - base * 0.35 + Math.sin(L.spin) * base * 0.15
      const core = ctx.createRadialGradient(gx, gy, base * 0.05, cx, cy, r0 * 1.15)
      core.addColorStop(0, look.core[0])
      core.addColorStop(0.3, look.core[1])
      core.addColorStop(0.7, look.core[2])
      core.addColorStop(1, look.core[3])
      ctx.fillStyle = core
      ctx.fill()

      // iridescent rim light
      const rim = ctx.createLinearGradient(cx - r0, cy - r0, cx + r0, cy + r0)
      rim.addColorStop(0, 'rgba(255,176,103,0.55)')
      rim.addColorStop(0.35, 'rgba(236,72,153,0.35)')
      rim.addColorStop(0.65, 'rgba(45,212,191,0.4)')
      rim.addColorStop(1, 'rgba(132,204,22,0.3)')
      ctx.strokeStyle = rim
      ctx.lineWidth = Math.max(1, base * 0.025)
      ctx.stroke()

      // glassy highlight
      const hl = ctx.createRadialGradient(cx - r0 * 0.35, cy - r0 * 0.45, 0, cx - r0 * 0.35, cy - r0 * 0.45, r0 * 0.6)
      hl.addColorStop(0, 'rgba(255,255,255,0.45)')
      hl.addColorStop(1, 'rgba(255,255,255,0)')
      ctx.fillStyle = hl
      ctx.fill()

      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => { cancelAnimationFrame(raf); window.removeEventListener('resize', resize) }
  }, [])

  return <canvas ref={ref} aria-hidden="true" />
}
