"""G8: the embedding process reads 4 pieces at a time, not 16 (voice recipe 9, 12). Measured on the research machine, CPU path, German
chunks: BATCH 16 left 2,855 MiB of main memory in use for good, BATCH 4 1,377 MiB (-52 %); MAX_TOKENS 256 saved only 174 MiB more and
would cut German chunks (281 tokens per 900 characters), so MAX_TOKENS stays 512. The owner can turn it back (g8.embed_batch4)."""

from __future__ import annotations

import subprocess
import textwrap

import pytest
from fastapi.testclient import TestClient

from app import embed_worker, flags, semantic
from app.main import create_app


def test_the_worker_reads_four_pieces_at_a_time_and_still_cuts_at_512_tokens():
    assert embed_worker.BATCH == 4
    assert embed_worker.MAX_TOKENS == 512          # 256 would cut German chunks (refuted, voice recipe 17)


def fake_model(batch):
    import numpy as np
    m = embed_worker.Model.__new__(embed_worker.Model)     # no ONNX session here: only the batching is looked at
    m.batch, calls = batch, []
    m._run = lambda texts: calls.append(list(texts)) or np.full((len(texts), 3), len(calls), dtype=np.float32)
    return m, calls


@pytest.mark.parametrize("batch, sizes", [(4, [4, 4, 2]), (16, [10]), (1, [1] * 10)])
def test_the_batch_decides_how_many_pieces_go_through_the_model_at_once(batch, sizes):
    m, calls = fake_model(batch)
    out = m.embed([f"t{i}" for i in range(10)])
    assert [len(c) for c in calls] == sizes
    assert out.shape == (10, 3)
    assert [t for c in calls for t in c] == [f"t{i}" for i in range(10)]        # order kept
    # each row is the answer of the call its text was in
    assert out[:, 0].tolist() == [i + 1 for i, n in enumerate(sizes) for _ in range(n)]


def test_a_query_gets_the_instruction_in_front_whatever_the_batch():
    m, calls = fake_model(4)
    m.embed(["a", "b"], query=True)
    assert all(t.startswith("Instruct: ") for t in calls[0]) and calls[0][1].endswith("Query:b")


def test_nothing_to_embed_is_an_empty_result_not_an_error():
    m, calls = fake_model(4)
    assert m.embed([]).shape == (0, 0) and calls == []


@pytest.mark.parametrize("given, kept", [(4, 4), (16, 16), (0, 1), (-3, 1), (500, 64)])
def test_the_batch_is_kept_between_1_and_64(given, kept):
    assert embed_worker.clamp_batch(given) == kept


def test_the_worker_takes_the_batch_on_its_command_line_and_defaults_to_four():
    assert embed_worker.parse(["--models", "m"]).batch == 4
    assert embed_worker.parse(["--models", "m", "--batch", "16"]).batch == 16
    assert embed_worker.parse(["--models", "m", "--cpu", "--site", "s"]).cpu is True


# ---------------------------------------------------------------- the client passes it on

FAKE_WORKER = textwrap.dedent('''
    import json, sys
    sys.stdout.write(json.dumps({"ready": True, "device": "cpu", "model": "x", "argv": sys.argv[1:]}) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        pass
''')


@pytest.fixture
def started(tmp_path, monkeypatch):
    """The command line the embedder starts its worker with."""
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(semantic, "WORKER", script)
    models = tmp_path / "models"
    models.mkdir()
    (models / "tokenizer.json").write_text("{}", encoding="utf-8")
    seen = []
    real = subprocess.Popen

    def popen(cmd, *a, **kw):
        seen.append(list(cmd))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(semantic.subprocess, "Popen", popen)

    def start(**kw):
        e = semantic.Embedder(models, **kw)
        e._start()
        return e, seen[-1]
    return start


@pytest.mark.parametrize("fixed, expect", [(True, "4"), (False, "16")])
def test_the_embedder_starts_its_worker_with_the_batch_it_is_told(started, fixed, expect):
    e, cmd = started(batch=lambda: semantic.embed_batch(fixed))
    try:
        assert cmd[cmd.index("--batch") + 1] == expect
    finally:
        e.close()


def test_without_a_batch_the_worker_uses_its_own_default(started):
    e, cmd = started()
    try:
        assert "--batch" not in cmd
    finally:
        e.close()


def test_the_batch_is_asked_at_every_start_so_a_flag_change_takes_effect_at_the_next_one(started):
    now = [True]
    e, cmd = started(batch=lambda: semantic.embed_batch(now[0]))
    assert cmd[cmd.index("--batch") + 1] == "4"
    e.close()
    now[0] = False
    e._start()
    try:
        assert e._proc is not None
    finally:
        e.close()


# ---------------------------------------------------------------- the owner's switch

def test_the_flag_is_on_by_default_and_says_what_it_does():
    on, what = flags.FLAGS["g8.embed_batch4"]
    assert on is True and "4" in what and "16" in what


def test_kairo_asks_the_flag_each_time_the_embedding_process_starts(migrated):
    with TestClient(create_app(migrated)) as c:
        ask = c.app.state.embed_batch
        assert ask() == 4
        assert c.put("/api/flags/g8.embed_batch4", json={"on": False}).status_code == 200
        assert ask() == 16
        assert c.put("/api/flags/g8.embed_batch4", json={"on": None}).status_code == 200
        assert ask() == 4
