// The numbers behind More -> System -> Voice check (/diag/voice): `kairo diag --json` as the PC sends it
// (GET /api/diag/voice, server/app/diag.py) turned into what the page draws. No React here, so node --test can check it.

const ORDER = { FAIL: 0, WARN: 1, PASS: 2 }

export const TONE = { PASS: 'ok', WARN: 'warn', FAIL: 'danger' }

// The harness' stages (server/app/bench_voice.py results, schema 1), in the order a turn goes through them.
export const STAGES = {
  vad_stop: 'Quiet after you speak',
  turn_decision: 'Deciding you are done',
  stt_final: 'Speech to text',
  llm_first_token: 'Voice model, first word',
  tts_first_audio: 'Voice, first sound',
  network: 'Network',
}

export const RATES = [
  ['false_interrupt_rate', 'Cut off by mistake', 'Kairo stopped talking although you were not talking to it'],
  ['missed_turn_rate', 'Turns it missed', 'You spoke and Kairo never answered'],
  ['speaker_false_accept_rate', 'Other voices let in', 'Someone else was answered'],
]

export const ms = (v) => (v == null || Number.isNaN(Number(v)) ? '–' : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`)

export const percent = (r) => (r == null || Number.isNaN(Number(r)) ? '–' : `${(r * 100).toFixed(r > 0 && r < 0.001 ? 2 : 1)} %`)

// Groups in the order the PC sent them, the ones with a FAIL first, then the ones with a WARN, then the healthy ones.
export function groupChecks(checks) {
  const groups = []
  for (const c of checks || []) {
    let g = groups.find((x) => x.group === c.group)
    if (!g) groups.push((g = { group: c.group, checks: [], worst: 'PASS' }))
    g.checks.push(c)
    if (ORDER[c.status] < ORDER[g.worst]) g.worst = c.status
  }
  return groups
    .map((g, i) => [g, i])
    .sort((a, b) => ORDER[a[0].worst] - ORDER[b[0].worst] || a[1] - b[1])
    .map(([g]) => g)
}

// The headline: is the stack healthy, and in how many words.
export function verdict(report) {
  const n = report?.counts || { pass: 0, warn: 0, fail: 0 }
  const line = `${n.pass} pass · ${n.warn} warning${n.warn === 1 ? '' : 's'} · ${n.fail} failed`
  if (!report) return { tone: 'neutral', label: '–', line: '' }
  if (n.fail) return { tone: 'danger', label: 'Not healthy', line }
  return n.warn ? { tone: 'warn', label: 'Healthy, with warnings', line } : { tone: 'ok', label: 'Healthy', line }
}

// A bar for a value against a target: fill and target as percents of a track that is a bit longer than either.
export function meter(value, target) {
  const v = Number(value) || 0
  const t = Number(target) || 0
  const track = Math.max(v, t, 1) * 1.15
  return { fill: Math.min(100, (v / track) * 100), target: t ? (t / track) * 100 : null, over: t > 0 && v > t }
}

// One bar per stage, all on the same scale (the largest p95), the p50 in front of the p95.
export function stageBars(byStage) {
  const rows = Object.entries(byStage || {}).map(([id, s]) => ({ id, title: STAGES[id] || id, p50: s?.p50 ?? null, p95: s?.p95 ?? null }))
  const top = Math.max(1, ...rows.map((r) => r.p95 ?? r.p50 ?? 0))
  return rows.map((r) => ({
    ...r,
    p50Pct: r.p50 == null ? 0 : Math.min(100, (r.p50 / top) * 100),
    p95Pct: r.p95 == null ? 0 : Math.min(100, (r.p95 / top) * 100),
  }))
}

// Key / value rows for the machine and the settings the numbers were made with.
export function machineRows(machine) {
  const m = machine || {}
  return [
    ['Graphics card', m.gpu ? `${m.gpu}${m.vram_gb ? ` · ${m.vram_gb} GB` : ''}` : null],
    ['Memory', m.ram_gb ? `${m.ram_gb} GB` : null],
    ['Processor', m.cpu || null],
  ].filter(([, v]) => v)
}

export function configRows(config) {
  const c = config || {}
  return [
    ['Quiet before Whisper starts', c.vad_stop_s != null ? `${c.vad_stop_s} s` : null],
    ['Turn detector', c.turn_analyzer],
    ['Speech to text', c.stt_model ? `${c.stt_model}${c.stt_compute_type ? ` (${c.stt_compute_type})` : ''}` : null],
    ['Voice model', c.llm_model],
    ['English voice', c.tts_en],
    ['German voice', c.tts_de],
  ].filter(([, v]) => v)
}
