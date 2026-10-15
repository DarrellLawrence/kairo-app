"""Run diagnosis (More -> System): the numbers DIAGNOSIS section G asked for, from the phone.

Read-only: the database is opened with mode=ro plus query_only, files are only read, nothing
calls the model or the network. Secrets are redacted (redact()) before the text leaves this
module, so the owner can copy or share it as is. The updater's own log and state stay in
C:\\Kairo\\updater (admins only): Kairo's account can't read them, the report says so.
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import re
import sqlite3
import sys
import time
from pathlib import Path

from sqlalchemy.engine import make_url

DAYS = 14
LOG_PATTERNS = ["quiet hours: no push for", "push failed", "gave up waiting", "maximum number of running instances",
                "Whisper", "usage_limited", "logged_out", "GoogleNotConnected", "Traceback"]
LOG_TAIL = 15
PHONE_TAIL = 25

# Order matters: specific token shapes first, then key=value secrets, then anything long and opaque.
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "<redacted key>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "<redacted jwt>"),
    (re.compile(r"\bya29\.[\w.-]+"), "<redacted google token>"),
    (re.compile(r"\b1//[\w-]{20,}"), "<redacted google token>"),
    (re.compile(r"\bGOCSPX-[\w-]+"), "<redacted>"),
    (re.compile(r"\bAIza[\w-]{30,}"), "<redacted>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<redacted>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}"), "<redacted>"),
    (re.compile(r"\btskey-[\w-]+"), "<redacted>"),
    (re.compile(r"\b\d{6}(?:-\d{6}){7}\b"), "<redacted recovery key>"),            # BitLocker
    (re.compile(r"(?i)([?&](?:code|state|token|key|sig|signature|auth|access_token|refresh_token)=)[^&\s\"']+"),
     r"\1<redacted>"),
    (re.compile(r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|credential|"
                r"private[_-]?key|p256dh|auth)[\w.-]*)(\"?'?\s*[:=]\s*\"?'?)(?!<|(?i:bearer)\s)([^\s\"',;}\]]+)"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)(https://[\w.-]*(?:push|notify|fcm|wns)[\w.-]*/)\S+"), r"\1<redacted>"),   # push endpoints
    (re.compile(r"(?<![\w+-])(?=[\w+-]*\d)(?=[\w+-]*[A-Za-z])[\w+-]{32,}={0,2}(?![\w+-])"), "<redacted>"),
]
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d ()-]{7,}\d\b|\b0\d{2} ?\d{3} ?\d{2} ?\d{2}\b")
_TAILNET = re.compile(r"\b([\w-]+)\.([\w-]+)\.ts\.net\b")


def redact(text: str) -> str:
    """Secrets out; addresses and phone numbers masked; the tailnet name hidden."""
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _PHONE.sub("<phone>", text)
    return _TAILNET.sub(r"\1.<tailnet>.ts.net", text)


def db_path(db_url: str) -> Path | None:
    url = make_url(db_url)
    return Path(url.database) if url.get_backend_name() == "sqlite" and url.database else None


def connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only, even by mistake: SQLite refuses every write on this connection."""
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only = ON")
    return c


def _t(x) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(x).replace("Z", "")[:26]) if x else None
    except ValueError:
        return None


def _pct(v: list[float], p: float):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 1) if v else None


class _Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def h(self, title: str) -> None:
        self.lines.append(f"\n== {title}")

    def add(self, *parts) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def rows(self, rows: list) -> None:
        if not rows:
            self.add("  (none)")
        for r in rows:
            self.add("  " + " | ".join("" if v is None else str(v) for v in r))


