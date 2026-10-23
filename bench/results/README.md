# Voice latency results

One JSON per run of the voice latency harness (`server/app/bench_voice.py`): how long after you stop talking does
Kairo's voice start, step by step, on the real call pipeline. Nothing here is committed except this file and
`.gitkeep`. A run writes to `<data folder>\bench\results\` (Kairo's data folder: `C:\Kairo\data` on the PC, `devdata/` in a
checkout), as `voice-<UTC time>-<live|mock>.json`.

## Run it (in `server/`)

    python -m app.bench_corpus render      # once: the clips, made with Kairo's own voices, into <data>\bench\corpus
    python -m app.bench_voice              # live: calls the running Kairo at http://127.0.0.1:$KAIRO_PORT (this PC)
    python -m app.bench_voice --mock       # no Kairo, no models, no GPU: fake speech to text / voice model / voice of known speed
    python -m app.bench_voice --clips 12 --warmup 1 --url http://127.0.0.1:8000 --out DIR --strict

From code (`kairo diag`): `from app import bench_voice` then `bench_voice.run(mock=True, url=None, out_dir=Path(...))`
returns the results document and writes it; `bench_voice.latest(results_dir)` reads the newest valid one.

A live run is a real call: the lines appear in the call's chat history and Kairo answers them (all of them are
questions and read-only requests; none asks Kairo to change anything). It refuses to start while a call looks to be under way
(a new call would replace it); `--force` overrides. It talks to Kairo's own address only (`http://127.0.0.1`, or its
`https://….ts.net`), and on the PC itself it is the owner already: no token.

Your own recordings take precedence over the rendered clips: put 16-bit WAVs named like the clip ids
(`q_en_01.wav`, see `bench/voice/corpus.json`) in `<data>\bench\own\`. Do this if the speaker check is on: it refuses
synthetic voices as "not you" (those turns are not counted as missed), and the other_speaker clips only mean something
against your own voiceprint.

## What is measured

Per clip, a fake phone (aiortc) plays it over WebRTC at real time, then silence, and listens.

- `total_ms`: the clip's true end of speech (its last voiced sample) to the first reply frame that is audible at the
  client (RMS >= 0.01 per 20 ms frame, like the phone's "playing" mark). Good to about 20 ms.
- `stages_ms`: from the server's own timing event for that turn (the numbers `app/turns.py` turns into the Voice speed steps;
  the first one after the clip began): `vad_stop` (the VAD's pause, 0.2 s), `turn_decision` (Smart Turn / the long pause, after speech to
  text), `stt_final` (Whisper, inside the pause), `llm_first_token`, `tts_first_audio` (0 when the opener came from the
  cache), `network` (the rest, up to the client). They add up to `total_ms`.
- `interrupted` (backchannel and noise clips): Kairo was talking, the clip was played over it, and the reply went silent
  because the PC's VAD heard it. `null` when Kairo's replies were too short to talk over.
- `missed`: no reply within 5 s. `speaker_accept`: the speaker check's verdict for the turn (`ignored` on the data
  channel = false), `null` when the check is off or has no voiceprint.

## The file

    {"schema": 1, "version": str, "git_sha": str, "at": ISO-8601, "mode": "live" | "mock",
     "machine": {"gpu": str|null, "vram_gb": float|null, "ram_gb": float|null, "cpu": str},
     "config": {"vad_stop_s": float, "turn_analyzer": str, "stt_model": str, "stt_compute_type": str,
                "llm_model": str|null, "tts_en": str, "tts_de": str},
     "turns": [{"clip": str, "lang": "en"|"de", "kind": str,
                "stages_ms": {"vad_stop", "turn_decision", "stt_final", "llm_first_token", "tts_first_audio", "network"},
                "total_ms": num|null, "interrupted": bool|null, "missed": bool, "speaker_accept": bool|null}],
     "summary": {"n": int, "p50_ms": num, "p90_ms": num, "p95_ms": num,
                 "by_stage": {"<stage>": {"p50": num, "p95": num}},
                 "false_interrupt_rate": num|null, "missed_turn_rate": num, "speaker_false_accept_rate": num|null,
                 "target_p95_ms": 1000, "pass": bool}}

`pass` = p95 <= 1000 ms and no missed turn and (false interrupts <= 5 % or not measured). The field names are exact
(`bench_voice.validate()` checks them; `save()` refuses a document that breaks them).

Percentiles are `app/latency.pct` (the Voice speed page's), over the turns that got a reply; `n` is how many. They are `null`
when none did. `false_interrupt_rate` is over the probes that could be made, `missed_turn_rate` over the questions,
statements and commands, `speaker_false_accept_rate` over the other_speaker clips the speaker check labelled.

## Reading it honestly

- `network` is "everything else": transport, the fake phone's jitter buffer, and the PC's VAD saying "stopped" a little
  after its 0.2 s pause (the server can't see the true end of speech). In the mock run, with no network at all, it is
  about 0.1 to 0.3 s. The fake phone is aiortc, not WebKit: the iPhone's own playout adds to what a person hears.
- A mock run measures the harness, not Kairo: its stages are the fixed waits built into the stand-ins
  (`bench_phone.MockDelays`: 120 / 150 / 80 ms) plus the real VAD and Smart Turn.
- A synthetic voice is not you: with the speaker check on, `other_speaker` results are only meaningful when the
  question clips are your own recordings.
