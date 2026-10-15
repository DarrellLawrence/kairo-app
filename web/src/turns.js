// Every voice and chat turn, step by step (owner's request 2026-10-15; server/app/turns.py).
// Marks are ms since the end of your speech (or Send): endpoint, transcribed, sent, first_words,
// first_sentence, audio_ready, playing. The PC adds speech to text and the wait for the brain.
// More -> System -> Voice speed shows p50/p95 per step and the last turns.

import { device } from './clientlog.js'

let seq = 0

export function startTurn(path, t0 = performance.now()) {
  const marks = {}
  const server = {}
  let model = null
  let finished = false
  const id = `${Date.now().toString(36)}-${(seq += 1)}`
  return {
    path,
    mark(name, at = performance.now()) { if (!(name in marks)) marks[name] = Math.max(0, Math.round(at - t0)) },
    has(name) { return name in marks },
    server(values) {
      for (const [k, v] of Object.entries(values || {})) if (typeof v === 'number' && Number.isFinite(v)) server[k] = Math.max(0, Math.round(v))
    },
    model(m) { if (m) model = String(m) },
    finish(outcome, reason) {
      if (finished) return
      finished = true
      const body = { turn_id: id, path, marks, server, model, outcome, reason: reason ? String(reason).slice(0, 200) : null, device: device() }
      try {
        fetch('/api/voice/turns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true })
          .catch(() => {})
      } catch { /* measuring must never break a turn */ }
    },
  }
}

// What happened to a turn's audio, from what voice.js threw.
export function audioOutcome(err) {
  if (!err) return ['played', null]
  if (err.name === 'AbortError') return ['interrupted', null]
  if (err.name === 'NeedTap') return ['blocked', err.why || err.message || 'NeedTap']
  return ['failed', err.message || err.name]
}