def _database(r: _Report, c: sqlite3.Connection, since: str) -> None:
    def q(sql: str, *a) -> list:
        try:
            return c.execute(sql, a).fetchall()
        except sqlite3.Error as e:
            return [("ERROR", str(e))]

    def one(sql: str, *a):
        rows = q(sql, *a)
        return " ".join(map(str, rows[0])) if rows and rows[0][0] == "ERROR" else (rows[0][0] if rows else None)

    def failed(rows: list) -> bool:
        if rows and rows[0][0] == "ERROR":
            r.rows(rows)
            return True
        return False

    r.h(f"G.1 approvals by status ({DAYS} d): status | n | from loop Send")
    r.rows(q("SELECT status, COUNT(*), COALESCE(SUM(reason LIKE 'Open loop%'), 0) FROM approvals WHERE created_at>=? GROUP BY status", since))
    r.h(f"G.2 open loops created ({DAYS} d): kind | source | status | n")
    r.rows(q("SELECT kind, source, status, COUNT(*) FROM open_loops WHERE created_at>=? GROUP BY 1,2,3 ORDER BY 4 DESC", since))
    r.add("close reasons:")
    r.rows(q("SELECT close_reason, COUNT(*) FROM open_loops WHERE closed_at>=? GROUP BY 1", since))
    r.add("open now (most raised first): kind | who | what | raised | snoozed")
    r.rows(q("SELECT kind, who, substr(what,1,70), raised, snoozed FROM open_loops WHERE status='open' "
             "ORDER BY raised DESC LIMIT 25"))
    r.h(f"G.2a loops checked before shown (Sonnet sanity check, {DAYS} d): verdict | why | n")
    r.rows(q("SELECT json_extract(check_json,'$.verdict'), json_extract(check_json,'$.why'), COUNT(*) FROM open_loops "
             "WHERE check_json IS NOT NULL AND json_extract(check_json,'$.verdict') IS NOT NULL "
             "AND COALESCE(closed_at, checked_at, created_at)>=? GROUP BY 1,2 ORDER BY 3 DESC", since))
    r.add("dropped by the check: kind | who | reason")
    r.rows(q("SELECT kind, who, substr(close_reason, 8, 140) FROM open_loops WHERE close_reason LIKE 'check:%' "
             "AND closed_at>=? ORDER BY closed_at DESC LIMIT 30", since))
    r.add("check failed (model down): n")
    r.rows(q("SELECT COUNT(*) FROM open_loops WHERE status='open' AND json_extract(check_json,'$.error') IS NOT NULL"))
    r.h("G.2b calendar truth")
    last = one("SELECT value FROM settings WHERE key='calendar.last_read'")
    try:
        lr = json.loads(last) if last else None
    except ValueError:
        lr = None
    if lr is None:
        r.add("last calendar read: none yet")
    else:
        state = "ok" if lr.get("ok") else "FAILED"
        r.add(f"last calendar read: {state} ({lr.get('calendars')} calendars, {lr.get('events')} events, "
              f"{lr.get('days')} days, at {lr.get('at')}){' ' + str(lr.get('error')) if lr.get('error') else ''}")
    tt = one("SELECT value FROM settings WHERE key='timetable.check'")
    try:
        t = json.loads(tt) if tt else None
    except ValueError:
        t = None
    if t is None:
        r.add("timetable: not checked yet")
    else:
        extra = f"; missing: {', '.join(t.get('missing') or [])}" if t.get("missing") else ""
        extra += f"; not FIXED: {', '.join(t.get('not_fixed') or [])}" if t.get("not_fixed") else ""
        r.add(f"timetable: {t.get('state')} ({t.get('present', 0)} weekly blocks in the calendar, until "
              f"{t.get('until') or '-'}){extra} (at {t.get('at')})")

    r.h(f"G.3 job runtimes ({DAYS} d): job | status | n | p50 s | p95 s")
    agg: dict[tuple, list[float]] = {}
    rows = q("SELECT job, status, started_at, finished_at FROM job_runs WHERE started_at>=?", since)
    for row in [] if failed(rows) else rows:
        a, b = _t(row[2]), _t(row[3])
        if a and b:
            agg.setdefault((row[0], row[1]), []).append((b - a).total_seconds())
    r.rows([(*k, len(v), _pct(v, .5), _pct(v, .95)) for k, v in sorted(agg.items())])
    r.add("errors: job | error | n")
    r.rows(q("SELECT job, substr(error,1,120), COUNT(*) FROM job_runs WHERE started_at>=? AND status='error' "
             "GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20", since))

    r.h(f"G.4 pushes and prompts ({DAYS} d)")
    r.add("every push (the notification boss): source | sent / held / dropped | n")
    r.rows(q("SELECT source, status, COUNT(*) FROM push_log WHERE ts>=? GROUP BY 1,2 ORDER BY 1,2", since))
    r.add("held or dropped, why | n")
    r.rows(q("SELECT why, COUNT(*) FROM push_log WHERE ts>=? AND status!='sent' GROUP BY 1 ORDER BY 2 DESC LIMIT 10", since))
    r.add("proactive pushes sent per day:")
    r.rows(q("SELECT substr(ts,1,10), COUNT(*) FROM push_log WHERE ts>=? AND status='sent' "
             "AND source IN ('loops','coach','location','lerndoku') GROUP BY 1", since))
    r.add("reviews:")
    r.rows(q("SELECT kind, COUNT(*) FROM reviews WHERE created_at>=? GROUP BY 1", since))
    r.add("calls: who started it | reason | status | n")
    r.rows(q("SELECT kind, reason, status, COUNT(*) FROM calls WHERE created_at>=? GROUP BY 1,2,3", since))
    r.add("briefs ok:", one("SELECT COUNT(*) FROM job_runs WHERE job='morning_brief' AND status='ok' AND started_at>=?", since))
    r.add("suspicious mails:", one("SELECT COUNT(*) FROM email_triage WHERE suspicious=1 AND triaged_at>=?", since))
    r.add("captures asking:", one("SELECT COUNT(*) FROM captures WHERE status='asking' AND created_at>=?", since))

    r.h(f"G.5 model runs ({DAYS} d): purpose | model | n | failed | p50 ms | p95 ms | tokens | usd")
    runs: dict[tuple, dict] = {}
    rows = q("SELECT purpose, model, ok, duration_ms, COALESCE(input_tokens,0)+COALESCE(output_tokens,0), "
             "COALESCE(cost_usd,0) FROM model_runs WHERE ts>=?", since)
    for row in [] if failed(rows) else rows:
        p, m, ok, d, tok, usd = row
        a = runs.setdefault((p, m), {"n": 0, "fail": 0, "d": [], "tok": 0, "usd": 0.0})
        a["n"] += 1
        a["fail"] += not ok
        a["tok"] += tok or 0
        a["usd"] += usd or 0
        if d:
            a["d"].append(d)
    r.rows([(*k, a["n"], a["fail"], _pct(a["d"], .5), _pct(a["d"], .95), a["tok"], round(a["usd"], 2))
            for k, a in sorted(runs.items(), key=lambda x: -x[1]["n"])])

    r.h(f"G.5a Claude start and first words ({DAYS} d), ms: purpose | warm | n | start p50 | start p95 | "
        "first words p50 | first words p95")
    starts: dict[tuple, tuple[list, list]] = {}
    rows = q("SELECT purpose, warm, startup_ms, first_text_ms FROM model_runs WHERE ts>=? AND "
             "(startup_ms IS NOT NULL OR first_text_ms IS NOT NULL)", since)
    for p, warm, st, ft in [] if failed(rows) else rows:
        a = starts.setdefault((p, "warm" if warm else "cold"), ([], []))
        if st is not None:
            a[0].append(st)
        if ft is not None:
            a[1].append(ft)
    r.rows([(*k, max(len(a[0]), len(a[1])), _pct(a[0], .5), _pct(a[0], .95), _pct(a[1], .5), _pct(a[1], .95))
            for k, a in sorted(starts.items())])

    r.h(f"G.5e voice and chat turns ({DAYS} d), ms: version | path | model | n | played | step | p50 | p95")
    rows = q("SELECT version, path, model, outcome, reason, steps_json FROM voice_turns WHERE ts>=? ORDER BY ts", since)
    turn_steps: dict[tuple, dict[str, list]] = {}
    played: dict[tuple, list] = {}
    fails: dict[str, int] = {}
    for version, path, model, outcome, reason, steps_json in [] if failed(rows) else rows:
        key = (version, path, model or "?")
        played.setdefault(key, []).append(outcome in ("played", "text"))
        if outcome not in ("played", "text"):
            why = f"{path}: {outcome}" + (f" ({reason})" if reason else "")
            fails[why[:160]] = fails.get(why[:160], 0) + 1
        try:
            for step, ms in json.loads(steps_json or "{}").items():
                turn_steps.setdefault(key, {}).setdefault(step, []).append(ms)
        except ValueError:
            continue
    r.rows([(*k, len(played[k]), sum(played[k]), step, _pct(v, .5), _pct(v, .95))
            for k, steps in sorted(turn_steps.items()) for step, v in steps.items()])
    if fails:
        r.add("  not played: " + "; ".join(f"{k} x{n}" for k, n in sorted(fails.items(), key=lambda x: -x[1])))

    r.h(f"G.5d waiting ({DAYS} d), ms: kind | n | p50 | p95")
    rows = q("SELECT kind, ms FROM latency WHERE ts>=?", since)
    waits: dict[str, list] = {}
    for kind, ms in [] if failed(rows) else rows:
        waits.setdefault(kind, []).append(ms)
    r.rows([(k, len(v), _pct(v, .5), _pct(v, .95)) for k, v in sorted(waits.items())])

    r.h("G.5b reply wall time (owner message -> Kairo reply), s: n | p50 | p95")
    chat, call, last = [], [], {}
    rows = q("SELECT thread_id, role, substr(text,1,7)='(spoken', created_at FROM chat_messages "
             "WHERE created_at>=? ORDER BY id", since)
    for tid, role, spoken, ts in [] if failed(rows) else rows:
        if role == "owner":
            last[tid] = (_t(ts), bool(spoken))
        elif role == "kairo" and tid in last:
            t0, was_spoken = last.pop(tid)
            t1 = _t(ts)
            if t0 and t1:
                (call if was_spoken else chat).append((t1 - t0).total_seconds())
    r.add(f"  chat {len(chat)} | {_pct(chat, .5)} | {_pct(chat, .95)}")
    r.add(f"  call {len(call)} | {_pct(call, .5)} | {_pct(call, .95)}")

    r.h("G.5c voice notes, upload -> filed, s: n | p50 | p95")
    vn = []
    rows = q("SELECT created_at, filed_at FROM captures WHERE source IN ('voice_note','app_voice') "
             "AND filed_at IS NOT NULL AND created_at>=?", since)
    for a, b in [] if failed(rows) else rows:
        ta, tb = _t(a), _t(b)
        if ta and tb:
            vn.append((tb - ta).total_seconds())
    r.add(f"  {len(vn)} | {_pct(vn, .5)} | {_pct(vn, .95)}")

    r.h("Health watch (fix plan 32): problem | since | text")
    value = one("SELECT value FROM settings WHERE key='health.state'")
    try:
        hs = json.loads(value) if value and not str(value).startswith("ERROR") else None
    except (TypeError, ValueError):
        hs = None
    if not hs:
        r.add("  not run yet")
    else:
        r.add(f"  checked {hs.get('checked_at')}, network checks {hs.get('full_at')}")
        for k, v in sorted((hs.get("problems") or {}).items()):
            r.add(f"  {k} | {v.get('since')} | {v.get('text')}")

    r.h("Realtime voice (OpenAI; owner's decision 2026-10-15)")
    cfg = one("SELECT value FROM settings WHERE key='realtime.settings'")
    r.add(f"  settings: {cfg or 'defaults'}")
    r.add("  months: month | spend + flags")
    r.rows(q("SELECT substr(key, 16), value FROM settings WHERE key LIKE 'realtime.month.%' ORDER BY key DESC LIMIT 3"))
    r.add(f"  fallbacks to the local voice ({DAYS} d): reason | n")
    r.rows(q("SELECT substr(outcome, 1, instr(outcome || ':', ':') - 1), COUNT(*) FROM audit_log "
             "WHERE action='realtime.fallback' AND ts>=? GROUP BY 1 ORDER BY 2 DESC", since))
    r.add("  last calls: when | model, length, how it ended")
    r.rows(q("SELECT ts, outcome FROM audit_log WHERE action='realtime.call' ORDER BY id DESC LIMIT 10"))

    r.h("Last self-test")
    value = one("SELECT value FROM settings WHERE key='selftest.tools'")
    try:
        st = json.loads(value) if value and not str(value).startswith("ERROR") else None
    except (TypeError, ValueError):
        st = None
    if not st:
        r.add("  not run yet")
    else:
        r.add(f"  {st.get('at')} ok={st.get('ok')} {st.get('seconds')}s reason: {st.get('reason') or '-'}")
        r.add("  checks:", json.dumps(st.get("checks") or {}, sort_keys=True))
        for w in st.get("warnings") or []:
            r.add("  warning:", w)

    r.h("Acceptance (docs/ACCEPTANCE.md, on this PC)")
    value = one("SELECT value FROM settings WHERE key='acceptance.results'")
    try:
        acc = json.loads(value) if value and not str(value).startswith("ERROR") else None
    except (TypeError, ValueError):
        acc = None
    if not acc:
        r.add("  not run yet")
    else:
        last = acc.get("last_run") or {}
        r.add(f"  last run {last.get('at')} on {last.get('version')}: {last.get('passed')}/{last.get('total')} passed "
              f"({last.get('seconds')}s)")
        for tid, res in sorted((acc.get("tests") or {}).items(), key=lambda x: int(x[0])):
            mark = "PASS" if res.get("ok") else "FAIL"
            failed = [k for k, v in (res.get("checks") or {}).items() if not v]
            r.add(f"  {mark} #{tid} ({res.get('by')}, {res.get('version')}): {res.get('detail', '')[:160]}"
                  + (f" | failed: {', '.join(failed)}" if failed else ""))


