// Fix plan 29: on a call Kairo starts speaking after its first sentence, while the rest of the
// answer is still being written. takeSentences() cuts the finished sentences off the text so far.

// A period here doesn't end a sentence: "z.B.", "Dr.", "3. Oktober" (German dates), "No. 5".
const NOT_AN_END = /(?:^|[\s(])(?:dr|mr|mrs|ms|st|nr|no|bzw|ca|vs|etc|usw|inkl|evtl|ggf|z\.b|u\.a|d\.h|e\.g|i\.e)\.$|\d\.$/i
// The end of a sentence: . ! ? … (and closing quotes or brackets) before a space, or a line break.
const END = /[.!?…]+["'”’»)\]]*(?=\s)|\n+/g

export const FIRST_MIN = 20   // the first piece starts the voice: short, but not just "Sure."
export const NEXT_MIN = 40    // later pieces are made while the one before plays: fewer, longer ones

/* -> { sentences: [...finished pieces], rest: 'text not finished yet' }. first: nothing was
   spoken yet in this answer. A piece shorter than the minimum waits for the next sentence. */
export function takeSentences(text, { first = true } = {}) {
  const sentences = []
  let start = 0
  END.lastIndex = 0
  let m
  while ((m = END.exec(text)) !== null) {
    const end = m.index + m[0].length
    if (m[0][0] === '.' && NOT_AN_END.test(text.slice(Math.max(start, m.index - 6), m.index + 1))) continue
    const piece = text.slice(start, end).trim()
    if (piece.length < (first && sentences.length === 0 ? FIRST_MIN : NEXT_MIN)) continue
    sentences.push(piece)
    start = end
  }
  return { sentences, rest: text.slice(start) }
}
