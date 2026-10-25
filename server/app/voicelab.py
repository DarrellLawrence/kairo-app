"""The voice lab (owner's request 2026-10-16): measure the voice candidates on THIS PC and pick.

More -> System -> Voice lab -> Run (about 10-20 minutes; the voice model is off meanwhile, calls use
the old pipeline). For every installed candidate model (voicellm.json), in llama.cpp on the RTX 3060:
  load time and VRAM; first-token time (p50/p95) on a warm call, English and German; tokens/s;
  tool calls: does it call ask_kairo when it must (recall) and not when it mustn't (false calls);
  fidelity: does it say Claude's answer with every fact right; naturalness, rated blind by Claude;
  the Today card: of the questions the card answers, how many it answers on the PC with the facts right
  (not via Claude), and how many it gets wrong (owner's request 2026-10-16).
Speech, the same clips for every engine: Whisper (speech to text, auto and English-only), Kokoro and
Piper (time to the first audio), Smart Turn (endpointing time; complete vs cut-off sentences), and
Kyutai's streaming STT + TTS when its lab copy is installed.

The pick: among models that call the tool reliably (recall >= 90%, false calls <= 15%), keep every
fact (>= 90%), answer the card's questions on the PC (>= 70%, wrong <= 10%) and sound natural (Claude:
>= 3.5 English, >= 3 German), the fastest to its first spoken word. The result is kept (Run diagnosis shows it) and the picked model talks from then on.

On the GPU (owner's report 2026-10-19): the report says whether the run was made on the GPU (every model's
layers and Whisper/Kokoro on it). A run off the GPU never switches the local voice off; Kairo runs the lab
again by itself once the voice is on the GPU (main.py).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import numpy as np                                  # comes with the voice packages
except ImportError:                                     # the app runs without them: no speech part
    np = None
from sqlmodel import Session

from . import bringup, gpu, voicellm
from .brain.voice_local import PORT, VoiceModel
from .db import SettingKV
from .latency import pct
from .voicemouth import FILLERS, Mouth, g3_options, mouth_args

log = logging.getLogger("kairo.voicelab")

LAST_KEY = "voicelab.last"
STATE_KEY = "voicelab.state"
PAD_KEY = "voice.smart_turn_pad"
TOOL_RECALL_MIN, FALSE_CALLS_MAX, FIDELITY_MIN = 0.9, 0.15, 0.9
CARD_MIN, CARD_WRONG_MAX = 0.7, 0.1
NATURAL_MIN = {"en": 3.5, "de": 3.0}


@dataclass
class Case:
    text: str
    answer: str | None = None              # Claude's answer when the tool must be called
    facts: tuple[tuple[str, ...], ...] = ()  # each fact: any of these must be in what is said
    card: bool = False                     # the Today card has the answer: no Claude needed


CASES = {
    "en": [
        Case("Hey, how's it going?"),
        Case("What's on my calendar tomorrow?", "Tomorrow: school from 8:00 to 15:30, then training at 18:00.",
             (("8",), ("15:30", "3:30", "half past three"), ("18", "6"))),
        Case("Haha okay, that's funny."),
        Case("Remind me to call Lea at five.", "Done: a reminder at 17:00 to call Lea.", (("5", "17", "five"), ("Lea",))),
        Case("Did Tom answer my email?", "Yes, Tom replied this morning: he's in for Saturday.", (("Tom",), ("Saturday",))),
        Case("Wait, give me a second."),
        Case("Add milk to my shopping list.", "Milk is on the shopping list.", (("milk",),)),
        Case("When is my next maths test?", "Your next maths test is on Tuesday at 10:15.",
             (("Tuesday",), ("10:15", "10.15", "quarter past ten"))),
        Case("Thanks, that's all for now."),
    ],
    "de": [
        Case("Hey, wie geht's?"),
        Case("Was steht morgen in meinem Kalender?", "Morgen: Schule von 8 bis 15:30, danach Training um 18 Uhr.",
             (("8", "acht"), ("15:30", "15.30", "halb vier"), ("18", "sechs"))),
        Case("Haha, okay, lustig."),
        Case("Erinnere mich, um fünf Lea anzurufen.", "Erledigt: Erinnerung um 17 Uhr, Lea anrufen.", (("17", "fünf", "5"), ("Lea",))),
        Case("Hat Tom auf meine Mail geantwortet?", "Ja, Tom hat heute Morgen geantwortet: er ist am Samstag dabei.",
             (("Tom",), ("Samstag",))),
        Case("Moment, kurz warten."),
        Case("Schreib Milch auf die Einkaufsliste.", "Milch steht auf der Einkaufsliste.", (("Milch",),)),
        Case("Wann ist meine nächste Mathe-Prüfung?", "Deine nächste Mathe-Prüfung ist am Dienstag um 10:15.",
             (("Dienstag",), ("10:15", "10.15", "viertel nach zehn"))),
        Case("Danke, das war's."),
    ],
}
LAB_CARD = {"now": "Thursday 16 October 2026; the owner is at Home",
            "events": "- Thu 18:00–19:30 Training (Gym)\n- Fri 08:00–15:30 School\n- Fri 19:00 Cinema with Noah (Kino Rex)",
            "tasks": "- buy a bike lock by 17:00\n- pay the phone bill (overdue)",
            "loops": "- reply: Lea asks if Friday works (Lea)\n- promise: send Noah the photos (Noah)",
            "weather": "Bern: now 12°C, cloudy; today 8°C to 15°C, cloudy, rain 30%",
            "trip": "Home to School:\n- 07:12, S3, platform 2, arrives 07:31\n- 07:42, S3, platform 2, arrives 08:01",
            "about": "Sam, apprentice, trains Tuesdays and Thursdays."}
LAB_ITEMS = [bringup.Item("L1", "loop", "reply: Lea asks if Friday works", "normal")]

SPEECH = {
    "en": ["Remind me to call the dentist tomorrow.", "What's on my calendar on Friday?",
           "I think I'll go for a run after school today."],
    "de": ["Erinnere mich morgen an den Zahnarzt.", "Was steht am Freitag in meinem Kalender?",
           "Ich glaube, ich gehe heute nach der Schule joggen."],
}
FIRST_CLAUSE = {"en": "Sure, one sec.", "de": "Klar, Moment."}
# The Today card's questions (owner's request 2026-10-16: 20 per language): (question, the facts that must be said)
CARD_CASES = {
    "en": [("What's the weather like today?", (("12", "twelve"),)), ("How warm does it get today?", (("15", "fifteen"),)),
           ("Is it going to rain?", (("30",),)), ("What's the lowest temperature today?", (("8", "eight"),)),
           ("When's my next train to school?", (("7:12", "07:12", "twelve past seven"),)),
           ("Which platform is my train to school on?", (("2", "two"),)),
           ("When do I get to school on the first train?", (("7:31", "07:31"),)),
           ("If I miss the 7:12, when's the next one?", (("7:42", "07:42"),)),
           ("What's next today?", (("18", "six"), ("training",))), ("Where is training tonight?", (("gym",),)),
           ("When does training end?", (("19:30", "half past seven", "7:30"),)),
           ("When does school start tomorrow?", (("8", "eight"),)),
           ("When does school finish tomorrow?", (("15:30", "half past three", "3:30"),)),
           ("What am I doing Friday evening?", (("cinema", "movie", "film"), ("Noah",))),
           ("Where's the cinema?", (("Rex",),)), ("What do I still have to buy today?", (("lock",),)),
           ("By when do I need the bike lock?", (("17", "five"),)), ("Is anything overdue?", (("phone bill", "bill"),)),
           ("What did Lea want?", (("Friday",),)), ("What did I promise Noah?", (("photos", "pictures"),))],
    "de": [("Wie ist das Wetter heute?", (("12", "zwölf"),)), ("Wie warm wird es heute?", (("15", "fünfzehn"),)),
           ("Regnet es heute?", (("30",),)), ("Was ist heute die tiefste Temperatur?", (("8", "acht"),)),
           ("Wann fährt mein nächster Zug zur Schule?", (("7:12", "07:12", "zwölf nach sieben"),)),
           ("Auf welchem Gleis fährt mein Zug zur Schule?", (("2", "zwei"),)),
           ("Wann bin ich mit dem ersten Zug in der Schule?", (("7:31", "07:31"),)),
           ("Wenn ich den Zug um 7:12 verpasse, wann fährt der nächste?", (("7:42", "07:42"),)),
           ("Was steht heute als nächstes an?", (("18", "sechs"), ("training",))),
           ("Wo ist das Training heute Abend?", (("gym",),)),
           ("Wann ist das Training fertig?", (("19:30", "halb acht"),)),
           ("Wann beginnt morgen die Schule?", (("8", "acht"),)),
           ("Wann ist morgen Schulschluss?", (("15:30", "halb vier"),)),
           ("Was mache ich am Freitagabend?", (("kino", "cinema", "film"), ("Noah",))),
           ("Wo ist das Kino?", (("Rex",),)), ("Was muss ich heute noch kaufen?", (("lock", "schloss"),)),
           ("Bis wann brauche ich das Velo-Schloss?", (("17", "fünf"),)),
           ("Ist etwas überfällig?", (("phone bill", "bill", "rechnung"),)),
           ("Was wollte Lea?", (("Friday", "Freitag"),)), ("Was habe ich Noah versprochen?", (("photos", "fotos", "bilder"),))],
}
for _lang, _rows in CARD_CASES.items():          # after the first small talk, before the goodbye
    CASES[_lang][1:1] = [Case(q, "; ".join(f[0] for f in facts), facts, card=True) for q, facts in _rows]

ANSWER = {"en": "Tomorrow you've got school from eight, then training at six.",
          "de": "Morgen hast du ab acht Schule und danach um sechs Training."}


class _Ear:
    """A speaker that only listens (the lab measures text, not audio)."""

    def __init__(self) -> None:
        self.text, self.said = "", []

    def feed(self, piece: str) -> None:
        self.text += piece

    def say(self, text: str) -> None:
        self.said.append(text)


def nvidia_smi() -> dict | None:
    """{used_mb, total_mb, name, driver} of the first GPU, or None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used,memory.total,name,driver_version", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        used, total, name, driver = [x.strip() for x in out[0].split(",")]
        return {"used_mb": int(used), "total_mb": int(total), "name": name, "driver": driver}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def has_facts(text: str, facts) -> bool:
    low = text.lower()
    return all(any(alt.lower() in low for alt in fact) for fact in facts)