def _release(r: _Report, app_root: Path, data_dir: Path) -> None:
    r.h("G.6 releases and update state")
    try:
        info = json.loads((app_root / "release.json").read_text(encoding="utf-8"))
        r.add(f"  running: {info.get('version')} (claude {info.get('claude_code_version')}, python {info.get('python')})")
    except (OSError, ValueError):
        r.add(f"  running: {app_root.name} (no release.json: a dev checkout)")
    if app_root.parent.name.lower() == "releases":
        r.add("  installed:", ", ".join(sorted(p.name for p in app_root.parent.iterdir() if p.is_dir())))
    from . import status_files   # fix F24: the installer's files moved to C:\Kairo\status
    written = sorted({status_files.path(data_dir, n) for n in status_files.LEGACY} | set((data_dir / "updates").glob("*.json")))
    for f in written:
        try:
            body = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if f.name == "boot-check.json":
            try:
                b = json.loads(body)
                r.add(f"  boot check {b.get('time')} ({b.get('trigger')}): all_ok={b.get('all_ok')}")
                for ch in b.get("checks") or []:
                    mark = "ok" if ch.get("ok") and ch.get("level") != "warn" else ("warn" if ch.get("ok") else "FAIL")
                    r.add(f"    {mark} {ch.get('name')}: {ch.get('detail')}")
                continue
            except ValueError:
                pass
        r.add(f"  {f.parent.name}/{f.name}: {' '.join(body.split())[:600]}")
    r.add("  updater log and state: C:\\Kairo\\updater (admins only; Kairo's account can't read them)")


