// node --test: "Teach Kairo your voice" (voiceprint.js). The mic is a stand-in that hands out 20 ms frames
// (voicelink.js's format); the PC is a fake fetch.
import assert from 'node:assert/strict'
import { test } from 'node:test'

const { PASSAGE, SECONDS, joinFrames, recordVoice, sendVoice } = await import('../src/voiceprint.js')

function fakeMic() {
  const mic = { stopped: false, push: null }
  const capture = async (onFrame) => { mic.push = onFrame; return { stop() { mic.stopped = true } } }
  const frames = (n, rms = 0.1) => { for (let i = 0; i < n; i++) mic.push?.(new Int16Array(320).fill(i % 100), rms) }
  return { mic, capture, frames }
}

const tick = () => new Promise((r) => setTimeout(r, 0))

test('the frames become one 16-bit recording', () => {
  const pcm = joinFrames([new Int16Array([1, 2]), new Int16Array([3]), new Int16Array(0)])
  assert.deepEqual([...pcm], [1, 2, 3])
})

test('it records 30 seconds, says how far and how much speech, then closes the mic', async () => {
  const { mic, capture, frames } = fakeMic()
  let released = 0
  const seen = []
  const rec = recordVoice({ capture, release: () => { released += 1 }, onProgress: (p) => seen.push(p) })
  await tick()
  frames(500, 0.1)               // 10 s of speech
  frames(250, 0.001)             // 5 s of a quiet room
  assert.equal(seen.at(-1).done, 0.5)
  assert.equal(seen.at(-1).speech, 10)
  frames(800, 0.1)               // more than enough: it stops by itself at 30 s
  const pcm = await rec.done
  assert.equal(pcm.length, SECONDS * 16000)
  assert.equal(mic.stopped, true)
  assert.equal(released, 1)
  assert.equal(seen.at(-1).done, 1)
})

test('stop sends what is there so far', async () => {
  const { mic, capture, frames } = fakeMic()
  const rec = recordVoice({ capture, release: () => {} })
  await tick()
  frames(100)
  rec.stop()
  frames(100)                    // after stop: not kept
  assert.equal((await rec.done).length, 100 * 320)
  assert.equal(mic.stopped, true)
})

test('no mic: the recording fails with the reason', async () => {
  const rec = recordVoice({ capture: async () => { throw new Error('NotAllowedError') }, release: () => {} })
  await assert.rejects(rec.done, /NotAllowedError/)
})

test('the recording goes to the PC as 16 kHz 16-bit audio; the PC says what to do when it is not enough', async () => {
  const calls = []
  const ok = async (url, opts) => { calls.push({ url, opts }); return { ok: true, status: 200, text: async () => JSON.stringify({ check: true }) } }
  const pcm = new Int16Array([5, 6, 7])
  assert.deepEqual(await sendVoice(pcm, ok), { check: true })
  assert.equal(calls[0].url, '/api/voice/speaker/enroll')
  assert.equal(calls[0].opts.method, 'POST')
  assert.equal(calls[0].opts.headers['Content-Type'], 'audio/l16; rate=16000')
  assert.deepEqual([...new Int16Array(calls[0].opts.body)], [5, 6, 7])
  const short = async () => ({ ok: false, status: 400, text: async () => JSON.stringify({ detail: 'only 4 s of speech: read the text aloud for about 30 seconds' }) })
  await assert.rejects(sendVoice(pcm, short), /only 4 s of speech/)
})

test('the text to read is about half a minute and nothing personal', () => {
  const words = PASSAGE.split(/\s+/).length
  assert.ok(words > 70 && words < 130, `${words} words`)
})
