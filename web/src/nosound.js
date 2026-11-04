// "No sound? Tap to play" only when Kairo is speaking and this phone hears nothing (owner 2026-11-04: the orange
// button showed on every greeting, sound or not). Feed it the output level while speaking; it says when the
// line has been silent for QUIET_MS. Any sound settles it for the rest of that answer.
export const QUIET_MS = 3000
export const HEARD_LEVEL = 0.01      // the same loudness rtclink counts as "playing"

export function silenceWatch({ quietMs = QUIET_MS, heard = HEARD_LEVEL } = {}) {
  let since = null
  let sound = false
  return {
    reset() { since = null; sound = false },
    // true: speaking for quietMs with no sound at all
    feed(level, now) {
      if (sound) return false
      if (level >= heard) { sound = true; return false }
      if (since === null) since = now
      return now - since >= quietMs
    },
  }
}
