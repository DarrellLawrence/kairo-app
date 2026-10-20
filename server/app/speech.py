"""What Kairo says on a call is plain speech (owner's call 2026-10-20).

On real calls the owner heard Claude's internal notes: "The owner mentioned…", "L23 (urgent, loop)",
"Let me check the bring-up list", numbered dumps. Claude is told to answer in plain spoken sentences
(realtime.brain_prompt, conversation.TURN_RULES); plain() is the safety net, the one sanitizer for every
text that is spoken or shown on a call (Claude's answers on every voice line, the voice model's words,
the bring-up lines). It
- takes out internal ids (L23, K4, #12, loop 23) and tag parentheticals like "(urgent, loop)";
- drops whole sentences that are meta or thinking out loud ("Let me check…", "I'll look at the … list",
  "The owner …", "According to the bring-up list…"); "the owner" elsewhere becomes "you";
- turns a numbered or bulleted dump into at most two short plain sentences (the first items);
- never returns "" when there was something to say (only thinking out loud is nothing to say).
PlainStream does the same for words that arrive in pieces, without holding back a normal sentence's
first words (the voice starts at its first clause).
"""

from __future__ import annotations

import re

_SPECIAL = re.compile(r"<\|[^<>|\s]{1,40}\|>|<tool_call>.*?</tool_call>|</?tool_call>", re.S)
_MARKDOWN = re.compile(r"\*\*|__|`+|^#{1,6}\s+", re.M)

# ---------------------------------------------------------------- ids and tags
_TAG_WORDS = {"urgent", "normal", "low", "high", "priority", "loop", "loops", "question", "capture", "test", "pushback",
              "reminder", "task", "reply", "promise", "deadline", "waiting", "open", "snoozed", "kind", "item"}
_PAREN = re.compile(r"\s*\(([^()]{1,60})\)")
_ID_PAREN = re.compile(r"\s*\(\s*(?:(?:id|item|loop|task|card|nr|no)\.?\s*[:#]?\s*#?\d{1,6}|#\d{1,6}|[LKQT]\d{1,6})\s*\)", re.I)
_WORD_ID = re.compile(r"\b(loop|task|item|capture|card|approval)\s+(?:id\s*)?#?\s*\d{1,6}\b", re.I)
_ID_WORD = re.compile(r"\b(?:item|id)\s+(?=[LKQT]\d)", re.I)
_LK_ID = re.compile(r"\b[LK]\d{1,6}\b")                    # open loops and Kairo's questions: never words
_QT_ID = re.compile(r"\b[QT]\d{1,6}\b(?=\s*(?:\(|:|-\s))")  # captures and tests, only where they look like ids
_HASH_ID = re.compile(r"\s*#\d{1,6}\b")
_ID_TAIL = re.compile(r"(?:\b[LKQT]\d*|#\d*)\s*$")          # streaming: an id may still be coming

# ---------------------------------------------------------------- meta sentences
_LEAD = r"(?:(?:ok(?:ay)?|so|sure|yeah|yes|alright|all right|right|hm+|well|now|first|next|then|also)[,.!]?\s+)*"
_INTERNAL = (r"(?:the\s+|my\s+|your\s+|kairo'?s\s+)?(?:bring[- ]?up(?:\s+(?:list|items?))?|agenda|call context|"
             r"open loops?(?:\s+list)?|loops?\s+list|tool(?:\s+result)?|kairo[- ]notes?|system prompt|my instructions)")
_META = [re.compile(p, re.I) for p in (
    rf"^{_LEAD}let(?:'s| me| us)\s+(?:check|look|see|think|pull|go|review|find|search|grab|open|read|scan|consult|"
    r"have a look|take a look|figure)\b",
    rf"^{_LEAD}(?:i'll|i will|i'm going to|i am going to|i need to|i should|i have to|i must|i'm|i am)\s+"
    r"(?:quickly\s+|just\s+|first\s+)?(?:check(?:ing)?|look(?:ing)?(?:\s+(?:at|through|over|up|into))?|go(?:ing)? through|"
    r"scan(?:ning)?|review(?:ing)?|consult(?:ing)?|read(?:ing)? through|search(?:ing)?|pull(?:ing)? up)\s+"
    rf"(?:[\w']+[\s-]+){{0,3}}?(?:{_INTERNAL}|list)\b",
    rf"^{_LEAD}(?:the\s+)?(?:owner|user)(?:'s)?\b",
    rf"^{_LEAD}(?:according to|based on|per|from|looking at|checking)\s+{_INTERNAL}\b",
    r"bring[- ]?up (?:list|items?)|ask_kairo|kairo-note|\btool (?:call|result)|\bfunction call|system prompt",
)]
_OWNER_START = re.compile(rf"^{_LEAD}(?:the\s+)?owner", re.I)
_OWNER = [(re.compile(p, re.I), r) for p, r in (
    (r"\bthe owner's\b", "your"), (r"\bthe owner is\b", "you are"), (r"\bthe owner has\b", "you have"),
    (r"\bthe owner was\b", "you were"), (r"\bthe owner does\b", "you do"),
    (r"\bthe owner (want|need|like|prefer|plan|say|ask|mention|think|know|seem|go)s\b", r"you \1"),
    (r"\bthe owner\b", "you"))]

