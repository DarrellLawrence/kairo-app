// node --test: "No sound? Tap to play" shows only when Kairo speaks and the phone hears nothing.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QUIET_MS, silenceWatch } from '../src/nosound.js'

test('stays hidden while Kairo is heard, even in a pause later in the answer', () => {
  const w = silenceWatch()
  assert.equal(w.feed(0, 0), false)
  assert.equal(w.feed(0.2, 300), false)
  assert.equal(w.feed(0, QUIET_MS + 5000), false)
})

test('shows after a silent stretch', () => {
  const w = silenceWatch()
  assert.equal(w.feed(0, 0), false)
  assert.equal(w.feed(0.001, QUIET_MS - 1), false)
  assert.equal(w.feed(0, QUIET_MS), true)
})

test('starts over for the next answer', () => {
  const w = silenceWatch()
  w.feed(0.5, 0)
  w.reset()
  assert.equal(w.feed(0, 10), false)
  assert.equal(w.feed(0, 10 + QUIET_MS), true)
})
