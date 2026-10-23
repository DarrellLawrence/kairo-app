"""G11, the model router (app/router.py, config/routing.yaml): privacy classes enforced in code, default-deny,
a ceiling for every purpose, a scanner that can only tighten, the subscription as the default and a local route
that is off. Made-up data only."""

from __future__ import annotations

import itertools
import re
import time
from pathlib import Path

import pytest
import yaml

from app import loops, models, outbound, router, triage
from app.settings import REPO_ROOT

SHIPPED = REPO_ROOT / "config"
ALL_PURPOSES = sorted(models.DEFAULTS) + ["something-new"]
EVERYTHING = {"claude", "local", "groq", "cloudflare", "ollama_cloud", "mistral_free", "openrouter_free", "gemini_free"}


def rules_from(tmp_path: Path, text: str):
    (tmp_path / "routing.yaml").write_text(text, encoding="utf-8")
    return router.load_rules(tmp_path)


def everything_on(monkeypatch, tmp_path: Path):
    """Every provider enabled in the file AND an adapter for each in the code: the worst case for the privacy rule."""
    monkeypatch.setattr(router, "ADAPTERS", frozenset(EVERYTHING))
    rows = {"claude": "R1", "local": "R0", "groq": "R2", "cloudflare": "R2", "ollama_cloud": "R2",
            "mistral_free": "R3", "openrouter_free": "R3", "gemini_free": "R3"}
    doc = {"providers": {n: {"class": t, "enabled": True} for n, t in rows.items()},
           "local": {"purposes": sorted(router.LOCAL_ELIGIBLE), "lab_model_hash": "abc123"}}
    return rules_from(tmp_path, yaml.safe_dump(doc))


# ---------------------------------------------------------------- the purpose registry

def test_every_purpose_has_a_ceiling_and_nothing_else_does():
    assert set(router.CEILINGS) == set(models.DEFAULTS)                      # all 33, none stale
    assert all(c in router.CLASSES for c in router.CEILINGS.values())


def test_an_unknown_purpose_counts_as_personal_and_only_the_selftest_is_below_personal():
    assert router.ceiling_of("something-new") == "personal"
    below = {p for p, c in router.CEILINGS.items() if router.RANK[c] < router.RANK["personal"]}
    assert below == {"selftest"}                                             # loosening any other is an owner decision
    for p in ("chat", "call", "voice_note", "money", "money_review", "letters", "document", "summary"):
        assert router.ceiling_of(p) == "secret", p


def test_only_a_few_purposes_can_ever_run_locally_and_none_of_them_is_about_dates_or_judgment():
    assert router.LOCAL_ELIGIBLE == {"reply_check"}
    assert not router.LOCAL_ELIGIBLE & models.NEVER_HAIKU
    for dated in ("triage", "reminder", "capture", "letters", "promises", "brief", "draft", "chat", "call"):
        assert dated not in router.LOCAL_ELIGIBLE, dated
    assert set(router.LOCAL_KIND) == router.LOCAL_ELIGIBLE                   # a timeout for each, and for no other


# The prompt of every purpose that may run locally. A purpose cannot become eligible without its prompt being listed
# here, and the words below keep dates and safety judgments out of it (voice recipe 13: a 2.6B model got 8 of 39
# dates exactly right; review of G11: triage asks for a deadline and for the prompt-injection flag).
LOCAL_PROMPTS = {"reply_check": loops.REPLY_CHECK_PROMPT}
_DATE_OR_SAFETY = re.compile(r"\b(dates?|dated|deadlines?|due|YYYY\S*|times?|today|tomorrow|suspicious|inject\w*|instruct\w*|"
                             r"forward|invite|reveal)\b", re.I)


def test_every_purpose_that_may_run_locally_has_a_prompt_without_dates_or_safety_flags():
    assert set(LOCAL_PROMPTS) == set(router.LOCAL_ELIGIBLE)                  # add the prompt here when adding a purpose
    for purpose, prompt in LOCAL_PROMPTS.items():
        assert _DATE_OR_SAFETY.findall(prompt) == [], purpose