# ---------------------------------------------------------------- lists
_ITEM = re.compile(r"^\s*(?:[-*•‣]|\d{1,2}[.)])\s+(.*\S)\s*$")
_INLINE_FIRST = re.compile(r"(?:^|(?<=[:\n])|(?<=[.!?;]\s))\s*1[.)]\s+(?=\S)")
_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")
MAX_ITEMS = 2
ITEM_WORDS = 14


def _tags(m: re.Match) -> str:
    words = [w for w in re.split(r"[\s,/;]+", m.group(1).lower()) if w]
    return "" if words and all(w in _TAG_WORDS for w in words) else m.group(0)


def _tokens(text: str) -> str:
    """Ids, tags, markup and special tokens out; spacing as it was at both ends."""
    t = _SPECIAL.sub("", text)
    t = _MARKDOWN.sub("", t)
    t = _PAREN.sub(_tags, t)
    t = _ID_PAREN.sub("", t)
    t = _WORD_ID.sub(r"\1", t)
    t = _ID_WORD.sub("", t)
    t = _LK_ID.sub("", t)
    t = _QT_ID.sub("", t)
    t = _HASH_ID.sub("", t)
    t = re.sub(r"[ \t]+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", t)
    lead = " " if text[:1].isspace() else ""
    tail = " " if text[-1:].isspace() else ""
    core = re.sub(r"[ \t]{2,}", " ", t).strip()
    return f"{lead}{core}{tail}" if core else (lead or tail)


def _sentence_start(text: str, original: str | None = None) -> str:
    """Leading punctuation left by a removed id goes; the first letter is made upper case only when
    something was taken off the front ("L23: you owe Lea" -> "You owe Lea")."""
    t = re.sub(r"^[\s,;:.\-–—]+", "", text)
    cut = original is None or t[:1] != original.lstrip()[:1]
    return t[:1].upper() + t[1:] if cut and t[:1].islower() else t


def _is_meta(sentence: str) -> bool:
    return any(rx.search(sentence) for rx in _META)


def _you(text: str) -> str:
    for rx, rep in _OWNER:
        text = rx.sub(rep, text)
    return text


def _clip(item: str) -> str:
    words = item.split()
    return " ".join(words[:ITEM_WORDS]).rstrip(" ,;:.")


def _inline_list(text: str) -> str:
    """ "Three things: 1. a 2. b 3. c" -> one item per line, when it is really 1., 2., 3. in order."""
    m = _INLINE_FIRST.search(text)
    if m is None:
        return text
    head, rest, n = text[:m.start()], text[m.end():], 2
    parts = []
    while True:
        nxt = re.search(rf"\s{n}[.)]\s+(?=\S)", rest)
        if nxt is None:
            break
        parts.append(rest[:nxt.start()])
        rest, n = rest[nxt.end():], n + 1
    if not parts:
        return text
    return head.rstrip() + "\n" + "\n".join(f"{i + 1}. {p.strip()}" for i, p in enumerate(parts + [rest]))


def _sentences(text: str) -> tuple[list[str], list[str]]:
    """(kept sentences, notes about the owner that were dropped: said as "you" if nothing else is left)."""
    kept, rescue = [], []
    for s in _SENTENCES.split(text):
        s = _sentence_start(_tokens(s), s).strip()
        if not re.search(r"\w", s):
            continue
        if _is_meta(s):
            if _OWNER_START.match(s):
                rescue.append(_sentence_start(_you(re.sub(rf"^{_LEAD}", "", s, flags=re.I))))
            continue
        kept.append(_you(s))
    return kept, rescue


def _plain(text: str) -> tuple[str, str]:
    """(what to say, what to say instead if that is empty)."""
    t = _SPECIAL.sub("", text or "")
    t = _inline_list(t)
    lines = t.splitlines()
    out, rescue, i = [], [], 0
    while i < len(lines):
        j = i
        while j < len(lines) and _ITEM.match(lines[j]):
            j += 1
        if j - i >= 2:                                    # a list: its first items, at most two sentences
            items = []
            for line in lines[i:j]:
                body = re.sub(r"^[\s,;:.\-–—]+", "", _tokens(_ITEM.match(line).group(1))).strip()
                if re.search(r"\w", body) and not _is_meta(body):
                    items.append(_clip(_you(body)))
                if len(items) == MAX_ITEMS:
                    break
            intro = out.pop() if out and out[-1].rstrip().endswith(":") else None
            if items and intro:                           # "Open: a, b." (as written after the colon)
                out.append(f"{intro.rstrip().rstrip(':')}: {', '.join(items)}.")
            elif items:                                   # "A. B."
                out.extend(f"{_sentence_start(x)}." for x in items)
            elif intro:
                out.append(intro)
            i = j
            continue
        kept, saved = _sentences(lines[i] if j == i else "\n".join(lines[i:j]))
        out.extend(kept)
        rescue.extend(saved)
        i = max(j, i + 1)
    said = re.sub(r"\s+", " ", " ".join(out)).strip()
    said = re.sub(r"\s+([,.;:!?])", r"\1", said)
    return said, re.sub(r"\s+", " ", " ".join(rescue)).strip()