def wer(ref: str, hyp: str) -> float:
    r, h = re.findall(r"[\wäöüß']+", ref.lower()), re.findall(r"[\wäöüß']+", hyp.lower())
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return round(d[len(h)] / max(1, len(r)), 3)


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def wav_samples(wav: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        return data, w.getframerate()


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def stats(values: list) -> dict:
    v = [x for x in values if isinstance(x, (int, float))]
    return {"p50": pct(v, .5), "p95": pct(v, .95), "n": len(v)}


# ---------------------------------------------------------------- the language models

# What must never be said aloud (G3's gate, "markup spoken 0 of N"): a model's tool-call or reasoning markers that got through
# the sanitiser (brain/voice_local.TextCalls) into the words.
_MARKUP = re.compile(r"</?think>|<\|?/?tool_(?:call|response)|\btool_(?:call|response)\|>|\bcall:[A-Za-z_]\w*\s*\{|\[TOOL_CALLS\]", re.I)


def spoke_markup(text: str | None) -> bool:
    return bool(text and _MARKUP.search(text))


async def _run_model(vm: VoiceModel, temperature: float, layout: dict | None = None) -> dict:
    out = {"ttft": [], "first_text": [], "tok_s": [], "cold_ms": {}, "need": 0, "hits": 0, "small": 0, "false": 0,
           "facts_n": 0, "facts_ok": 0, "card_n": 0, "card_pc": 0, "card_wrong": 0, "card_claude": 0,
           "replies": {"en": [], "de": []}, "errors": [], "spoken_n": 0, "markup": 0}
    for lang, cases in CASES.items():
        answers = {c.text: c.answer for c in cases}

        async def brain(request, lines, about, _answers=answers, _lang=lang):
            for text, ans in _answers.items():
                if ans and (text.lower() in request.lower() or request.lower() in text.lower()):
                    return ans, False
            last = next((ln[7:] for ln in reversed(lines) if ln.startswith("Owner: ")), "")
            return _answers.get(last) or ("Done." if _lang == "en" else "Erledigt."), False
        mouth = Mouth(vm, ask=brain, context=dict(LAB_CARD), items=LAB_ITEMS, name="Sam", temperature=temperature,
                      ask_timeout_s=120,     # the lab scores Claude's answers: no call cap here
                      clock=lambda: dt.datetime(2026, 10, 16, 6, 55), **(layout or {}))
        for i, case in enumerate(cases):
            ear = _Ear()
            try:
                r = await asyncio.wait_for(mouth.turn(case.text, lang, ear), 90)
            except Exception as exc:   # a model that hangs or errors is measured as failing
                out["errors"].append(f"{lang} {case.text!r}: {type(exc).__name__}: {exc}"[:200])
                continue
            if i == 0:
                out["cold_ms"][lang] = r.first_token_ms
            else:
                out["ttft"].append(r.first_token_ms)
                out["first_text"].append(r.first_text_ms)
            if r.timings.get("predicted_per_second"):
                out["tok_s"].append(round(float(r.timings["predicted_per_second"]), 1))
            spoken = ear.text.strip()
            out["spoken_n"] += 1
            out["markup"] += 1 if spoke_markup(spoken) else 0
            if case.card:                      # the card has it: on the PC, right, at once
                out["card_n"] += 1
                if r.asked:
                    out["card_claude"] += 1
                elif has_facts(spoken, case.facts):
                    out["card_pc"] += 1
                else:
                    out["card_wrong"] += 1
            elif case.answer:
                out["need"] += 1
                out["hits"] += 1 if r.asked else 0
                out["facts_n"] += 1
                out["facts_ok"] += 1 if r.asked and has_facts(spoken, case.facts) else 0
            else:
                out["small"] += 1
                out["false"] += 1 if r.asked else 0
            out["replies"][lang].append({"owner": case.text, "kairo": spoken, "filler": ear.said[0] if ear.said else None})
    return out


def _summary(raw: dict) -> dict:
    return {"first_token_ms": stats(raw["ttft"]), "first_word_ms": stats(raw["first_text"]), "cold_first_token_ms": raw["cold_ms"],
            "tokens_per_s": round(sum(raw["tok_s"]) / len(raw["tok_s"]), 1) if raw["tok_s"] else None,
            "tool_recall": round(raw["hits"] / raw["need"], 2) if raw["need"] else None,
            "false_calls": round(raw["false"] / raw["small"], 2) if raw["small"] else None,
            "fidelity": round(raw["facts_ok"] / raw["facts_n"], 2) if raw["facts_n"] else None,
            "card_on_pc": round(raw["card_pc"] / raw["card_n"], 2) if raw["card_n"] else None,
            "card_wrong": round(raw["card_wrong"] / raw["card_n"], 2) if raw["card_n"] else None,
            "card_via_claude": round(raw["card_claude"] / raw["card_n"], 2) if raw["card_n"] else None,
            "markup_spoken": raw.get("markup", 0), "spoken_replies": raw.get("spoken_n", 0),
            "errors": raw["errors"][:5]}


JUDGE = """You are judging voice assistants for a phone-call app. Each one answered the same owner lines
(English and German). Rate each assistant per language, 1 to 5: how natural, warm and short it sounds
SPOKEN on the phone (5 = like a real person), in the right language, keeping the facts it was given.
Be strict: robotic, long, list-like, wrong-language or invented answers score low.

Answer with JSON only: {{"A": {{"en": 4.0, "de": 3.5, "note": "one short line"}}, ...}}

{replies}"""


def judge_prompt(labeled: dict[str, dict]) -> str:
    parts = []
    for label, replies in labeled.items():
        lines = [f"## Assistant {label}"]
        for lang in ("en", "de"):
            for r in replies.get(lang, []):
                lines.append(f"[{lang}] Owner: {r['owner']}\n[{lang}] Assistant: {r['kairo'] or '(silence)'}")
        parts.append("\n".join(lines))
    return JUDGE.format(replies="\n\n".join(parts))


def parse_judge(text: str, labels: list[str]) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    out = {}
    for label in labels:
        v = data.get(label) if isinstance(data, dict) else None
        if isinstance(v, dict):
            out[label] = {k: (float(v[k]) if isinstance(v.get(k), (int, float)) else None) for k in ("en", "de")}
            out[label]["note"] = str(v.get("note") or "")[:200]
    return out


def pick(models: dict, language_mode: str = "auto", prefer: str | None = None, refuse_markup: bool = False) -> tuple[str | None, str]:
    """The fastest model that passes; with `prefer` (g3.gemma_first), that one if it passes. (id, why).
    `refuse_markup` (also g3.gemma_first): a model that spoke tool-call markup does not pass. Off, the count is only reported:
    leaving a model out moves the live voice model, which is the owner's Gemma decision (recipe 16.6), not a side effect."""
    langs = ("en",) if language_mode == "en" else ("en", "de")
    ok, why_not = [], []
    for mid, m in models.items():
        if m.get("error"):
            why_not.append(f"{mid}: {m['error']}")
            continue
        problems = []
        if (m.get("tool_recall") or 0) < TOOL_RECALL_MIN:
            problems.append(f"calls the tool {m.get('tool_recall')}")
        if (m.get("false_calls") or 0) > FALSE_CALLS_MAX:
            problems.append(f"false calls {m.get('false_calls')}")
        if (m.get("fidelity") or 0) < FIDELITY_MIN:
            problems.append(f"facts kept {m.get('fidelity')}")
        if m.get("card_on_pc") is not None and m["card_on_pc"] < CARD_MIN:
            problems.append(f"answers from the card {m['card_on_pc']}")
        if m.get("card_wrong") is not None and m["card_wrong"] > CARD_WRONG_MAX:
            problems.append(f"wrong from the card {m['card_wrong']}")
        if refuse_markup and (m.get("markup_spoken") or 0) > 0:
            problems.append(f"spoke tool-call markup in {m['markup_spoken']} of {m.get('spoken_replies')} replies")
        for lang in langs:
            score = (m.get("natural") or {}).get(lang)
            if score is not None and score < NATURAL_MIN[lang]:
                problems.append(f"natural ({lang}) {score}")
        speed = (m.get("first_word_ms") or {}).get("p50")
        if speed is None:
            problems.append("no timing")
        if problems:
            why_not.append(f"{mid}: " + ", ".join(problems))
        else:
            ok.append((speed, mid))
    passing = {mid: speed for speed, mid in ok}
    if prefer in passing:
        faster = [f"{mid} {speed} ms" for speed, mid in sorted(ok) if mid != prefer and speed < passing[prefer]]
        return prefer, (f"preferred, and it passes ({passing[prefer]} ms to its first word)"
                        + (f"; faster but not picked: {', '.join(faster)}" if faster else "")
                        + (f"; not picked: {'; '.join(why_not)}" if why_not else ""))
    if ok:
        speed, mid = min(ok)
        return mid, f"fastest that passes ({speed} ms to its first word)" + (f"; not picked: {'; '.join(why_not)}" if why_not else "")
    return None, "no model passed: " + "; ".join(why_not)


# ---------------------------------------------------------------- the lab

class Lab:
    def __init__(self, *, engine, data_dir: Path, site_dir: Path, voice_model: VoiceModel, voice=None,
                 smart_turn=None, judge: Callable[[str], str | None] | None = None,
                 tools_dir: Path | None = None, now: Callable[[], dt.datetime] | None = None,
                 server_prefix: Callable[[Path], list[str]] | None = None, port: int = PORT):
        self.engine, self.data_dir, self.site_dir = engine, Path(data_dir), Path(site_dir)
        self.vm, self.voice, self.smart_turn, self.judge = voice_model, voice, smart_turn, judge
        self.tools_dir = Path(tools_dir) if tools_dir else self.data_dir.parent / "tools"
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.server_prefix = server_prefix or (lambda binary: [str(binary)])
        self.port = port

    def _state(self, **kw) -> None:
        with Session(self.engine) as s:
            _put_state(s, {**kw, "at": self.now().isoformat(timespec="seconds")})

    def _options(self) -> dict:
        """The owner's G3 switches: the lab measures with the same server flags the real server gets."""
        with Session(self.engine) as s:
            return g3_options(s)

    def _prefer(self) -> str | None:
        """g3.gemma_first, read once per run."""
        with Session(self.engine) as s:
            return voicellm.preferred(s)

    def run(self, language_mode: str = "auto", by: str = "owner") -> dict:
        """by: "owner" (Run) or "auto" (Kairo ran it by itself)."""
        started = time.monotonic()
        report: dict = {"at": self.now().isoformat(timespec="seconds"), "gpu": nvidia_smi(), "models": {}, "speech": {},
                        "kyutai": None, "pick": None, "by": by}
        self._state(running=True, step="starting")
        self.vm.pause()                                    # the GPU is the lab's now; calls use the old pipeline
        try:
            self._models(report, language_mode)
            self._judge(report)
            self._speech(report)
            self._kyutai(report)
            report["on_gpu"], report["off_gpu"] = ran_on_gpu(report["models"], self._voice_devices())
            prefer = self._prefer()                        # g3.gemma_first: prefers Gemma, and refuses a model that spoke markup
            choice, why = pick(report["models"], language_mode, prefer=prefer, refuse_markup=prefer is not None)
            if choice is None and not report["on_gpu"]:
                why += "; measured off the GPU, so the voice stays as it was"
            report["pick"] = {"model": choice, "why": why}
            with Session(self.engine) as s:
                if choice is not None or report["on_gpu"]:   # a run off the GPU never switches the voice off
                    voicellm.set_choice(s, choice, "lab")    # none passed on the GPU: the old voice until the owner picks
                pad = (report["speech"].get("smart_turn") or {}).get("pad_side")
                if pad:
                    row = s.get(SettingKV, PAD_KEY) or SettingKV(key=PAD_KEY)
                    row.value = pad
                    s.add(row)
                    s.commit()
            report["estimate_ms"] = estimate(report)
        except Exception as exc:   # a broken lab run says why and gives the GPU back
            log.exception("voice lab")
            report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            report["seconds"] = int(time.monotonic() - started)
            with Session(self.engine) as s:
                row = s.get(SettingKV, LAST_KEY) or SettingKV(key=LAST_KEY)
                row.value = json.dumps(report)
                s.add(row)
                s.commit()
            self._state(running=False, step="done")
            self.vm.resume()
        return report

    def _models(self, report: dict, language_mode: str) -> None:
        binary = voicellm.server_binary(self.site_dir)
        inst = voicellm.installed(self.site_dir)
        for s_ in inst.get("skipped") or []:
            if s_.get("model") in voicellm.model_ids():
                report["models"][s_["model"]] = {"title": (voicellm.spec(s_["model"]) or {}).get("title"),
                                                 "error": f"not installed: {s_.get('why')}"}
        for mid in voicellm.available(self.site_dir):
            spec = voicellm.spec(mid) or {}
            self._state(running=True, step=f"model {spec.get('title', mid)}")
            entry = {"title": spec.get("title"), "quant": (inst["models"][mid] or {}).get("quant")}
            path = voicellm.model_path(self.site_dir, mid)
            before = nvidia_smi()
            vm = VoiceModel(lambda p=path, m=mid: (self.server_prefix(binary), p, m, int(spec.get("context", 8192))),
                            log_dir=self.data_dir / "logs", port=self.port, options=self._options)
            t0 = time.monotonic()
            try:
                if not vm.wait_ready():
                    entry["error"] = vm.status().get("error") or "did not start"
                    report["models"][mid] = entry
                    continue
                entry["load_s"] = round(time.monotonic() - t0, 1)
                entry["device"] = {k: v for k, v in gpu.llama_device(vm.log_dir / "voice-llm.log").items()
                                   if k in ("device", "layers", "ok", "reason")}   # llama.cpp's own log: GPU or CPU
                after = nvidia_smi()
                if before and after:
                    entry["vram_mb"] = after["used_mb"] - before["used_mb"]
                raw = asyncio.run(_run_model(vm, float(spec.get("temperature", 0.6)), mouth_args(self._options())))
                entry.update(_summary(raw))
                entry["_replies"] = raw["replies"]
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                vm.shutdown()
            report["models"][mid] = entry

    def _voice_devices(self) -> dict:
        """Where Whisper and Kokoro ran (the speech part loaded them), {} when the voice can't say."""
        try:
            return self.voice.devices() if self.voice is not None and hasattr(self.voice, "devices") else {}
        except Exception:   # the check never breaks a run
            log.exception("voice lab: where the voice runs")
            return {}

    def _judge(self, report: dict) -> None:
        live = {mid: m for mid, m in report["models"].items() if m.get("_replies")}
        if not live:
            return
        mids = list(live)
        random.Random(report["at"]).shuffle(mids)          # blind: Claude sees A, B, C
        labels = {chr(65 + i): mid for i, mid in enumerate(mids)}
        self._state(running=True, step="Claude rates the replies")
        text = None
        if self.judge:
            try:
                text = self.judge(judge_prompt({lab: live[mid]["_replies"] for lab, mid in labels.items()}))
            except Exception as exc:
                report["judge_error"] = f"{type(exc).__name__}: {exc}"[:200]
        scores = parse_judge(text or "", list(labels))
        for lab, mid in labels.items():
            m = report["models"][mid]
            m["natural"] = scores.get(lab) or {"en": None, "de": None, "note": "not rated"}
            m["samples"] = {lang: m["_replies"][lang][1:3] for lang in m["_replies"]}
        for m in report["models"].values():
            m.pop("_replies", None)

    def _speech(self, report: dict) -> None:
        if self.voice is None:
            return
        if np is None:
            report["speech"] = {"error": "the voice packages are not installed"}
            return
        self._state(running=True, step="speech: Whisper, Kokoro, Piper, Smart Turn")
        sp: dict = {"stt": {}, "tts": {}}
        clips = []
        for lang, sentences in SPEECH.items():
            for text in sentences:
                try:
                    wav, used = self.voice.speak(text, lang)
                    x, rate = wav_samples(wav)
                    clips.append((lang, text, resample(x, rate, 16000)))
                except Exception as exc:
                    sp.setdefault("errors", []).append(f"tts {lang}: {type(exc).__name__}: {exc}"[:160])
        report["_clips"] = clips
        for mode in ("auto", "en"):
            rows = []
            for lang, text, x in clips:
                if mode == "en" and lang != "en":
                    continue
                pcm = to_pcm16(np.concatenate([x, np.zeros(3200, np.float32)]))
                t = time.monotonic()
                try:
                    tr = self.voice.transcribe_pcm(pcm, language="en" if mode == "en" else None)
                    rows.append({"ms": int((time.monotonic() - t) * 1000), "wer": wer(text, tr.text), "lang": lang})
                except Exception as exc:
                    sp.setdefault("errors", []).append(f"stt: {type(exc).__name__}: {exc}"[:160])
            sp["stt"][mode] = {"ms": stats([r["ms"] for r in rows]),
                               "wer": round(sum(r["wer"] for r in rows) / len(rows), 3) if rows else None}
        for lang in ("en", "de"):
            first, full = [], []
            for _ in range(3):
                for text, bucket in ((FIRST_CLAUSE[lang], first), (ANSWER[lang], full)):
                    t = time.monotonic()
                    try:
                        self.voice.speak(text, lang)
                        bucket.append(int((time.monotonic() - t) * 1000))
                    except Exception:
                        pass
            sp["tts"][lang] = {"first_clause_ms": stats(first), "sentence_ms": stats(full)}
        if self.smart_turn is not None and self.smart_turn.available() and clips:
            sp["smart_turn"] = smart_turn_check(self.smart_turn, clips)
        report["speech"] = sp

    def _kyutai(self, report: dict) -> None:
        clips = report.pop("_clips", [])
        kdir = voicellm.kyutai_dir(self.tools_dir)
        if not voicellm.kyutai_ready(self.tools_dir):
            report["kyutai"] = {"state": "not installed", "how": "More -> System -> Voice lab -> Install the Kyutai test"}
            return
        py = kdir / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        script = Path(__file__).resolve().parents[1] / "tools" / "kyutai_bench.py"
        self._state(running=True, step="Kyutai streaming STT and TTS")
        with tempfile.TemporaryDirectory() as tmp:
            items = []
            for i, (lang, text, x) in enumerate(c for c in clips if c[0] == "en"):   # Kyutai: English and French only
                p = Path(tmp) / f"clip{i}.wav"
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(24000)
                    w.writeframes(to_pcm16(resample(x, 16000, 24000)))
                items.append({"wav": str(p), "text": text})
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps({"clips": items, "sentences": [FIRST_CLAUSE["en"], ANSWER["en"]],
                                       "kyutai": voicellm.manifest()["kyutai"]}), encoding="utf-8")
            out = Path(tmp) / "out.json"
            env = {"HF_HOME": str(kdir / "hf"), "HF_HUB_OFFLINE": "1", "NO_TORCH_COMPILE": "1",   # no secrets passed on
                   **{k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA")
                      if k in os.environ}}
            try:
                p = subprocess.run([str(py), "-I", str(script), str(job), str(out)], capture_output=True, text=True,
                                   timeout=1200, env=env)
                data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
                if p.returncode != 0 and not data:
                    data = {"error": (p.stderr or p.stdout)[-400:]}
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                data = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if data.get("stt"):
            data["stt"]["wer"] = round(sum(wer(c["text"], h) for c, h in zip(items, data["stt"].get("texts", []))) /
                                       max(1, len(items)), 3)
        report["kyutai"] = {"state": "measured" if not data.get("error") else "failed", **data}


