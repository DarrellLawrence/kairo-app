// More -> System -> Sound check, test 2 ("like a call"), over the real call line. Kairo's voice on a call is not a sound
// the app plays: it is the PC's voice arriving as the WebRTC connection's own stream in an <audio> element, the one
// route iOS echo-cancels. A test that plays a sound in the app with the mic open (the old test 2) proves something
// else. So when the call line is possible, this starts it (the voice button's line, which records no call), has the
// PC say one fixed line (no model involved) and reports when its voice has played. Research §7 item 5.
import { flagOn } from './flags.js'
import { rtcStatus, rtcSupported, startRtc } from './rtclink.js'

export const CALL_TEST_TEXT = 'Sound check two. This is how I sound on a call.'
const SPOKEN_TIMEOUT_MS = 20000

/* Resolves to a controller { line, say(), tapToPlay(), close() } with the line connected, or null when the call line
   can't be used (switch off, not supported, the PC can't take a WebRTC call, or it fell over while connecting):
   then the screen runs the old test. say() resolves 'spoken' when the voice has played, 'timeout' or 'fallback'. */
export async function startCallCheck({ on = {}, supported = rtcSupported, status = rtcStatus, start = startRtc, timeoutMs = SPOKEN_TIMEOUT_MS } = {}) {
  if (!flagOn('g7.sound_check_rtc') || !supported()) return null
  const st = await status()
  if (!st?.available) return null
  let waiting = null
  const settle = (how) => {
    if (!waiting) return
    clearTimeout(waiting.timer)
    waiting.resolve(how)
    waiting = null
  }
  const line = start({ path: 'chat_voice', on: { ...on, answered: () => settle('spoken'), fallback: () => settle('fallback') } })
  if (!(await line.ready)) { line.close(); return null }
  return {
    line,
    say: () => new Promise((resolve) => {
      settle('replaced')
      waiting = { resolve, timer: setTimeout(() => settle('timeout'), timeoutMs) }
      line.say(CALL_TEST_TEXT)
    }),
    tapToPlay: () => line.tapToPlay(),
    close: () => { settle('closed'); line.close() },
  }
}