def test_triage_never_runs_locally_because_it_returns_a_deadline_and_the_injection_flag(tmp_path):
    assert "YYYY-MM-DD" in triage.PROMPT and '"suspicious"' in triage.PROMPT      # why: the mail's date becomes a task, the flag is a defence
    assert _DATE_OR_SAFETY.findall(triage.PROMPT)                                  # the check above would catch it
    assert "triage" not in router.LOCAL_ELIGIBLE and "triage" not in router.LOCAL_KIND
    rules = router.load_rules(SHIPPED)
    ok = dict(content="personal", local_flag=True, local_ready=True, local_model="abc")
    lab = {"providers": {"claude": {"class": "R1", "enabled": True}, "local": {"class": "R0", "enabled": True}},
           "local": {"purposes": ["triage"], "lab_model_hash": "abc"}}
    listed = rules_from(tmp_path, yaml.safe_dump(lab))
    assert listed.local_purposes == frozenset() and any("triage" in p for p in listed.problems)
    assert router.decide(listed, "triage", **ok).route == "claude" and router.decide(rules, "triage", **ok).route == "claude"


# ---------------------------------------------------------------- the scanner

@pytest.mark.parametrize("text, hit, klass", [
    ("write to anna.example@mail.example about it", "email", "personal"),
    ("call 079 555 12 34 tonight", "phone", "personal"),
    ("call +41 79 555 12 34 tonight", "phone", "personal"),
    ("call 0041795551234 tonight", "phone", "personal"),
    ("IBAN CH93 0076 2011 6238 5295 7 please", "iban", "secret"),
    ("iban ch9300762011623852957".upper(), "iban", "secret"),
    ("DE89 3704 0044 0532 0130 00", "iban", "secret"),
    ("card 4111 1111 1111 1111 expires", "card", "secret"),
    ("AHV 756.1234.5678.97", "ahv", "secret"),
    ("token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "key", "secret"),
    ("sk-" + "x" * 24, "key", "secret"),
    ("ghs_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "key", "secret"),
    ("xoxb-" + "1234567890-abcdef", "key", "secret"),
    ("AKIA" + "ABCDEFGHIJKLMNOP", "key", "secret"),
    ("AIza" + "SyA1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P", "key", "secret"),
    ("-----BEGIN RSA PRIVATE KEY-----", "key", "secret"),
])
def test_the_scanner_finds_what_it_should(text, hit, klass):
    found, hits = router.scan(text)
    assert hit in hits and found == klass


def test_the_scanner_finds_names_from_the_database_whole_words_only():
    names = router.name_tokens(["Maja Beispiel", "Tom", "Li"])              # "Li" is too short to count
    assert router.scan("lunch with maja tomorrow", names) == ("personal", ("name",))
    assert router.scan("Beispiel, Maja asked", names)[1] == ("name",)
    assert router.scan("the tomato and the lily", names) == ("public", ())     # not inside another word
    assert router.scan("Maja", None) == ("public", ())                       # no list, no names


def test_the_scanner_is_not_fooled_by_invisible_characters_or_full_width_digits():
    assert "email" in router.scan("a​nna@mail.example")[1]
    assert "iban" in router.scan("CH93 0076 2011 6238 5295 7".replace("0", "０"))[1]


def test_the_scanner_says_what_it_found_never_the_text():
    klass, hits = router.scan("mail anna.example@mail.example, IBAN CH93 0076 2011 6238 5295 7")
    assert klass == "secret" and {"email", "iban"} <= set(hits)                  # (digits of an IBAN can look like a phone number too)
    assert set(hits) <= set(router.HIT_CLASS) and not any("anna" in h or "CH93" in h for h in hits)


def test_the_scanner_can_only_tighten_the_class_of_a_purpose():
    samples = ["", "hello", "anna.example@mail.example", "CH93 0076 2011 6238 5295 7", "ordinary words " * 40]
    names = router.name_tokens(["Maja Beispiel"])
    for purpose, text in itertools.product(ALL_PURPOSES, samples):
        scanned, hits = router.scan(text, names)
        d = router.decide(router.load_rules(SHIPPED), purpose, content=scanned, hits=hits)
        assert router.RANK[d.content] >= router.RANK[router.ceiling_of(purpose)], (purpose, text)
        assert router.RANK[d.content] >= router.RANK[scanned]
    # a caller that passes a looser class than the ceiling is ignored
    assert router.decide(router.load_rules(SHIPPED), "chat", content="public").content == "secret"
    assert router.decide(router.load_rules(SHIPPED), "selftest", content="secret").content == "secret"
    assert router.decide(router.load_rules(SHIPPED), "selftest", content="nonsense").content == "personal"


def test_the_scanner_is_fast_on_a_big_prompt_and_survives_hostile_text():
    big = ("The quick brown fox, 12 apples and 3 pears; see you at 10:30 on 23.10.2026. " * 700)[:52_000]
    names = router.name_tokens([f"Person Number{i}" for i in range(200)])
    t0 = time.perf_counter()
    for _ in range(5):
        assert router.scan(big, names) == ("public", ())
    assert (time.perf_counter() - t0) / 5 < 0.5                              # measured ~1-5 ms; the margin is for a busy CI box
    t0 = time.perf_counter()
    for hostile in ("9 " * 30_000, "A1" * 30_000, "CH93" + " 0" * 30_000, "+" + "1 " * 30_000, "a" * 60_000 + "@", "x@" * 20_000):
        router.scan(hostile, names)
    assert time.perf_counter() - t0 < 20                                     # no catastrophic backtracking


# ---------------------------------------------------------------- the rules file

def test_the_shipped_routing_yaml_lists_every_class_with_free_cloud_off():
    rules = router.load_rules(SHIPPED)
    assert rules is not None and not rules.problems
    on = {n for n, p in rules.providers.items() if p.enabled}
    assert on == {"claude", "local"}
    assert {n: p.tier for n, p in rules.providers.items()} == {
        "claude": "R1", "local": "R0", "groq": "R2", "cloudflare": "R2", "ollama_cloud": "R2",
        "mistral_free": "R3", "openrouter_free": "R3", "gemini_free": "R3"}
    assert rules.local_purposes == frozenset() and rules.lab_model_hash == ""
    assert rules.providers["local"].host == router.LOCAL_HOST


def test_the_shipped_file_says_why_freellmapi_and_colibri_are_not_used_and_what_free_cloud_needs():
    text = (SHIPPED / "routing.yaml").read_text(encoding="utf-8")
    assert "Not used: FreeLLMAPI" in text and "Not used: Colibri" in text
    lines = text.splitlines()
    fl = next(i for i, ln in enumerate(lines) if "Not used: FreeLLMAPI" in ln)
    assert "Not used: Colibri" in lines[fl + 1]                              # one line each, the reason on the same line
    assert "nondeterministic" in lines[fl] or "does not pin" in lines[fl]
    assert "3060" in lines[fl + 1] or "nothing measured" in lines[fl + 1]
    assert "dated owner outbound decision" in text and "no adapter" in text
    assert "freellmapi:" not in text and "colibri:" not in text              # comments, not providers
    assert "18 or" in text and "public/low content only" in text             # recipe 16.8: what the decision must cover
    assert "model-improvement" in text                                       # recipe 16.12: the owner's check of the one cloud that has everything


def test_free_cloud_has_no_host_in_outbound_py_and_no_adapter_in_the_code():
    assert router.ADAPTERS == {"claude", "local"}
    src = (REPO_ROOT / "server" / "app" / "outbound.py").read_text(encoding="utf-8")
    for host in ("groq.com", "cloudflare.com", "ollama.com", "mistral", "openrouter", "generativelanguage", "gemini"):
        assert host not in src, host
    assert "router.py" not in outbound.MODULES                               # the router never touches the network
    code = (REPO_ROOT / "server" / "app" / "router.py").read_text(encoding="utf-8")
    assert not any(k in code for k in ("import httpx", "urllib.request", "import requests", "http.client", "socket"))


def test_a_class_looser_than_the_code_allows_switches_the_provider_off(tmp_path):
    rules = rules_from(tmp_path, """
providers:
  claude: { class: R1, enabled: true }
  mistral_free: { class: R0, enabled: true }
  groq: { class: R1, enabled: true }
  local: { class: R1, enabled: true }
  odd: { class: R2, enabled: true }
  weird: { class: banana, enabled: true }
""")
    assert {n: p.enabled for n, p in rules.providers.items()} == {
        "claude": True, "mistral_free": False, "groq": False, "local": True, "odd": False, "weird": False}
    assert len(rules.problems) == 4 and any("mistral_free" in p for p in rules.problems)     # an unlisted name is R3 at best
    # tighter than the floor is fine (a local model the owner doesn't want to trust fully)
    assert rules_from(tmp_path, "providers:\n  local: { class: R2, enabled: true }\n").providers["local"].enabled


def test_the_local_provider_must_be_on_this_pc(tmp_path):
    rules = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: true }\n  local: { class: R0, enabled: true, host: 10.0.0.5 }\n")
    assert rules.providers["local"].enabled is False and "127.0.0.1" in rules.problems[0]


def test_purposes_that_may_never_run_locally_are_ignored_in_the_file(tmp_path):
    rules = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: true }\nlocal:\n  purposes: [reply_check, triage, reminder, chat]\n  lab_model_hash: h\n")
    assert rules.local_purposes == {"reply_check"} and len(rules.problems) == 3 and any("triage" in x for x in rules.problems)