def _memory(r: _Report, index: Path) -> None:
    """v8 C6: how much the memory index holds and how it last ran (counts only, never note text)."""
    r.h("Semantic memory (index)")
    if not index.is_file():
        r.add("  no index yet")
        return
    try:
        c = connect_ro(index)
        try:
            n, nv = c.execute("SELECT COUNT(*), COUNT(vec) FROM chunks").fetchone()
            by = c.execute("SELECT source, COUNT(*) FROM docs GROUP BY source ORDER BY source").fetchall()
            last = c.execute("SELECT value FROM meta WHERE key='last'").fetchone()
        finally:
            c.close()
    except sqlite3.Error as e:
        r.add(f"  can't read the index: {e}")
        return
    r.add(f"  {n} pieces, {nv} with meaning ({'keywords + meaning' if nv else 'keywords only'})")
    r.add("  sources: " + (", ".join(f"{src} {k}" for src, k in by) or "none"))
    try:
        info = json.loads(last[0]) if last else {}
        at = dt.datetime.fromtimestamp(info["at"], dt.timezone.utc).isoformat(timespec="seconds") if info.get("at") else "never"
        r.add(f"  last re-index {at}; embedding model on {info.get('device') or 'none'}")
    except (ValueError, TypeError, KeyError, OSError):
        pass


