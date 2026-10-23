"""kairo diag, this PC: graphics and main memory against the 12 GB / 16 GB the plan is sized for, HF_HUB_OFFLINE, the
Windows Firewall rule for calls, the Tailscale path to the phone, the call line."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import types

import pytest

from app import diag, gpu, tailnet
from tests.test_diag import comp, live, pc, run  # noqa: F401  (pc is a fixture)


@pytest.fixture
def only_system(monkeypatch):
    monkeypatch.setattr(diag, "CHECKS", [diag._system, diag._network])
    monkeypatch.setattr(diag, "firewall_check", lambda: diag.ok("net.firewall", "rule", "fine"))
    monkeypatch.setattr(tailnet, "status", lambda data_dir: None)
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **kw: {"available": False, "value": None, "off": False, "label": "not available", "why": "Windows only"})


def card(used, total=12288, processes=()):
    return {"name": "NVIDIA GeForce RTX 3060", "driver": "580.1", "used_mb": used, "total_mb": total, "processes": list(processes)}


def with_gpu(g, **extra):
    base = live()
    base["devices"]["gpu"] = g
    return {**base, **extra}


@pytest.mark.parametrize("used, total, status", [(7000, 12288, "PASS"), (11000, 12288, "WARN"), (12000, 12288, "FAIL"), (5000, 8192, "WARN")])
def test_graphics_memory_is_judged_against_the_twelve_gb_the_plan_is_sized_for(pc, only_system, used, total, status):
    _, by_id = run(pc, live=with_gpu(card(used, total, [{"pid": 1, "name": "llama-server.exe", "used_mb": 3100, "kairo": "voice_model"}])))
    c = by_id["system.vram"]
    assert c["status"] == status and c["value"] == used and c["unit"] == "MB"
    assert "of" in c["detail"] and "voice_model 3.0 GB" in c["detail"]
    if total < 10000:
        assert "this card has 8.0 GB" in c["detail"]


def test_no_graphics_card_answering_is_a_fail(pc, only_system, monkeypatch):
    monkeypatch.setattr(gpu, "summary", lambda: None)
    c = run(pc, live=with_gpu(None))[1]["system.vram"]
    assert c["status"] == "FAIL" and "nvidia-smi" in c["detail"]
    monkeypatch.setattr(gpu, "summary", lambda: card(7000))              # the server had none, this process asks itself
    assert run(pc, live=None)[1]["system.vram"]["status"] == "PASS"


@pytest.mark.parametrize("mem, status", [((8000, 16384), "PASS"), ((14000, 16384), "WARN"), ((16000, 16384), "FAIL"), (None, "WARN")])
def test_main_memory_against_the_sixteen_gb(pc, only_system, monkeypatch, mem, status):
    monkeypatch.setattr(diag, "ram", lambda: mem)
    c = run(pc, live=with_gpu(card(7000)))[1]["system.ram"]
    assert c["status"] == status and (mem is None or c["value"] == mem[0])


def test_ram_reads_the_real_os_here():
    used, total = diag.ram()
    assert 0 < used <= total


def test_ram_on_windows_comes_from_globalmemorystatusex(monkeypatch):
    def fill(ref):
        ref._obj.total, ref._obj.avail = 16 * 1024 ** 3, 6 * 1024 ** 3
        return 1
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=fill)), raising=False)
    assert diag.ram() == (10 * 1024, 16 * 1024)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=lambda ref: 0)), raising=False)
    assert diag.ram() is None


@pytest.mark.parametrize("value, status", [("1", "PASS"), ("true", "PASS"), (None, "WARN"), ("0", "WARN")])
def test_hf_hub_offline_is_a_warning_when_the_voice_could_wait_for_hugging_face(pc, only_system, value, status):
    c = run(pc, live=with_gpu(card(7000), env={"HF_HUB_OFFLINE": value}))[1]["system.hf_offline"]
    assert c["status"] == status
    assert run(pc, live=None)[1]["system.hf_offline"]["status"] == "WARN"


# ---------------------------------------------------------------- the firewall rule

RULE = {"found": True, "enabled": "True", "action": "Allow", "direction": "Inbound", "protocol": ["UDP"],
        "remote": ["100.64.0.0/255.192.0.0", "fd7a:115c:a1e0::/48"], "program": ["C:\\Python312\\python.exe"]}


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "_base_executable", "C:\\Python312\\python.exe", raising=False)


def rule(monkeypatch, **over):
    monkeypatch.setattr(diag, "firewall_rule", lambda: over.pop("whole") if "whole" in over else {**RULE, **over})
    return diag.firewall_check()


def test_the_calls_rule_is_fine_when_it_is_on_udp_tailscale_only_and_for_kairos_python(windows, monkeypatch):
    c = rule(monkeypatch)                                              # Windows writes the /10 as a netmask
    assert c.status == "PASS" and "UDP only" in c.detail and "Tailscale's addresses only" in c.detail
    assert rule(monkeypatch, remote="100.64.0.0/10", protocol="UDP").status == "PASS"   # one value, not a list


@pytest.mark.parametrize("over, words", [
    ({"whole": {"found": False}}, "there is no rule"),
    ({"enabled": "False"}, "switched off"),
    ({"action": "Block"}, "not Allow"),
    ({"remote": ["100.64.0.0/10", "192.168.1.0/24"]}, "192.168.1.0/24: not only Tailscale"),
    ({"remote": ["Any"]}, "Any: not only Tailscale"),
    ({"remote": []}, "not only Tailscale"),
    ({"program": ["C:\\Old\\python.exe"]}, "another Python"),
])
def test_a_wrong_or_missing_calls_rule_is_a_fail_with_the_reason(windows, monkeypatch, over, words):
    c = rule(monkeypatch, **over)
    assert c.status == "FAIL" and words in c.detail


def test_a_rule_for_tcp_is_only_a_warning_and_an_unreadable_one_asks_for_an_administrator(windows, monkeypatch):
    assert rule(monkeypatch, protocol=["TCP", "UDP"]).status == "WARN"
    c = rule(monkeypatch, whole=None)
    assert c.status == "WARN" and "administrator" in c.detail


def test_the_rule_is_not_a_thing_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    c = diag.firewall_check()
    assert c.status == "PASS" and "not applicable" in c.detail


def test_the_rule_is_asked_from_powershell_by_name_from_the_environment_and_a_failure_is_none(monkeypatch):
    calls = []

    def run_ps(cmd, **kw):
        calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(RULE))
    monkeypatch.setattr(subprocess, "run", run_ps)
    assert diag.firewall_rule() == RULE
    cmd, kw = calls[0]
    assert cmd[-2] == "-Command" and "$env:KAIRO_FW_RULE" in cmd[-1] and diag.FIREWALL_RULE not in cmd[-1]
    assert kw["env"]["KAIRO_FW_RULE"] == diag.FIREWALL_RULE and kw["timeout"] <= 30
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=3, stdout=""))
    assert diag.firewall_rule() is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("powershell")))
    assert diag.firewall_rule() is None


def test_the_rule_name_is_the_one_the_installer_creates():
    from pathlib import Path
    psm1 = (Path(__file__).resolve().parents[2] / "installer" / "Kairo.psm1").read_text(encoding="ascii")
    assert f"$script:CallFirewallRule = '{diag.FIREWALL_RULE}'" in psm1


def test_what_the_server_could_not_read_is_asked_again_by_whoever_runs_the_command(pc, only_system, monkeypatch):
    report, _ = run(pc, live=with_gpu(card(7000)), source="server")
    seen = [c["status"] for c in report["checks"] if c["id"] == "net.firewall"]
    monkeypatch.setattr(diag, "firewall_check", lambda: diag.bad("net.firewall", "rule", "there is no rule"))
    again = diag.recheck_here({**report, "checks": [{**c, "status": "WARN"} if c["id"] == "net.firewall" else c for c in report["checks"]]})
    assert seen == ["PASS"] and [c["status"] for c in again["checks"] if c["id"] == "net.firewall"] == ["FAIL"]
    assert diag.recheck_here(report) == report                          # a PASS stays what the server said


# ---------------------------------------------------------------- the line and the Tailscale path

def peer(direct=True, online=True, os_="iOS", relay=None):
    return {"ips": ["100.64.0.2"], "os": os_, "online": online, "direct": direct, "lan": True if direct else None, "relay": relay, "peer_relay": False}


def tail(monkeypatch, peers, source="live", age=0, self_relay="fra"):
    monkeypatch.setattr(tailnet, "status", lambda data_dir: {"peers": peers, "self_relay": self_relay, "source": source, "at": time.time() - age})


def test_a_phone_that_reaches_the_pc_directly_is_a_pass(pc, only_system, monkeypatch):
    tail(monkeypatch, [peer(), peer(online=False, direct=False), peer(os_="Windows", direct=False)])      # only the phone counts
    c = run(pc, live=with_gpu(card(7000)))[1]["net.tailscale"]
    assert c["status"] == "PASS" and "1 device(s) online, all direct" in c["detail"] and c["value"] == 0


def test_a_relayed_phone_is_a_warning_that_says_where_the_relay_is(pc, only_system, monkeypatch):
    tail(monkeypatch, [peer(direct=False, relay="fra")])
    c = run(pc, live=with_gpu(card(7000)))[1]["net.tailscale"]
    assert c["status"] == "WARN" and "1 of 1 device(s) go through Tailscale's relay (fra)" in c["detail"] and c["value"] == 1


def test_nobody_online_or_no_status_says_it_cannot_tell(pc, only_system, monkeypatch):
    tail(monkeypatch, [peer(online=False)])
    assert "can't be told" in run(pc, live=with_gpu(card(7000)))[1]["net.tailscale"]["detail"]
    monkeypatch.setattr(tailnet, "status", lambda data_dir: None)
    c = run(pc, live=with_gpu(card(7000)))[1]["net.tailscale"]
    assert c["status"] == "WARN" and "isn't available" in c["detail"]


def test_an_old_snapshot_says_how_old_it_is(pc, only_system, monkeypatch):
    tail(monkeypatch, [peer()], source="snapshot", age=3600)
    assert "snapshot 60 min old" in run(pc, live=with_gpu(card(7000)))[1]["net.tailscale"]["detail"]


def test_calls_on_the_old_line_are_a_fail_with_the_reason(pc, only_system):
    good = with_gpu(card(7000))
    assert run(pc, live=good)[1]["net.line"]["status"] == "PASS"
    bad = with_gpu(card(7000))
    bad["devices"]["line"] = {"webrtc_ready": False, "reason": "no_tailnet", "last_turn": "old"}
    c = run(pc, live=bad)[1]["net.line"]
    assert c["status"] == "FAIL" and "this PC has no Tailscale address right now" in c["detail"]
    assert run(pc, live=None)[1]["net.line"]["status"] == "FAIL"