def ran_on_gpu(models: dict, voice_devices: dict) -> tuple[bool, list[str]]:
    """(True, []) when the run was made on the GPU: the PC has an NVIDIA card, every model that started had all
    its layers on it, and Whisper and Kokoro ran on it. Else (False, why)."""
    if not gpu.present():
        return False, ["no NVIDIA driver on this PC"]
    off = []
    measured = {mid: m["device"] for mid, m in models.items() if m.get("device")}
    if not measured:
        off.append("no voice model started")
    for mid, d in measured.items():
        if not d.get("ok"):
            off.append(f"{mid}: {d.get('reason') or 'not on the GPU'}"[:200])
    for name, c in (voice_devices or {}).items():
        if name in ("whisper", "kokoro") and c.get("loaded") and not c.get("ok"):
            off.append(f"{name}: {c.get('reason') or 'on the CPU'}"[:200])
    return not off, off


def smart_turn_check(st, clips) -> dict:
    """Complete sentences should score high, the same audio cut mid-sentence low. Both pad sides tried."""
    out = {}
    for side in ("right", "left"):
        st.pad_side = side
        full, cut, ms = [], [], []
        for _lang, _text, x in clips:
            for bucket, audio in ((full, x), (cut, x[: int(len(x) * 0.55)])):
                p = st.probability(to_pcm16(audio))
                if p is not None:
                    bucket.append(p)
                    ms.append(st.last_ms)
        if full and cut:
            out[side] = {"complete": round(sum(full) / len(full), 2), "cut": round(sum(cut) / len(cut), 2),
                         "accuracy": round((sum(p >= 0.5 for p in full) + sum(p < 0.5 for p in cut)) / (len(full) + len(cut)), 2),
                         "ms": stats(ms)}
    if not out:
        return {"error": st.failed or "no result"}
    best = max(out, key=lambda k: (out[k]["accuracy"], out[k]["complete"] - out[k]["cut"]))
    st.pad_side = best
    return {**out, "pad_side": best}