def test_enabled_must_be_exactly_true(tmp_path):
    rules = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: 'yes' }\n  local: { class: R0 }\n")
    assert not rules.providers["claude"].enabled and not rules.providers["local"].enabled


@pytest.mark.parametrize("text", ["", "[]", "providers: [claude]", "providers: 3", ": : :", "version: 1\n"])
def test_a_missing_or_broken_file_gives_no_rules(tmp_path, text):
    assert rules_from(tmp_path, text) is None
    assert router.load_rules(tmp_path / "nope") is None and router.load_rules(None) is None


# ---------------------------------------------------------------- the decision

def test_the_subscription_is_the_default_for_every_purpose_in_every_mode():
    rules = router.load_rules(SHIPPED)
    for purpose, mode in itertools.product(ALL_PURPOSES, router.MODES):
        d = router.decide(rules, purpose, content=router.ceiling_of(purpose), mode=mode, model="haiku")
        assert (d.route, d.tier, d.model) == ("claude", "R1", "haiku"), (purpose, mode)
        assert "default" in d.reason and d.rules is True


def test_the_local_route_is_off_for_every_purpose_even_when_the_flag_is_on_and_the_model_is_up():
    rules = router.load_rules(SHIPPED)
    for purpose in ALL_PURPOSES:
        d = router.decide(rules, purpose, content=router.ceiling_of(purpose), local_flag=True, local_ready=True)
        assert d.route == "claude", purpose                                  # no purpose is listed, the lab has not passed
    d = router.decide(rules, "reply_check", content="personal", local_flag=False)
    assert "g11.local_route is off" in d.reason
    d = router.decide(rules, "reply_check", content="personal", local_flag=True, local_ready=True)
    assert "router lab has not passed" in d.reason


