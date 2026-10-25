// The owner-only switches (server/app/flags.py; GET/PUT /api/flags), seen from the phone.
//
// Two jobs. (1) The phone's own behaviour can sit behind a switch (g7.*): flagOn(name) answers at once, from
// what the PC said last, because a call starts inside a tap and iOS wants the audio set up right there, with
// no time to ask the PC. The answer is read once when a call starts, never per frame. (2) More -> System ->
// Experiments lists whatever the PC lists: groupFlags() and describeFlag() word it, setFlag() flips it.
import { api } from './api.js'

// The phone's own switches and their defaults: the same names and values as server/app/flags.py (a test pins
// them). Used until the PC has answered, and when it is away.
export const DEFAULTS = {
  'g7.mic_constraints': true,
  'g7.call_audio_mode': true,
  'g7.call_logs': true,
  'g7.no_aec_warning': true,
  'g7.listen_on_barge_in': true,    // off until the owner switches it on (P12): the PC sends {"type":"barge_in"} whenever it cuts Kairo off (g6)
  'g7.sound_check_rtc': true,
}

let known = null   // name -> on, from the PC's last list

export function resetFlags() { known = null }

/* The list the PC sent (GET /api/flags, or what PUT answers): remember it. */
export function applyFlags(rows) {
  if (!Array.isArray(rows)) return rows
  known = new Map(rows.filter((r) => r && typeof r.name === 'string').map((r) => [r.name, Boolean(r.on)]))
  return rows
}

export function flagOn(name) {
  if (known && known.has(name)) return known.get(name)
  return DEFAULTS[name] === true
}

/* Ask the PC for the list. Never throws: with the PC away the last answer (or the defaults) stays. */
export async function refreshFlags() {
  try {
    const res = await fetch('/api/flags')
    if (!res.ok) return null
    const rows = await res.json()
    if (!Array.isArray(rows)) return null
    return applyFlags(rows)
  } catch { return null }
}

/* Read the list now and again each time the app comes back to the front (a switch flipped on another device). */
export function keepFlags() {
  refreshFlags()
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshFlags() })
}

/* value: true, false, or null for "back to the default". Returns the PC's new list. */
export async function setFlag(name, value) {
  return applyFlags(await api(`/api/flags/${encodeURIComponent(name)}`, { method: 'PUT', body: { on: value } }))
}

// ---------------------------------------------------------------- the Experiments list

// The build groups' names (the markers in server/app/flags.py); a prefix nobody named shows as itself.
export const GROUP_TITLES = {
  g1: 'Claude CLI', g2: 'Whisper', g3: 'Mouth', g4: 'Jitter buffer', g5: 'Hand-off', g6: 'Turn-taking',
  g7: 'Phone', g8: 'Windows', g9: 'Speech text', g10: 'Fast paths', g11: 'Router',
}

const prefixOf = (name) => (name.includes('.') ? name.split('.')[0] : name)
const groupNumber = (prefix) => { const m = /^g(\d+)$/.exec(prefix); return m ? Number(m[1]) : Infinity }

/* [{ prefix, title, rows }]: the build groups in number order, then the rest by name; switches by name. */
export function groupFlags(rows) {
  const groups = new Map()
  for (const r of rows || []) {
    const prefix = prefixOf(r.name)
    if (!groups.has(prefix)) groups.set(prefix, { prefix, title: GROUP_TITLES[prefix] || prefix, rows: [] })
    groups.get(prefix).rows.push(r)
  }
  return [...groups.values()]
    .map((g) => ({ ...g, rows: g.rows.slice().sort((a, b) => a.name.localeCompare(b.name)) }))
    .sort((a, b) => groupNumber(a.prefix) - groupNumber(b.prefix) || a.prefix.localeCompare(b.prefix))
}

const word = (on) => (on ? 'On' : 'Off')

/* What a row says: now, the default, whether the owner changed it, and what a tap on the main button does. */
export function describeFlag(row) {
  return {
    now: word(row.on), default: word(row.default), changed: row.on !== row.default,
    next: !row.on, nextLabel: row.on ? 'Turn off' : 'Turn on',
  }
}

export const changedCount = (rows) => (rows || []).filter((r) => r.on !== r.default).length