def estimate(report: dict) -> dict:
    """On the PC, before the phone: quiet before the turn ends + speech to text + the voice model's
    first word + its first audio. The phone adds the network and starting playback (Voice speed)."""
    sp = report.get("speech") or {}
    st = sp.get("smart_turn") or {}
    endpoint = 200 + ((st.get(st.get("pad_side"), {}) or {}).get("ms") or {}).get("p50", 0) if st.get("pad_side") else 500
    stt = ((sp.get("stt") or {}).get("auto") or {}).get("ms", {}).get("p50")
    tts = ((sp.get("tts") or {}).get("en") or {}).get("first_clause_ms", {}).get("p50")
    picked = (report.get("pick") or {}).get("model")
    llm = ((report["models"].get(picked) or {}).get("first_word_ms") or {}).get("p50") if picked else None
    out = {"endpoint": endpoint, "stt": stt, "voice_model_first_word": llm, "first_audio": tts}
    if all(isinstance(v, (int, float)) for v in out.values()):
        out["total"] = sum(out.values())
    k = report.get("kyutai") or {}
    if k.get("state") == "measured" and llm is not None:
        kt = k.get("stt", {}).get("after_end_ms", {}).get("p50")
        kf = k.get("tts", {}).get("first_audio_ms", {}).get("p50")
        if kt is not None and kf is not None:
            out["kyutai_total"] = endpoint + kt + llm + kf
    return out


