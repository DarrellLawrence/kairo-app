"""Model router (G11, voice recipe section 13): which provider may see what, decided in code.

The model is still reached only through app/brain/. This module decides and explains; it never
calls a model and never touches the network (it is not in outbound.MODULES and must not become one).

Content classes, from the most harmless to the most private:

    public < low < personal < secret

Provider classes (the most private class each may receive; default-deny):

    R0 local         llama-server on 127.0.0.1, this PC only       up to secret
    R1 subscription  Claude through the Claude Code CLI            up to secret (the owner checks that
                                                                   "model improvement" is off, voice recipe 16.12)
    R2 free cloud, does not train on what it gets                  up to low
    R3 free cloud that trains, or unknown                          public only

What keeps this safe:
- config/routing.yaml lists the providers. One that is not listed is off. One without an adapter in
  ADAPTERS is off too, whatever the file says (there is no free-cloud adapter, and no host for one in
  outbound.py). The file can switch on only what the code knows, and cannot make a provider more trusted
  than the code's floor (FLOOR): a looser class in the file switches the provider off.
- Every purpose in models.DEFAULTS has a content ceiling here (CEILINGS). An unknown purpose is personal.
- The scanner (email, phone, IBAN, card, AHV number, key, a name from the database) can only tighten the
  class, never loosen it.
- The subscription is the default for every purpose. The local route is off for every purpose until the
  owner turns g11.local_route on AND the router lab has passed for that purpose and model (routing.yaml
  local.purposes + lab_model_hash, PC measurement P21). A job with tools, images, or an owner turn never
  goes local.
- Every decision is logged with its reason (route_log, More -> Usage). The text of a prompt never is.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

import yaml
from sqlmodel import Session, delete, select

from . import flags
from .db import RouteLog, utcnow
from .models import NEVER_HAIKU

log = logging.getLogger("kairo.router")

CLASSES = ("public", "low", "personal", "secret")           # least to most private
RANK = {c: i for i, c in enumerate(CLASSES)}
TIERS = ("R0", "R1", "R2", "R3")
# The most private class each provider class may receive.
MAY_RECEIVE = {"R0": "secret", "R1": "secret", "R2": "low", "R3": "public"}
MODES = ("owner", "call", "job")

# The providers that have an adapter in this version. A provider the yaml lists but this set lacks is off.
# Free cloud has none: enabling one needs a dated owner outbound decision (voice recipe 16.8), an entry in
# outbound.py and an adapter that refuses without a router Decision and without tools == [].
ADAPTERS: frozenset[str] = frozenset({"claude", "local"})

# The class a provider must have at least (looser in the yaml -> the provider is off). Names not listed
# are R3. The hosts of the free-cloud providers are in routing.yaml only; no code uses them.
FLOOR = {"local": "R0", "claude": "R1", "groq": "R2", "cloudflare": "R2", "ollama_cloud": "R2"}
LOCAL_HOST = "127.0.0.1"

# The most private content each purpose may carry. Unknown purposes count as personal. Only the self-test
# (a fixed question) is below personal; loosening any other needs a code change and an owner decision.
_SECRET = ("chat", "call", "voice_note", "chat_heavy", "learn_call", "learn", "letters", "document", "gauntlet",
           "coach", "plan", "review_big", "judgment", "judgment_high", "proactive", "summary", "money", "money_review")
_PERSONAL = ("triage", "capture", "brief", "reminder", "promises", "reply_check", "draft", "study", "lerndoku",
             "voice_check", "acceptance", "loop_check", "voice_agenda", "voice_lab")
CEILINGS: dict[str, str] = {**{p: "secret" for p in _SECRET}, **{p: "personal" for p in _PERSONAL}, "selftest": "public"}
UNKNOWN_PURPOSE = "personal"

# The only purposes that can ever run on the local model: Haiku jobs that read mail or text and answer a
# small JSON, no dates or times (a small model got 8 of 39 reminders exactly right), no tools, no images.
# routing.yaml local.purposes can only choose among these.
LOCAL_ELIGIBLE = frozenset({"triage", "reply_check", "summary"})
LOCAL_KIND = {"triage": "small", "reply_check": "small", "summary": "batch"}     # which timeout: 4 s or 15 s
assert not (LOCAL_ELIGIBLE & NEVER_HAIKU)


def ceiling_of(purpose: str) -> str:
    return CEILINGS.get(purpose, UNKNOWN_PURPOSE)


def tighter(a: str, b: str) -> str:
    """The more private of two classes. This is the only way a class changes after the ceiling: up."""
    return a if RANK[a] >= RANK[b] else b


# ---------------------------------------------------------------- scanner (can only tighten)

_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
_EMAIL = re.compile(r"[\w.%+\-]{1,64}@[\w\-]{1,63}(?:\.[\w\-]{1,63}){1,5}")
_PHONE = re.compile(r"(?<![\w.])(?:\+|00)\d(?:[ ()./\-]?\d){8,14}(?!\d)"
                    r"|(?<![\w.])0\d{1,2}[ ./\-]?\d{3}[ ./\-]?\d{2}[ ./\-]?\d{2}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ \-]?[A-Z0-9]){11,30}(?![A-Z0-9])")
_CARD = re.compile(r"(?<![\w-])\d(?:[ \-]?\d){12,18}(?![\w-])")
_AHV = re.compile(r"(?<!\d)756[ .]?\d{4}[ .]?\d{4}[ .]?\d{2}(?!\d)")
_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
                  r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
HIT_CLASS = {"email": "personal", "phone": "personal", "name": "personal",
             "iban": "secret", "card": "secret", "ahv": "secret", "key": "secret"}
_MIN_NAME = 3
_WORD = re.compile(r"[^\W_]+")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def name_tokens(names: Iterable[str]) -> frozenset[str]:
    """The words of the names in the database (3+ letters each, lower case): a text that has one of them as a whole
    word is about a person. A name made only of shorter words (a two-letter surname) is not looked for."""
    return frozenset(p.casefold() for n in names for p in re.split(r"[\W_]+", unicodedata.normalize("NFKC", n or ""))
                     if len(p) >= _MIN_NAME)


def scan(text: str, names: frozenset[str] | None = None) -> tuple[str, tuple[str, ...]]:
    """(the class this text needs at least, the kinds found). Never returns the matched text.
    public means 'nothing found', not 'safe': the ceiling of the purpose is what decides, the scan only raises it."""
    if not text:
        return "public", ()
    t = unicodedata.normalize("NFKC", _INVISIBLE.sub("", text))
    hits: list[str] = []
    if "@" in t and _EMAIL.search(t):
        hits.append("email")
    if _PHONE.search(t):
        hits.append("phone")
    if any(len(re.sub(r"[ \-]", "", m.group())) in range(15, 35) and sum(c.isdigit() for c in m.group()) >= 8
           for m in _IBAN.finditer(t)):
        hits.append("iban")
    if any(13 <= len(d := re.sub(r"\D", "", m.group())) <= 19 and _luhn(d) for m in _CARD.finditer(t)):
        hits.append("card")
    if _AHV.search(t):
        hits.append("ahv")
    if _KEY.search(t):
        hits.append("key")
    if names and not names.isdisjoint(w.casefold() for w in _WORD.findall(t)):
        hits.append("name")
    klass = "public"
    for h in hits:
        klass = tighter(klass, HIT_CLASS[h])
    return klass, tuple(hits)


# ---------------------------------------------------------------- the rules (config/routing.yaml)

@dataclass(frozen=True)
class Provider:
    name: str
    tier: str
    enabled: bool
    host: str = ""


@dataclass(frozen=True)
class Rules:
    providers: dict[str, Provider]
    local_purposes: frozenset[str] = frozenset()
    lab_model_hash: str = ""
    timeout_s: dict[str, float] = field(default_factory=lambda: {"small": 4.0, "batch": 15.0})
    problems: tuple[str, ...] = ()        # what the loader refused, in words (More -> Usage shows them)


def _tier_ok(name: str, tier: str) -> bool:
    return TIERS.index(tier) >= TIERS.index(FLOOR.get(name, "R3"))


def load_rules(config_dir: Path | None) -> Rules | None:
    """The rules from <config_dir>/routing.yaml, or None when there is no usable file (then only the
    subscription runs, see decide). A row the code doesn't allow is dropped and said in `problems`."""
    path = Path(config_dir) / "routing.yaml" if config_dir else None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path and path.is_file() else None
    except (OSError, yaml.YAMLError, UnicodeError):
        log.warning("routing.yaml unreadable", exc_info=True)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return None
    problems: list[str] = []
    providers: dict[str, Provider] = {}
    for name, row in raw["providers"].items():
        name = str(name)
        row = row if isinstance(row, dict) else {}
        tier = str(row.get("class") or "").upper()
        host = str(row.get("host") or "")
        if tier not in TIERS:
            problems.append(f"{name}: class {row.get('class')!r} is not one of R0-R3, so it is off")
            tier, enabled = "R3", False
        elif not _tier_ok(name, tier):
            problems.append(f"{name}: {tier} is more trusted than the code allows ({FLOOR.get(name, 'R3')} at best), so it is off")
            enabled = False
        elif name == "local" and host and host != LOCAL_HOST:
            problems.append(f"local: host {host!r} is not {LOCAL_HOST}, so it is off")
            enabled = False
        else:
            enabled = row.get("enabled") is True
        providers[name] = Provider(name, tier, enabled, host)
    local = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    purposes = set()
    for p in local.get("purposes") or []:
        if p in LOCAL_ELIGIBLE:
            purposes.add(p)
        else:
            problems.append(f"local.purposes: {p!r} may never run on the local model, ignored")
    timeouts = {"small": 4.0, "batch": 15.0}
    for k, v in (local.get("timeout_s") or {}).items():
        if k in timeouts and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 60:
            timeouts[k] = float(v)
    return Rules(providers, frozenset(purposes), str(local.get("lab_model_hash") or "").strip(), timeouts, tuple(problems))


