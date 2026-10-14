"""The embedding model in its own process (v8 C6). Started by app.semantic.Embedder:

    python -I -X utf8 embed_worker.py --models <dir> [--site <tools\\memory-<hash>>]

Why a process: voice already loads a CPU onnxruntime and CUDA 12 DLLs into Kairo; a second
onnxruntime (the GPU build) in the same process would clash (one module name, one DLL per name).
Here the memory packages are first on the path and nothing else is loaded. A crash or a hang here
never takes Kairo down: the client kills and restarts this process.

No Kairo imports on purpose. Protocol: one JSON object per line on stdin, one answer per line on
the original stdout (everything else, e.g. onnxruntime's prints, goes to stderr).
    -> first line out: {"ready": true, "device": "cuda"|"cpu", "model": "onnx/..."} or {"ready": false, "error": ...}
    {"op": "embed", "texts": [...], "query": false} -> {"ok": true, "dim": 1024, "b64": <float32 rows>}
    {"op": "rank", "db": <index.sqlite>, "query": "...", "n": 30, "min": 0.25} -> {"ok": true, "hits": [[id, sim], ...]}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
from pathlib import Path

GPU_PREFS = ["onnx/model_fp16.onnx"]
CPU_PREFS = ["onnx/model_quantized.onnx", "onnx/model_int8.onnx", "onnx/model_uint8.onnx", "onnx/model.onnx"]
QUERY_TASK = "Given a question about the owner's life, retrieve the notes that answer it"
MAX_TOKENS = 512
BATCH = 16


class Model:
    def __init__(self, models_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        tok = models_dir / "tokenizer.json"
        if not tok.is_file():
            raise RuntimeError(f"the embedding model is missing ({tok})")
        if hasattr(ort, "preload_dlls"):
            try:
                ort.preload_dlls()          # CUDA/cuDNN from the nvidia-* packages next to onnxruntime
            except Exception as exc:        # no CUDA: the CPU does it
                print(f"no CUDA libraries: {exc}", file=sys.stderr)
        tries = []
        if "CUDAExecutionProvider" in ort.get_available_providers():
            tries += [(p, ["CUDAExecutionProvider", "CPUExecutionProvider"]) for p in GPU_PREFS]
        tries += [(p, ["CPUExecutionProvider"]) for p in CPU_PREFS]
        errors = []
        self.session = None
        for rel, providers in tries:
            path = models_dir / rel
            if not path.is_file():
                continue
            try:
                opts = ort.SessionOptions()
                opts.log_severity_level = 3
                session = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
            except Exception as exc:        # e.g. a driver too old for this CUDA: the next one
                errors.append(f"{rel}: {type(exc).__name__}: {exc}"[:200])
                continue
            on_gpu = session.get_providers()[0] == "CUDAExecutionProvider"
            if "CUDAExecutionProvider" in providers and not on_gpu:
                errors.append(f"{rel}: CUDA did not start (no GPU or driver too old)")
                continue                    # the fp16 model is for the GPU: the CPU takes the int8 one
            self.session, self.model, self.device = session, rel, "cuda" if on_gpu else "cpu"
            break
        if self.session is None:
            raise RuntimeError("no embedding model could start: " + ("; ".join(errors) or "no model file"))
        self.tok = Tokenizer.from_file(str(tok))
        self.tok.enable_truncation(MAX_TOKENS - 1)
        self.eos = self.tok.token_to_id("<|endoftext|>")
        self.pad = self.eos if self.eos is not None else 0
        self.matrix = None                  # (db, version, ids, vectors) for rank

    def embed(self, texts: list[str], query: bool = False):
        import numpy as np
        if query:
            texts = [f"Instruct: {QUERY_TASK}\nQuery:{t}" for t in texts]
        out = [self._run(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)]
        return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)

    def _run(self, batch: list[str]):
        """Qwen3-Embedding: the hidden state of the final EOS token, L2-normalised."""
        import numpy as np
        ids = []
        for enc in self.tok.encode_batch(batch):
            seq = list(enc.ids)
            if self.eos is not None and (not seq or seq[-1] != self.eos):
                seq.append(self.eos)
            ids.append(seq)
        n = max(len(x) for x in ids)
        input_ids = np.full((len(ids), n), self.pad, dtype=np.int64)
        mask = np.zeros((len(ids), n), dtype=np.int64)
        for r, seq in enumerate(ids):        # right padding: the last real token is at mask.sum() - 1
            input_ids[r, :len(seq)] = seq
            mask[r, :len(seq)] = 1
        feeds = {}
        for inp in self.session.get_inputs():
            if inp.name == "input_ids":
                feeds[inp.name] = input_ids
            elif inp.name == "attention_mask":
                feeds[inp.name] = mask
            elif inp.name == "position_ids":
                feeds[inp.name] = np.clip(np.cumsum(mask, axis=1) - 1, 0, None).astype(np.int64)
            elif inp.name.startswith("past_key_values"):   # a decoder export: an empty cache
                heads, dim = inp.shape[1], inp.shape[3]
                dtype = np.float16 if "float16" in inp.type else np.float32
                feeds[inp.name] = np.zeros((len(ids), heads, 0, dim), dtype=dtype)
        names = [o.name for o in self.session.get_outputs()]
        hidden = self.session.run(["last_hidden_state" if "last_hidden_state" in names else names[0]], feeds)[0]
        vec = hidden[np.arange(len(ids)), mask.sum(axis=1) - 1].astype(np.float32)
        return vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)

    def rank(self, db: str, query: str, n: int, minimum: float) -> list[list]:
        import numpy as np
        q = self.embed([query], query=True)[0]
        c = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            row = c.execute("SELECT value FROM meta WHERE key='vectors'").fetchone()
            version = row[0] if row else ""
            if self.matrix is None or self.matrix[:2] != (db, version):
                rows = c.execute("SELECT id, vec FROM chunks WHERE vec IS NOT NULL").fetchall()
                ids = np.array([r[0] for r in rows], dtype=np.int64)
                mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows \
                    else np.zeros((0, len(q)), np.float32)
                self.matrix = (db, version, ids, mat)
        finally:
            c.close()
        _, _, ids, mat = self.matrix
        if not len(ids) or mat.shape[1] != len(q):
            return []
        sims = mat @ q
        return [[int(ids[i]), round(float(sims[i]), 4)] for i in np.argsort(-sims)[:n] if sims[i] >= minimum]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--site", default="")
    a = ap.parse_args()
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)                           # stray prints (onnxruntime's included) go to stderr
    sys.stdout = sys.stderr
    if a.site:
        sys.path.insert(0, a.site)

    def say(obj: dict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    try:
        model = Model(Path(a.models))
    except Exception as exc:
        say({"ready": False, "error": f"{type(exc).__name__}: {exc}"[:400]})
        return 1
    say({"ready": True, "device": model.device, "model": model.model})
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req.get("op") == "embed":
                vecs = model.embed([str(t) for t in req.get("texts") or []], bool(req.get("query")))
                say({"ok": True, "dim": int(vecs.shape[1]) if vecs.size else 0,
                     "b64": base64.b64encode(vecs.astype("<f4").tobytes()).decode("ascii")})
            elif req.get("op") == "rank":
                say({"ok": True, "hits": model.rank(str(req["db"]), str(req["query"]), int(req.get("n", 30)),
                                                    float(req.get("min", 0.25)))})
            else:
                say({"ok": False, "error": "unknown op"})
        except Exception as exc:
            say({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