def _logs(r: _Report, log_dir: Path) -> None:
    def age(p: Path) -> int:               # kairo.log.5 is the oldest, kairo.log the newest (rotation)
        suffix = p.name.rsplit(".", 1)[-1]
        return -int(suffix) if suffix.isdigit() else 0
    files = sorted(log_dir.glob("kairo.log*"), key=age)
    r.h(f"Logs: {', '.join(p.name for p in files) or 'none'}")
    counts = {p: 0 for p in LOG_PATTERNS}
    levels: dict[str, int] = {}
    first = last = None
    tail: list[str] = []
    phone: list[str] = []                  # what the iPhone reported (web/src/clientlog.js)
    phone_counts: dict[str, int] = {}
    for f in files:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stamp = line[:19]
                    if len(stamp) == 19 and stamp[4] == "-" and stamp[10] == " ":
                        first = min(first or stamp, stamp)
                        last = max(last or stamp, stamp)
                        level = line[24:].split(" ", 1)[0]
                        levels[level] = levels.get(level, 0) + 1
                        if level in ("ERROR", "CRITICAL", "WARNING"):
                            tail = (tail + [line.rstrip()[:300]])[-LOG_TAIL:]
                    for p in LOG_PATTERNS:
                        if p in line:
                            counts[p] += 1
                    if " kairo.client: " in line:
                        msg = line.split(" kairo.client: ", 1)[1].rstrip()
                        name = msg.split(" ", 1)[0]
                        phone_counts[name] = phone_counts.get(name, 0) + 1
                        phone = (phone + [f"{line[:19]} {msg[:400]}"])[-PHONE_TAIL:]
        except OSError as e:
            r.add(f"  {f.name}: unreadable ({e.__class__.__name__})")
    r.add(f"  span: {first or '-'} .. {last or '-'}")
    r.add("  lines by level:", ", ".join(f"{k} {v}" for k, v in sorted(levels.items())) or "-")
    r.add("  pattern counts:", ", ".join(f"'{k}' {v}" for k, v in counts.items()))
    r.add(f"  last {LOG_TAIL} warnings/errors:")
    for line in tail:
        r.add("    " + line)
    r.h("Phone events (sound, from the iPhone)")
    r.add("  counts:", ", ".join(f"{k} {v}" for k, v in sorted(phone_counts.items())) or "none yet")
    for line in phone:
        r.add("    " + line)


def run(*, db_url: str, data_dir: Path, app_root: Path, now: dt.datetime | None = None) -> dict:
    """The whole report, redacted. Never raises for missing tables, files or logs: it says so instead."""
    started = time.monotonic()
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now.replace(tzinfo=None) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    r = _Report()
    r.add(f"Kairo diagnosis {now.isoformat(timespec='seconds')} (read-only, secrets redacted)")
    r.add(f"python {sys.version.split()[0]} on {platform.system()} {platform.release()}")
    path = db_path(db_url)
    try:
        if path is None or not path.is_file():
            raise sqlite3.OperationalError(f"no database at {path}")
        c = connect_ro(path)
    except sqlite3.Error as e:
        r.h("Database")
        r.add(f"  can't open read-only: {e}")
    else:
        try:
            _database(r, c, since)
        finally:
            c.close()
    _release(r, app_root, data_dir)
    _memory(r, data_dir / "memory" / "index.sqlite")
    _logs(r, data_dir / "logs")
    r.add(f"\n(took {time.monotonic() - started:.1f}s)")
    return {"text": redact("\n".join(r.lines)), "at": now.isoformat(timespec="seconds")}