def plain(text: str) -> str:
    """The one speech sanitizer: what a call may say or show of this text (module docstring)."""
    said, rescue = _plain(text)
    return said or rescue


# ---------------------------------------------------------------- in pieces

_SENT_END = re.compile(r"[.!?…]+[\"'”’»)\]]*\s+|\n+")
_CLAUSE = re.compile(r"[,;:](?=\s)")
_INTERJECTION = re.compile(r"\s*(?:sure|okay|ok|yeah|yes|yep|right|alright|hm+|got it|nice|cool|great|perfect|done|klar|ja|"
                           r"gut|alles klar|genau|super|hey|oh|ah|well|so)[,.!]\s+", re.I)
_LEAD_RX = re.compile(_LEAD, re.I)
_HOLD = re.compile(r"(?:let\b|let's\b|i'll\b|i will\b|i'm going\b|i am going\b|i need to\b|i should\b|i have to\b|i must\b|"
                   r"the owner|the user|owner\b|according\b|based on\b|looking at\b|checking\b|per\b)", re.I)
_LIST_START = re.compile(r"\s*(?:[-*•‣]|\d{1,2}[.)])\s")
_MAYBE_LIST = re.compile(r"\s*(?:[-*•‣]|\d{1,2}[.)]?)?$")


class PlainStream:
    """plain() for words that arrive in pieces. A sentence is decided on its first words: one that may be
    meta ("Let me…", "The owner…", "According to…", "I'll …") is held to its end and checked whole; any
    other goes out at once, piece by piece, with ids and tags taken out (a piece that could still be an id
    or a tag waits for the next one). A list is held to the end and said as at most two sentences."""

    def __init__(self) -> None:
        self.buf = ""
        self.mode = "start"            # start (a sentence begins) / pass / hold / list
        self.said = False              # anything said yet (else the notes about the owner, as "you")
        self.rescue: list[str] = []
        self._space = True             # what went out ended with a space (or nothing went out yet)

    def _out(self, text: str) -> str:
        text = text.replace("\n", " ")
        if self._space:
            text = text.lstrip()
        if text:
            self._space = text[-1].isspace()
            self.said = self.said or bool(re.search(r"\w", text))
        return text

    def _whole(self, text: str) -> str:
        said, rescue = _plain(text)
        if rescue:
            self.rescue.append(rescue)
        return self._out(said + " ") if said else ""

    def feed(self, delta: str) -> str:
        self.buf += delta or ""
        out = ""
        while self.buf:
            if self.mode == "list":
                return out
            if self.mode == "start":
                if _LIST_START.match(self.buf):
                    self.mode = "list"
                    return out
                if _MAYBE_LIST.fullmatch(self.buf):
                    return out
                m = _INTERJECTION.match(self.buf)
                if m:                                         # "Sure, " / "Okay, ": never meta, a cached opener
                    out += self._out(m.group(0))
                    self.buf = self.buf[m.end():]
                    continue
                s = self.buf[_LEAD_RX.match(self.buf.lstrip()).end() + len(self.buf) - len(self.buf.lstrip()):]
                if _HOLD.match(s):
                    self.mode = "hold"
                    continue
                if _SENT_END.search(self.buf) or _CLAUSE.search(s) or len(re.findall(r"\S+(?=\s)", s)) >= 2:
                    self.mode = "pass"
                    continue
                return out
            end = _SENT_END.search(self.buf)
            if self.mode == "hold":
                if end is None:
                    return out
                out += self._whole(self.buf[:end.end()])
                self.buf, self.mode = self.buf[end.end():], "start"
                continue
            if end is not None:                                # pass: this sentence is fine; the next starts fresh
                out += self._out(_tokens(self.buf[:end.end()]))
                self.buf, self.mode = self.buf[end.end():], "start"
                continue
            cut = len(self.buf)
            if not self.buf[-1:].isspace():                    # a word still growing
                cut = self.buf.rfind(" ") + 1
            opened = self.buf.rfind("(", 0, cut)
            if opened >= 0 and ")" not in self.buf[opened:cut]:   # a parenthetical still open: maybe a tag
                cut = opened
            tail = _ID_TAIL.search(self.buf[:cut])
            if tail:
                cut = tail.start()
            if cut <= 0:
                return out
            out += self._out(_tokens(self.buf[:cut]))
            self.buf = self.buf[cut:]
            return out
        return out

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        mode, self.mode = self.mode, "start"
        out = ""
        if rest.strip():
            out = self._out(_tokens(rest)) if mode == "pass" else self._whole(rest).rstrip()
        if not self.said and self.rescue:
            out += self._out(" ".join(self.rescue))
        return out.rstrip() if out.endswith(" ") and mode != "pass" else out
