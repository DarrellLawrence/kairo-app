// node --test: a call's answer is spoken sentence by sentence (fix plan 29). The browser parts are
// stand-ins: fetch returns the sentence as "audio", Audio "plays" it by noting its text.
import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'

const events = []
const blobs = new Map()
let failOn = null   // a sentence iOS refuses to play (NeedTap)

globalThis.window = globalThis
globalThis.fetch = async (url, opts = {}) => {
  if (url !== '/api/voice/speak') return { ok: true, json: async () => ({}) }
  const { text } = JSON.parse(opts.body)
  events.push(`fetch ${text}`)
  await new Promise((r) => setTimeout(r, 5))
  if (opts.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' })
  return { ok: true, arrayBuffer: async () => new TextEncoder().encode(text).buffer }
}
URL.createObjectURL = (blob) => { const id = `blob:${blobs.size}`; blobs.set(id, blob); return id }
URL.revokeObjectURL = () => {}
globalThis.Audio = class {
  play() {
    blobs.get(this.src).text().then((text) => {
      if (text === failOn) { this.onerror?.(); return }
      events.push(`play ${text}`)
      setTimeout(() => { this.currentTime = 1; this.ontimeupdate?.(); this.onended?.() }, 20)
    })
    return Promise.resolve()
  }
  pause() {}
}

const { speechQueue } = await import('../src/voice.js')
const tick = (ms) => new Promise((r) => setTimeout(r, ms))

beforeEach(() => { events.length = 0; failOn = null })

test('sentences play in order, the next one is made while the current one plays', async () => {
  const q = speechQueue({ call: true })
  q.push('First sentence.')
  await tick(10)
  q.push('Second sentence.')
  q.push('Third sentence.')
  q.end()
  await q.done
  assert.deepEqual(events.filter((e) => e.startsWith('play')), ['play First sentence.', 'play Second sentence.', 'play Third sentence.'])
  assert.ok(events.indexOf('fetch Second sentence.') < events.indexOf('play Second sentence.'))
  assert.ok(events.indexOf('fetch Third sentence.') < events.indexOf('play Second sentence.'))   // made ahead
})

test('it waits for more until the answer is finished', async () => {
  const q = speechQueue({ call: true })
  let finished = false
  q.done.then(() => { finished = true })
  q.push('One.')
  await tick(80)
  assert.equal(finished, false)   // the answer may still go on
  q.push('Two.')
  q.end()
  await q.done
  assert.deepEqual(events.filter((e) => e.startsWith('play')), ['play One.', 'play Two.'])
})

test('talking over Kairo stops it at once', async () => {
  const abort = new AbortController()
  const q = speechQueue({ signal: abort.signal, call: true })
  q.push('Something long.')
  q.push('Never heard.')
  await tick(12)
  abort.abort()
  await q.done
  q.push('Too late.')
  await tick(40)
  assert.ok(!events.includes('play Never heard.'))
  assert.ok(!events.some((e) => e.includes('Too late.')))
})

test('a sentence iOS would not play waits for the tap, then the rest follows', async () => {
  failOn = 'Blocked.'
  let taps = 0
  const q = speechQueue({ call: true, onNeedTap: async () => { taps += 1; failOn = null } })
  q.push('Blocked.')
  q.push('After the tap.')
  q.end()
  await q.done
  assert.equal(taps, 1)
  assert.ok(events.includes('play After the tap.'))
})
