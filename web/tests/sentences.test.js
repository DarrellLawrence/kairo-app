// node --test (no extra packages): the sentence cutter for calls (fix plan 29).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { takeSentences } from '../src/sentences.js'

test('cuts finished sentences and keeps the unfinished rest', () => {
  const r = takeSentences('Your dentist appointment is at nine tomorrow. I put it in the calendar and')
  assert.deepEqual(r.sentences, ['Your dentist appointment is at nine tomorrow.'])
  assert.equal(r.rest.trim(), 'I put it in the calendar and')
})

test('a sentence is only finished once the next word starts', () => {
  const r = takeSentences('Your dentist appointment is at nine tomorrow.')
  assert.deepEqual(r.sentences, [])
})

test('a short first sentence waits for the next one', () => {
  const r = takeSentences('Sure. The training plan for this week is ready now. ')
  assert.deepEqual(r.sentences, ['Sure. The training plan for this week is ready now.'])
})

test('later pieces are longer: short sentences are merged', () => {
  const r = takeSentences('Okay. Done. Anything else I can do for you today? ', { first: false })
  assert.deepEqual(r.sentences, ['Okay. Done. Anything else I can do for you today?'])
})

test('abbreviations and German dates do not end a sentence', () => {
  const r = takeSentences('Die Prüfung ist am 3. Oktober, z.B. in Raum vier, bzw. online. Danach hast du frei. ', { first: false })
  assert.deepEqual(r.sentences, ['Die Prüfung ist am 3. Oktober, z.B. in Raum vier, bzw. online.'])
  assert.equal(r.rest.trim(), 'Danach hast du frei.')   // short: waits for more, or the end of the answer
})

test('questions, exclamations and line breaks end sentences', () => {
  const text = 'Do you want me to move the gym session to Thursday? Great, I will do that and tell the coach!\nSee you then. '
  const r = takeSentences(text)
  assert.deepEqual(r.sentences, ['Do you want me to move the gym session to Thursday?', 'Great, I will do that and tell the coach!'])
  assert.equal(r.rest.trim(), 'See you then.')
})

test('streamed in small pieces, the result is the same', () => {
  const text = 'The parcel arrives on Friday between ten and twelve. I will remind you on Thursday evening. Bye for now.'
  let buf = ''
  const out = []
  for (let i = 0; i < text.length; i += 3) {
    buf += text.slice(i, i + 3)
    const r = takeSentences(buf, { first: out.length === 0 })
    out.push(...r.sentences)
    buf = r.rest
  }
  if (buf.trim()) out.push(buf.trim())
  assert.equal(out.join(' '), text)
  assert.equal(out[0], 'The parcel arrives on Friday between ten and twelve.')
})