def lab_passed(tmp_path):
    return rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: true }\n  local: { class: R0, enabled: true }\n"
                                "local:\n  purposes: [reply_check]\n  lab_model_hash: abc\n")


def test_a_lab_passed_purpose_runs_locally_only_when_everything_holds(tmp_path):
    rules = lab_passed(tmp_path)
    ok = dict(content="personal", local_flag=True, local_ready=True, local_model="abc")
    assert router.decide(rules, "reply_check", **ok).route == "local"
    assert router.decide(rules, "reply_check", **ok).tier == "R0"
    for change, why in [({"local_flag": False}, "g11.local_route is off"), ({"local_ready": False}, "not running"),
                        ({"call_open": True}, "call is open"), ({"tools": True}, "no tools"), ({"images": True}, "no tools"),
                        ({"mode": "owner"}, "background jobs"), ({"mode": "call"}, "background jobs")]:
        d = router.decide(rules, "reply_check", **{**ok, **change})
        assert d.route == "claude" and why in d.reason, (change, d.reason)
    for purpose, why in [("triage", "may never"), ("summary", "may never"), ("reminder", "may never"), ("chat", "may never")]:
        d = router.decide(rules, purpose, **ok)
        assert d.route == "claude", purpose
    # no hash, no local
    bare = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: true }\n  local: { class: R0, enabled: true }\nlocal:\n  purposes: [reply_check]\n")
    assert router.decide(bare, "reply_check", **ok).route == "claude"


