// node --test: a call's audio setup (research §7, g7.call_audio_mode). Kairo's voice comes only as the call
// connection's own stream in an <audio> element; for iOS to cancel the echo, the page must be in the call's audio
// mode BEFORE the mic opens and must play nothing else (no silent Web Audio buffer, no blob <audio>) while the
// mic is open. Every place a call starts or is tapped is run here against stand-ins that record what the page did.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, test } from 'node:test'
import { applyFlags, resetFlags } from '../src/flags.js'
import { startRealtime, startLine } from '../src/realtime.js'
import { startRtc } from '../src/rtclink.js'
import { closeMic, micStream, unlockAudio } from '../src/voice.js'
import { installPhone, playbackBeforeCall } from './phone-fakes.js'

const flag = (on) => [{ name: 'g7.call_audio_mode', on, default: true, what: '' }]
afterEach(() => resetFlags())

const settle = () => new Promise((r) => setTimeout(r, 5))

// The entry points that live in plain JS (the ones in .jsx screens are checked by reading the source, below).
// Each runs from a phone whose mic is not open yet, as in a tap that answers a call.
const ENTRIES = {
  'unlockAudio({call:true}): Call.jsx tap, answer and orb; More.jsx sound check': async () => { unlockAudio({ call: true }) },
  'startRtc: the line starting (rtclink.js ready)': async () => { const l = startRtc({ path: 'call' }); await l.ready; l.close() },
  'startRtc: tap to play (rtclink.js tapToPlay)': async () => { const l = startRtc({ path: 'call' }); await l.ready; l.tapToPlay(); l.close() },
  'startRtc: the voice button (chat_voice)': async () => { const l = startRtc({ path: 'chat_voice' }); await l.ready; l.close() },
  'startRealtime: the line starting (realtime.js ready)': async () => { const l = startRealtime({ path: 'call' }); await l.ready; l.close() },
  'startRealtime: tap to play (realtime.js tapToPlay)': async () => { const l = startRealtime({ path: 'call' }); await l.ready; l.tapToPlay(); l.close() },
  'startLine: the tap that starts a call (realtime.js startLine)': async () => {
    const l = startLine({ path: 'call', status: { available: false, reason: 'local' } })
    await l.ready
    l.close()
  },
}

for (const [name, run] of Object.entries(ENTRIES)) {
  test(`g7.call_audio_mode on, ${name}: call audio mode first, nothing else played`, async () => {
    const { rec } = installPhone()
    await run()
    await settle()
    assert.equal(playbackBeforeCall(rec.session), false, JSON.stringify(rec.session))
    assert.ok(rec.session.length > 0 && rec.session[0].type === 'play-and-record', JSON.stringify(rec.session))
    assert.equal(rec.buffers, 0, 'no silent Web Audio buffer while a call is set up')
    assert.deepEqual(rec.plays, [], 'no blob <audio> priming while a call is set up')
  })
}

test('g7.call_audio_mode on: the first tap of a call sets the mode before the mic exists, and the mic opens inside it', async () => {
  const { rec } = installPhone()
  unlockAudio({ call: true })
  assert.deepEqual(rec.session, [{ type: 'play-and-record', mic: false }])
  const l = startRtc({ path: 'call' })
  await l.ready
  assert.equal(rec.gum.length, 1)
  assert.deepEqual(rec.session, [{ type: 'play-and-record', mic: false }])   // nothing flipped it on the way
  l.close()
  assert.equal(rec.session.at(-1).type, 'playback')                          // the hang-up closes the mic and goes back
})

test('g7.call_audio_mode on: the call\'s <audio> is the connection\'s own and is not muted', async () => {
  const { rec } = installPhone()
  const l = startRtc({ path: 'call' })
  await l.ready
  assert.equal(rec.audios.length, 1)                  // one element, the call's (no priming element beside it)
  assert.equal(rec.audios[0].muted, false)
  l.close()
})

test('g7.call_audio_mode on: a call that never got its mic (hung up first) leaves the audio mode as chat replies need it', () => {
  const { rec } = installPhone()
  unlockAudio({ call: true })                         // the tap that was meant to start the call
  closeMic()                                          // hung up before the mic opened
  assert.deepEqual(rec.session.map((x) => x.type), ['play-and-record', 'playback'])
  closeMic()                                          // and a second close changes nothing
  assert.equal(rec.session.length, 2)
})

test('g7.call_audio_mode on: replacing a mic whose track ended does not flip to playback on the way, off it does (the old way)', async () => {
  for (const on of [true, false]) {
    applyFlags(flag(on))
    const { rec, micTrack } = installPhone()
    await micStream()                                  // a mic from earlier (the chat mic, kept for a minute)
    micTrack.readyState = 'ended'                      // ... whose track iOS ended
    rec.session.length = 0
    micTrack.readyState = 'ended'
    await micStream()                                  // the next call opens a new one
    assert.deepEqual(rec.session.map((x) => x.type), on ? [] : ['playback', 'play-and-record'], `flag ${on}`)
    closeMic()
  }
})

test('g7.call_audio_mode off: the old start, normal audio first, a silent buffer and a silent blob', async () => {
  applyFlags(flag(false))
  const { rec } = installPhone()
  unlockAudio({ call: true })
  assert.equal(rec.session[0].type, 'playback')
  assert.equal(rec.buffers, 1)
  assert.equal(rec.plays.length, 1)
  const l = startRtc({ path: 'call' })
  await l.ready
  assert.equal(playbackBeforeCall(rec.session), true)   // what the research measured on the old code
  l.close()
})

test('a plain unlockAudio() (chat replies) is unchanged: playback, silent buffer, silent blob', () => {
  const { rec } = installPhone()
  unlockAudio()
  assert.equal(rec.session[0].type, 'playback')
  assert.equal(rec.buffers, 1)
  assert.equal(rec.plays.length, 1)
})

// ---------------------------------------------------------------- the entry points inside the screens

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const unlocks = (text) => [...text.matchAll(/unlockAudio\(([^)]*)\)/g)].map((m) => m[1].replace(/\s+/g, ' ').trim())

test('every call entry point asks for the call audio mode (the plan\'s ten, and the voice button)', () => {
  const callOnly = ['screens/Call.jsx', 'rtclink.js', 'realtime.js']   // every unlockAudio here is a call's
  const want = { 'screens/Call.jsx': 3, 'rtclink.js': 2, 'realtime.js': 3 }
  for (const f of callOnly) {
    const found = unlocks(src(f))
    assert.equal(found.length, want[f], `${f}: ${found.length} unlockAudio calls`)
    for (const a of found) assert.equal(a, '{ call: true }', `${f}: unlockAudio(${a})`)
  }
  // More.jsx: the sound check's tap is a call's only for the call test, the speaker test is a chat reply's
  const more = src('screens/More.jsx')
  const check = more.slice(more.indexOf('function SoundCheckPage'), more.indexOf('function AcceptancePage'))
  assert.deepEqual(unlocks(check), ['{ call: step === \'call\' }', '{ call: true }'])
  // Chat.jsx: the voice button's tap, before startLine
  const chat = src('screens/Chat.jsx')
  const press = chat.slice(chat.indexOf('const pressMic'), chat.indexOf('const toggleSpeak'))
  assert.deepEqual(unlocks(press), ['{ call: true }'])
  assert.deepEqual(unlocks(chat.slice(chat.indexOf('const toggleSpeak'))), [''])   // the speak toggle (no call) stays plain
})