def last(s: Session) -> dict:
    row = s.get(SettingKV, LAST_KEY)
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


def state(s: Session) -> dict:
    row = s.get(SettingKV, STATE_KEY)
    try:
        return json.loads(row.value) if row and row.value else {"running": False}
    except ValueError:
        return {"running": False}


def _put_state(s: Session, value: dict) -> None:
    row = s.get(SettingKV, STATE_KEY) or SettingKV(key=STATE_KEY)
    row.value = json.dumps(value)
    s.add(row)
    s.commit()


def clear_stale(s: Session) -> bool:
    """Kairo just started, so no lab can be running: a run cut off by a restart left running=True behind,
    which blocked Run (409) and the run by itself for good. True when one was cleared."""
    st = state(s)
    if not st.get("running"):
        return False
    _put_state(s, {"running": False, "step": f"cut off by a restart ({st.get('step')})", "at": st.get("at")})
    return True


def set_waiting(s: Session, why: str | None) -> None:
    """Why the lab hasn't run by itself yet (Voice lab page, Run diagnosis); None: not waiting.
    Never written over a run in progress."""
    st = state(s)
    if st.get("running") or st.get("waiting") == why:
        return
    st.pop("waiting", None)
    st.pop("waiting_since", None)
    if why:
        st.update(waiting=why, waiting_since=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    _put_state(s, st)


def filler_words() -> set[str]:
    return {f for fs in FILLERS.values() for f in fs}