# ---------------------------------------------------------------- the decision

@dataclass(frozen=True)
class Decision:
    route: str                 # "claude" | "local" | a free-cloud name (none has an adapter yet) | "none"
    tier: str | None           # R0..R3 of the route, None when nothing runs
    purpose: str
    mode: str
    content: str               # the class the decision was made for: the ceiling, tightened by the scanner
    reason: str                # in plain words
    model: str | None = None   # Claude's alias for the subscription, "local" for the local model
    hits: tuple[str, ...] = ()
    fallback_from: str | None = None
    rules: bool = True         # False: no usable routing.yaml


# Without a usable routing.yaml only the subscription is possible: it is what Kairo always did, and the router
# adds nothing. Anything else needs the file.
_SUBSCRIPTION_ONLY = Rules({"claude": Provider("claude", "R1", True)})


def decide(rules: Rules | None, purpose: str, *, content: str, mode: str = "job", model: str | None = None,
           tools: bool = False, images: bool = False, claude_hold: str = "", call_open: bool = False,
           local_flag: bool = False, local_ready: bool = False, hits: tuple[str, ...] = (),
           exclude: frozenset[str] = frozenset(), fallback_from: str | None = None, fallback_why: str = "") -> Decision:
    """The route for one model call. First allowed wins: a free-cloud provider (none has an adapter), then the
    local model, then the subscription. `content` is the class to protect, ceiling_of(purpose) tightened by scan():
    a caller can't pass anything looser than the ceiling.

    A provider is skipped when it isn't listed, isn't enabled, has no adapter, is a class not allowed to see this
    content (MAY_RECEIVE), or doesn't fit the call (tools, images, mode, a call open, not ready, on hold: claude_hold says why)."""
    have = rules is not None
    rules = rules or _SUBSCRIPTION_ONLY
    content = tighter(content if content in RANK else UNKNOWN_PURPOSE, ceiling_of(purpose))
    base = dict(purpose=purpose, mode=mode, content=content, hits=hits, fallback_from=fallback_from, rules=have)
    why: dict[str, str] = {}
    order = [n for n in rules.providers if n not in ("local", "claude")] + ["local", "claude"]
    for name in order:
        skip = _skip(rules, name, purpose=purpose, content=content, mode=mode, tools=tools, images=images,
                     claude_hold=claude_hold, call_open=call_open, local_flag=local_flag, local_ready=local_ready,
                     excluded=name in exclude)
        if skip:
            why[name] = skip
            continue
        tier = rules.providers[name].tier
        if name == "claude":
            reason = "the subscription: the default for every purpose" if have else "the subscription, as always (no routing.yaml)"
            if purpose in LOCAL_ELIGIBLE and "local" in why:
                reason += f"; local not used: {why['local']}"
            if fallback_from:
                reason = f"{fallback_from} did not work out" + (f" ({fallback_why})" if fallback_why else "") + "; " + reason
        elif name == "local":
            reason = "the local model: this purpose passed the router lab"
        else:
            reason = f"{name}: {content} content is allowed on {tier}"
        return Decision(name, tier, model=model if name == "claude" else name, reason=reason, **base)
    return Decision("none", None, reason="no route allowed: " + "; ".join(f"{n}: {w}" for n, w in why.items()), **base)