def test_a_local_model_without_a_job_slot_is_not_used_and_the_reason_says_so(tmp_path):
    """The voice model's server has one slot today: a job would evict the mouth's cached start (review of G11)."""
    rules = lab_passed(tmp_path)
    ok = dict(content="personal", local_flag=True, local_model="abc")
    why = "the voice model's server has one slot; the local route needs a second slot for jobs (-np 2)"
    d = router.decide(rules, "reply_check", local_ready=False, local_why=why, **ok)
    assert d.route == "claude" and why in d.reason
    d = router.decide(rules, "reply_check", local_ready=False, **ok)                 # no reason given: the old words
    assert d.route == "claude" and "the local model is not running" in d.reason
    assert router.decide(rules, "reply_check", local_ready=True, local_why=why, **ok).route == "local"   # ready wins: why is only the words


def test_the_lab_passes_one_model_and_only_that_model_runs_locally(tmp_path):
    """lab_model_hash is the fingerprint of the model the router lab passed (More -> Usage shows the running one). A
    different model, an unknown one or none is not that model: the job goes to the subscription."""
    rules = lab_passed(tmp_path)
    ok = dict(content="personal", local_flag=True, local_ready=True)
    assert router.decide(rules, "reply_check", local_model="abc", **ok).route == "local"
    for running in ("abd", "", "ABC", "abc "):
        d = router.decide(rules, "reply_check", local_model=running, **ok)
        assert d.route == "claude" and "not the model the router lab passed" in d.reason, running
    assert router.decide(rules, "reply_check", **ok).route == "claude"                      # nothing said: not that model


def test_a_rejected_local_answer_goes_to_the_subscription_with_the_reason(tmp_path):
    rules = lab_passed(tmp_path)
    first = router.decide(rules, "reply_check", content="personal", local_flag=True, local_ready=True, local_model="abc", model="haiku")
    again = router.decide(rules, "reply_check", content="personal", local_flag=True, local_ready=True, local_model="abc", model="haiku",
                          exclude=frozenset({"local"}), fallback_from="local")
    assert first.route == "local" and again.route == "claude" and again.fallback_from == "local"
    assert again.reason.startswith("local did not work out")


def test_when_the_subscription_is_paused_a_lab_passed_job_still_runs_locally_and_the_rest_wait(tmp_path):
    rules = lab_passed(tmp_path)
    ok = dict(local_flag=True, local_ready=True, local_model="abc", claude_hold="paused (usage limit)")
    assert router.decide(rules, "reply_check", content="personal", **ok).route == "local"
    d = router.decide(rules, "triage", content="personal", **ok)
    assert d.route == "none" and "paused (usage limit)" in d.reason
    assert router.decide(router.load_rules(SHIPPED), "reply_check", content="personal", claude_hold="paused (usage limit)").route == "none"


