"""One encoder pass for finding the language and writing the words down (voice plan G2, behind the flag
g2.single_encode_langid).

Whisper with language=None encodes the audio twice: once to find the language among 99, once to decode.
The encoder is the biggest part of a decode, so every turn in "English and German" costs up to 1.6 times
what a turn with the language given costs (CPU measurement; the card's saving is P2 on the PC). Here the
audio is encoded once, English or German is read from that same encoder output, and the decoder is run on
it (faster-whisper's generate_segments reuses an encoder output it is given for the first 30 s window).

faster-whisper's own multilingual=True switch does not do this job: it finds the language per window but
does not say which, and Kairo needs the language to pick the voice. This path therefore uses
generate_segments, which is semi-internal: it is pinned to the version in requirements-voice.txt and a
test (tests/test_voice_langid.py) fails on a bump. If it ever raises, Voice falls back to Whisper's own
language=None and says so in devices().
"""

from __future__ import annotations

import dataclasses
import inspect
from types import SimpleNamespace

PINNED = "1.2.1"            # the faster-whisper version this was checked against (requirements-voice.txt)
LANGUAGES = ("en", "de")    # the only two languages Kairo hears and speaks
ACCEPT_MIN = 0.5            # English and German together must carry at least this much of the probability


def pick_language(probs: dict[str, float], prev: str | None) -> tuple[str, float, bool]:
    """(language, its probability, accepted). English or German, whichever Whisper finds likelier, but only
    taken when the two together carry at least ACCEPT_MIN: a very short clip heard as Dutch with p = 0.99 is
    not believed, and the turn keeps the last turn's language (the likelier of the two on the first turn)."""
    p_en, p_de = probs.get("en", 0.0), probs.get("de", 0.0)
    likelier = "en" if p_en >= p_de else "de"
    if p_en + p_de >= ACCEPT_MIN:
        return likelier, max(p_en, p_de), True
    if prev in LANGUAGES:
        return prev, probs.get(prev, 0.0), False
    return likelier, max(p_en, p_de), False


def options_for(tokenizer, opts: dict):
    """faster-whisper's TranscriptionOptions, built the way WhisperModel.transcribe builds them: every field
    from transcribe()'s own defaults, then opts on top."""
    from faster_whisper import WhisperModel
    from faster_whisper.transcribe import TranscriptionOptions, get_suppressed_tokens
    args = {n: p.default for n, p in inspect.signature(WhisperModel.transcribe).parameters.items()
            if p.default is not inspect.Parameter.empty}
    args.update(opts)
    temperature = args["temperature"]
    args["temperatures"] = list(temperature) if isinstance(temperature, (list, tuple)) else [temperature]
    if args["suppress_tokens"]:
        args["suppress_tokens"] = get_suppressed_tokens(tokenizer, args["suppress_tokens"])
    wanted = {f.name for f in dataclasses.fields(TranscriptionOptions)}
    missing = wanted - set(args)
    if missing:
        raise RuntimeError(f"faster-whisper's options changed: {sorted(missing)}")
    return TranscriptionOptions(**{n: args[n] for n in wanted})


def transcribe_en_de(model, audio, *, prev_language: str | None = None, vad_filter: bool = True, **opts):
    """(segments, info) like WhisperModel.transcribe, in English or German found from the same encoder pass
    that writes the words. audio: 16 kHz float32. info has language, language_probability, duration and
    accepted (False: Whisper was not sure of English or German and the previous language was used)."""
    import numpy as np
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps

    fe = model.feature_extractor
    info = SimpleNamespace(language=prev_language or "en", language_probability=0.0, accepted=False,
                           duration=audio.shape[0] / fe.sampling_rate)
    if vad_filter:                       # as transcribe() does: only the speech goes in
        audio = np.concatenate(collect_chunks(audio, get_speech_timestamps(audio, VadOptions()))[0], axis=0)
    features = fe(audio)
    content_frames = features.shape[-1] - 1
    if content_frames <= 0:              # nothing but quiet: no words, and no encoder pass either
        return [], info
    encoded = model.encode(pad_or_trim(features[:, :min(fe.nb_max_frames, content_frames)]))
    ranked = model.model.detect_language(encoded)[0]
    info.language, info.language_probability, info.accepted = pick_language(
        {token[2:-2]: p for token, p in ranked}, prev_language)
    tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe", language=info.language)
    segments = list(model.generate_segments(features, tokenizer, options_for(tokenizer, opts), False, encoded))
    return segments, info