def _skip(rules: Rules, name: str, *, purpose, content, mode, tools, images, claude_hold, call_open, local_flag,
          local_ready, excluded) -> str | None:
    """Why this provider can't take the call, or None when it can."""
    prov = rules.providers.get(name)
    if prov is None:
        return "not listed in routing.yaml"
    if excluded:
        return "tried already"
    if not prov.enabled:
        return "switched off in routing.yaml"
    if name not in ADAPTERS:
        return "no adapter in this version"
    if RANK[content] > RANK[MAY_RECEIVE[prov.tier]]:
        return f"{content} content may not go to {prov.tier}"
    if name == "claude":
        return claude_hold or None
    if name == "local":
        if not local_flag:
            return "g11.local_route is off"
        if purpose not in LOCAL_ELIGIBLE:
            return "this purpose may never run on the local model"
        if purpose not in rules.local_purposes or not rules.lab_model_hash:
            return "the router lab has not passed for this purpose"
        if mode != "job":
            return "only background jobs run locally"
        if tools or images:
            return "it has no tools and no images"
        if call_open:
            return "a call is open and the voice needs the model"
        if not local_ready:
            return "the local model is not running"
        return None
    if tools or images or mode != "job":
        return "free cloud takes background jobs with no tools and no images"
    return None


# ---------------------------------------------------------------- the router: flags, scan, decision, log