def test_switching_the_subscription_off_in_the_file_stops_everything_that_needs_it(tmp_path):
    rules = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: false }\n")
    d = router.decide(rules, "chat", content="secret")
    assert d.route == "none" and "claude: switched off in routing.yaml" in d.reason
    d = router.decide(rules_from(tmp_path, "providers:\n  groq: { class: R2, enabled: true }\n"), "chat", content="secret")
    assert d.route == "none" and "claude: not listed in routing.yaml" in d.reason


# ---------------------------------------------------------------- the matrix

def all_calls():
    for purpose, scanned, mode, tools, images, paused, call_open, flag, ready in itertools.product(
            ALL_PURPOSES, router.CLASSES, router.MODES, (False, True), (False, True), (False, True), (False, True),
            (False, True), (False, True)):
        yield purpose, scanned, dict(mode=mode, tools=tools, images=images, claude_hold="paused (usage limit)" if paused else "", call_open=call_open,
                                     local_flag=flag, local_ready=ready, local_model="abc123")


def test_matrix_no_free_cloud_route_for_personal_or_secret_content_in_any_mode(monkeypatch, tmp_path):
    """The privacy rule: whatever the file says, whatever has an adapter, R2 and R3 never see personal or secret
    content, and R3 sees only public. Every provider on, every flag on."""
    worst = everything_on(monkeypatch, tmp_path)
    assert {n for n, p in worst.providers.items() if p.enabled} == EVERYTHING
    seen = set()
    for purpose, scanned, kw in all_calls():
        d = router.decide(worst, purpose, content=scanned, **kw)
        seen.add(d.route)
        if d.route == "none":
            continue
        assert router.RANK[d.content] <= router.RANK[router.MAY_RECEIVE[d.tier]], (purpose, scanned, kw, d)
        if d.tier in ("R2", "R3"):
            assert router.RANK[d.content] <= router.RANK["low"], (purpose, scanned, kw, d)
            assert d.mode == "job" and not kw["tools"] and not kw["images"]
        if d.tier == "R3":
            assert d.content == "public"
        if router.RANK[d.content] >= router.RANK["personal"]:
            assert d.tier in ("R0", "R1"), (purpose, scanned, kw, d)
    assert {"claude", "local"} <= seen and seen & {"groq", "cloudflare", "ollama_cloud", "mistral_free"}   # the matrix isn't vacuous


def test_matrix_with_the_shipped_file_every_call_is_the_subscription_or_nothing():
    rules = router.load_rules(SHIPPED)
    for purpose, scanned, kw in all_calls():
        d = router.decide(rules, purpose, content=scanned, **kw)
        assert d.route == ("none" if kw["claude_hold"] else "claude"), (purpose, scanned, kw, d)


def test_matrix_only_a_public_selftest_could_ever_reach_a_free_cloud_provider(monkeypatch, tmp_path):
    worst = everything_on(monkeypatch, tmp_path)
    free = set()
    for purpose, scanned, kw in all_calls():
        d = router.decide(worst, purpose, content=scanned, **kw)
        if d.route not in ("claude", "local", "none"):
            free.add((purpose, d.content))
    assert free == {("selftest", "public"), ("selftest", "low")}              # a fixed question; the scanner finds public or more


def test_matrix_nothing_routes_without_the_file_except_the_subscription_as_before(monkeypatch):
    monkeypatch.setattr(router, "ADAPTERS", frozenset(EVERYTHING))
    for purpose, scanned, kw in all_calls():
        d = router.decide(None, purpose, content=scanned, **kw)
        assert d.rules is False
        assert d.route == ("none" if kw["claude_hold"] else "claude"), (purpose, scanned, kw, d)
        assert "no routing.yaml" in d.reason or d.route == "none"


def test_a_provider_in_the_file_without_an_adapter_never_routes(tmp_path):
    rules = rules_from(tmp_path, "providers:\n  claude: { class: R1, enabled: true }\n  groq: { class: R2, enabled: true }\n")
    assert rules.providers["groq"].enabled                                    # the file says on ...
    for purpose in ALL_PURPOSES:
        d = router.decide(rules, purpose, content="public")
        assert d.route == "claude", purpose                                   # ... the code has no adapter, so it is off
