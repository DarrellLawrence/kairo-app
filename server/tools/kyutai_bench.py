"""Kyutai streaming speech to text + text to speech on this PC, for Kairo's voice lab (2026-10-16).

Runs in the Kyutai lab copy's own Python (tools\\kyutai-<hash>\\venv: torch + moshi), never Kairo's.
Kyutai's models are English and French only. Written against moshi 0.2.13 (run_inference.py, models/tts.py).

  python -I kyutai_bench.py download <kyutai dir> <voicellm.json>                     (the installer)
  python -I kyutai_bench.py <job.json> <out.json>                                     (the voice lab)

STT: each clip is fed frame by frame (80 ms) as fast as the GPU goes, then the model's delay is
flushed with silence; after_end_ms = the time from the clip's last frame to the final words (what the
owner waits for at the end of a turn). ms_per_frame must stay under 80 for live use.
TTS: time to the first audible audio of a sentence, and the real-time factor.
"""

import json
import os
import sys
import time


def download(kdir, k):
    os.environ["HF_HOME"] = os.path.join(kdir, "hf")
    from huggingface_hub import snapshot_download
    for repo in (k["stt_repo"], k["tts_repo"]):
        snapshot_download(repo)
    snapshot_download(k["voice_repo"], allow_patterns=[k["voice"] + "*"])
    print("kyutai models ready")


def _sync(torch):
    if torch.cuda.is_available():
        torch.cuda.synchronize()


def stt(job, out, torch, dev):
    import sphn
    from moshi.models import LMGen, loaders
    k = job["kyutai"]
    info = loaders.CheckpointInfo.from_hf_repo(k["stt_repo"])
    mimi = info.get_mimi(device=dev)
    tok = info.get_text_tokenizer()
    lm = info.get_moshi(device=dev, dtype=torch.bfloat16)
    gen = LMGen(lm, **info.lm_gen_config)
    frame = int(mimi.sample_rate / mimi.frame_rate)
    cfg = info.stt_config or {}
    delay_s = float(cfg.get("audio_delay_seconds", 0.5))
    prefix = int(float(cfg.get("audio_silence_prefix_seconds", 0.0)) * mimi.sample_rate)
    after, per_frame, texts = [], [], []
    mimi.streaming_forever(1)
    gen.streaming_forever(1)
    for clip in job["clips"]:
        pcm, _ = sphn.read(clip["wav"], sample_rate=mimi.sample_rate)
        x = torch.from_numpy(pcm[0:1]).to(dev)[None]
        x = torch.nn.functional.pad(x, (prefix, 0))
        tail = torch.zeros(1, 1, int((delay_s + 0.25) * mimi.sample_rate), device=dev)
        words = []
        mimi.reset_streaming()
        gen.reset_streaming()
        first = True

        def step(chunk):
            nonlocal first
            codes = mimi.encode(chunk)
            if first:
                gen.step(codes)
                first = False
            tokens = gen.step(codes)
            if tokens is not None:
                t = tokens[0, 0, 0].item()
                if t not in (0, 3):
                    words.append(tok.id_to_piece(t).replace("▁", " "))
        with torch.no_grad():
            chunks = [c for c in x.split(frame, dim=2) if c.shape[-1] == frame]
            t0 = time.monotonic()
            for c in chunks:
                step(c)
            _sync(torch)
            per_frame.append((time.monotonic() - t0) * 1000 / max(1, len(chunks)))
            t_end = time.monotonic()
            for c in tail.split(frame, dim=2):
                if c.shape[-1] == frame:
                    step(c)
            _sync(torch)
            after.append((time.monotonic() - t_end) * 1000)
        texts.append("".join(words).strip())
    out["stt"] = {"after_end_ms": _stats(after), "ms_per_frame": _stats(per_frame), "texts": texts,
                  "delay_s": delay_s, "frame_ms": 1000 * frame / mimi.sample_rate}


def tts(job, out, torch, dev):
    from moshi.models import loaders
    from moshi.models.tts import TTSModel
    k = job["kyutai"]
    info = loaders.CheckpointInfo.from_hf_repo(k["tts_repo"])
    model = TTSModel.from_checkpoint_info(info, n_q=32, temp=0.6, device=dev)
    voice = model.get_voice_path(k["voice"])
    cond = model.make_condition_attributes([voice], cfg_coef=2.0)
    firsts, rtfs = [], []
    for _ in range(2):                                   # the first round warms up
        for text in job["sentences"]:
            entries = model.prepare_script([text], padding_between=1)
            first, samples = [None], [0]
            t0 = time.monotonic()
            with model.mimi.streaming(1), torch.no_grad():
                def on_frame(fr):
                    if (fr != -1).all():
                        pcm = model.mimi.decode(fr[:, 1:, :])
                        samples[0] += pcm.shape[-1]
                        if first[0] is None and float(pcm.abs().max()) > 0.01:
                            _sync(torch)
                            first[0] = (time.monotonic() - t0) * 1000
                model.generate([entries], [cond], on_frame=on_frame)
            took = time.monotonic() - t0
            firsts.append(first[0])
            rtfs.append(took / max(1e-6, samples[0] / model.mimi.sample_rate))
    half = len(firsts) // 2
    out["tts"] = {"first_audio_ms": _stats(firsts[half:]), "rtf": round(sum(rtfs[half:]) / max(1, len(rtfs) - half), 2)}


def _stats(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return {"p50": None, "p95": None, "n": 0}
    return {"p50": round(v[len(v) // 2]), "p95": round(v[min(len(v) - 1, int(len(v) * 0.95))]), "n": len(v)}


def main(argv):
    if argv[:1] == ["download"]:
        download(argv[1], json.load(open(argv[2], encoding="utf-8"))["kyutai"])
        return 0
    job = json.load(open(argv[0], encoding="utf-8"))
    out = {"errors": []}
    try:
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        out["device"] = dev
        for part in (stt, tts):
            try:
                part(job, out, torch, dev)
            except Exception as exc:   # one part failing still reports the other
                out["errors"].append(f"{part.__name__}: {type(exc).__name__}: {exc}"[:300])
            if dev == "cuda":
                out["vram_mb"] = max(out.get("vram_mb") or 0, int(torch.cuda.max_memory_allocated() / 2 ** 20))
                torch.cuda.empty_cache()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    if out["errors"] and not out.get("stt") and not out.get("tts"):
        out["error"] = "; ".join(out["errors"])
    json.dump(out, open(argv[1], "w", encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