FLAG_ROUTER = "g11.router"
FLAG_LOCAL = "g11.local_route"
KEEP_DAYS = 90                 # route_log rows older than this are deleted (once a day)
NAMES_TTL_S = 300              # the people list is read again after this
BREAKER_FAILS = 3              # this many local failures within BREAKER_WINDOW_S ...
BREAKER_WINDOW_S = 600
BREAKER_OPEN_S = 600           # ... switch the local route off for this long (voice recipe 13)
RECENT = 40


class Router:
    """Decides every model call and writes the decision down. Never raises: if the router itself breaks, the call
    goes to the subscription as it always did, with 'router error' as the reason."""

    def __init__(self, engine, config_dir: Path | None, *, names: Callable[[], Iterable[str]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.engine = engine
        self.config_dir = config_dir
        self.rules = load_rules(config_dir)
        self._names_fn = names
        self._names: frozenset[str] = frozenset()
        self._names_at = float("-inf")
        self._clock = clock
        self._lock = threading.Lock()
        self._fails: deque[float] = deque()
        self._open_until = float("-inf")
        self._pruned_on: dt.date | None = None

    def reload(self) -> None:
        self.rules = load_rules(self.config_dir)

    # -- the local model's breaker

    def local_failed(self) -> None:
        with self._lock:
            now = self._clock()
            self._fails.append(now)
            while self._fails and now - self._fails[0] > BREAKER_WINDOW_S:
                self._fails.popleft()
            if len(self._fails) >= BREAKER_FAILS:
                self._open_until = now + BREAKER_OPEN_S
                self._fails.clear()

    def fails(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(1 for f in self._fails if now - f <= BREAKER_WINDOW_S)

    def breaker_open(self) -> bool:
        with self._lock:
            return self._clock() < self._open_until

    # -- one decision

    def route(self, purpose: str, text: str | None = "", *, mode: str = "job", model: str | None = None, tools: bool = False,
              images: bool = False, claude_hold: str = "", call_open: bool = False, local_ready: bool = False) -> Decision:
        """text is scanned and never stored. model: Claude's alias for this purpose (models.pick)."""
        try:
            with Session(self.engine) as s:
                if not flags.on(s, FLAG_ROUTER):
                    return self._plain(purpose, mode, model, "g11.router is off: the subscription, as always")
                klass, hits = scan(text or "", self._name_set())
                d = decide(self.rules, purpose, content=klass, mode=mode, model=model, tools=tools, images=images,
                           claude_hold=claude_hold, call_open=call_open, local_flag=flags.on(s, FLAG_LOCAL),
                           local_ready=local_ready and not self.breaker_open(), hits=hits)
                self._write(s, d)
                return d
        except Exception as exc:       # the router never stops a call
            log.warning("router failed: %s", exc, exc_info=True)
            return self._plain(purpose, mode, model, f"router error ({type(exc).__name__}): the subscription, as always")

    def escalate(self, d: Decision, why: str, *, model: str | None = None, claude_hold: str = "") -> Decision:
        """The route's answer was refused (local: not valid, too unsure, too slow): the next route, logged as a second
        decision with where it came from. Counts toward the local breaker."""
        if d.route == "local":
            self.local_failed()
        try:
            with Session(self.engine) as s:
                if not flags.on(s, FLAG_ROUTER):
                    return self._plain(d.purpose, d.mode, model, "g11.router is off: the subscription, as always")
                d2 = decide(self.rules, d.purpose, content=d.content, mode=d.mode, model=model, claude_hold=claude_hold,
                            hits=d.hits, exclude=frozenset({d.route}), fallback_from=d.route, fallback_why=why[:80])
                self._write(s, d2)
                return d2
        except Exception as exc:
            log.warning("router failed: %s", exc, exc_info=True)
            return self._plain(d.purpose, d.mode, model, f"router error ({type(exc).__name__}): the subscription, as always")

    def _plain(self, purpose: str, mode: str, model: str | None, reason: str) -> Decision:
        return Decision("claude", "R1", purpose, mode, ceiling_of(purpose), reason, model=model, rules=self.rules is not None)

    def _name_set(self) -> frozenset[str]:
        now = self._clock()
        if self._names_fn is not None and now - self._names_at > NAMES_TTL_S:
            try:
                self._names = name_tokens(self._names_fn())
            except Exception:
                log.warning("router: the people list could not be read", exc_info=True)
            self._names_at = now
        return self._names

    def _write(self, s: Session, d: Decision) -> None:
        s.add(RouteLog(purpose=d.purpose[:40], mode=d.mode, content=d.content, hits=",".join(d.hits) or None, route=d.route,
                       tier=d.tier, model=d.model, reason=d.reason[:300], fallback_from=d.fallback_from))
        today = utcnow().date()
        if self._pruned_on != today:
            s.exec(delete(RouteLog).where(RouteLog.ts < utcnow() - dt.timedelta(days=KEEP_DAYS)))
            self._pruned_on = today
        s.commit()

    # -- More -> Usage

    def view(self, days: int = 7) -> dict:
        """What the owner sees: the rules in force, and the routes taken with their reasons (newest first)."""
        with Session(self.engine) as s:
            router_on, local_on = flags.on(s, FLAG_ROUTER), flags.on(s, FLAG_LOCAL)
            rows = s.exec(select(RouteLog).where(RouteLog.ts >= utcnow() - dt.timedelta(days=days))
                          .order_by(RouteLog.id.desc())).all()
        by_route: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for r in rows:
            by_route[r.route] = by_route.get(r.route, 0) + 1
            if r.tier:
                by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
        rules = self.rules
        providers = []
        for name, p in (rules.providers if rules else {"claude": Provider("claude", "R1", True)}).items():
            if not p.enabled:
                status = "off: switched off in routing.yaml"
            elif name not in ADAPTERS:
                status = "off: no adapter in this version"
            elif name == "local" and not local_on:
                status = "off: g11.local_route is off"
            elif name == "local" and not (rules and rules.local_purposes and rules.lab_model_hash):
                status = "off: no purpose has passed the router lab"
            else:
                status = "on"
            providers.append({"name": name, "tier": p.tier, "enabled": p.enabled, "status": status})
        return {
            "router_on": router_on, "local_on": local_on, "rules_loaded": rules is not None,
            "problems": list(rules.problems) if rules else [], "providers": providers,
            "local": {"purposes": sorted(rules.local_purposes) if rules else [],
                      "lab_model_hash_set": bool(rules and rules.lab_model_hash), "could_run": sorted(LOCAL_ELIGIBLE)},
            "ceilings": dict(sorted(CEILINGS.items())), "days": days, "total": len(rows), "by_route": by_route,
            "by_tier": by_tier,
            "recent": [{"ts": r.ts.isoformat(), "purpose": r.purpose, "mode": r.mode, "content": r.content,
                        "hits": r.hits.split(",") if r.hits else [], "route": r.route, "tier": r.tier, "model": r.model,
                        "reason": r.reason, "fallback_from": r.fallback_from} for r in rows[:RECENT]],
        }
