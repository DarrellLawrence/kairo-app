"""Phase 3 life data: shopping list, chores, people, money (read only), health.

All of it lives in Kairo's own tables on kairo-home. Inputs: the app, iPhone
Shortcuts (Apple Pay Wallet "Transaction" automation, a daily Health export),
UBS e-banking camt.053 files, and the model's tier 0-1 tools. Kairo can never
pay or move money: there is no tool for it (tier 5).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from sqlmodel import Session, func, select

from .db import Chore, HealthDay, Person, ShoppingItem, Transaction, WishItem, utcnow

MAX_CAMT = 10 * 1024 * 1024

CATEGORIES = {
    # v8 E: before groceries, so a station kiosk or "Coop Pronto" counts as a snack, not the weekly shop
    "snacks": ["kiosk", "avec", "coop pronto", "migrolino", "selecta", "valora", "brezelkönig", "spar express"],
    "groceries": ["migros", "coop", "denner", "aldi", "lidl", "spar", "volg", "manor food", "landi"],
    "transport": ["sbb", "cff", "zvv", "bls", "postauto", "tpg", "mobility", "uber", "bolt", "tankstelle", "shell", "avia", "bp "],
    "eating out": ["restaurant", "mcdonald", "burger king", "starbucks", "cafe", "café", "kebab", "pizza", "subway", "takeaway", "bar "],
    "shopping": ["amazon", "galaxus", "digitec", "zalando", "h&m", "zara", "ikea", "interdiscount", "media markt", "decathlon", "ochsner"],
    "subscriptions": ["spotify", "netflix", "apple.com", "icloud", "swisscom", "salt", "sunrise", "wingo", "youtube", "disney"],
    "health": ["apotheke", "pharmacie", "amavita", "sunstore", "helsana", "css", "swica", "sanitas", "visana", "arzt", "zahnarzt"],
    "sport": ["fitness", "gym", "migros fitness", "basefit", "update fitness", "kieser", "sport"],
}


def categorize(merchant: str | None) -> str:
    m = (merchant or "").lower()
    for cat, words in CATEGORIES.items():
        if any(w in m for w in words):
            return cat
    return "other"


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- shopping

def shopping_open(s: Session) -> list[ShoppingItem]:
    return list(s.exec(select(ShoppingItem).where(ShoppingItem.done == False).order_by(ShoppingItem.id)).all())  # noqa: E712


def shopping_add(s: Session, items: list[str], source: str = "app") -> list[ShoppingItem]:
    have = {i.text.lower() for i in shopping_open(s)}
    added = []
    for text in items:
        text = " ".join(str(text).split())[:200]
        if text and text.lower() not in have:
            row = ShoppingItem(text=text, source=source)
            s.add(row)
            added.append(row)
            have.add(text.lower())
    s.commit()
    for r in added:
        s.refresh(r)
    return added


def shopping_toggle(s: Session, item_id: int) -> ShoppingItem:
    row = s.get(ShoppingItem, item_id)
    if row is None:
        raise KeyError(item_id)
    row.done = not row.done
    row.done_at = utcnow() if row.done else None
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def shopping_clear_done(s: Session) -> int:
    rows = s.exec(select(ShoppingItem).where(ShoppingItem.done == True)).all()  # noqa: E712
    for r in rows:
        s.delete(r)
    s.commit()
    return len(rows)


# ---------------------------------------------------------------- chores

def chores_due(s: Session, today: dt.date) -> list[Chore]:
    return [c for c in s.exec(select(Chore)).all() if c.last_done is None or c.last_done + dt.timedelta(days=c.every_days) <= today]


def chore_done(s: Session, chore_id: int, today: dt.date) -> Chore:
    c = s.get(Chore, chore_id)
    if c is None:
        raise KeyError(chore_id)
    c.last_done = today
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def chore_dict(c: Chore, today: dt.date) -> dict:
    nxt = (c.last_done + dt.timedelta(days=c.every_days)) if c.last_done else today
    return {"id": c.id, "title": c.title, "every_days": c.every_days, "last_done": c.last_done.isoformat() if c.last_done else None,
            "next": nxt.isoformat(), "due": nxt <= today}


# ---------------------------------------------------------------- people

def person_find(s: Session, name: str) -> Person | None:
    return s.exec(select(Person).where(func.lower(Person.name) == name.strip().lower())).first()


def people_list(s: Session, name: str | None = None) -> list[Person]:
    q = select(Person).order_by(Person.name)
    if name:
        q = q.where(func.lower(Person.name).contains(name.strip().lower()))
    return list(s.exec(q).all())


def people_note(s: Session, *, name: str, note: str = "", contacted: bool = False, relation: str | None = None,
                today: dt.date, email: str | None = None) -> Person:
    p = person_find(s, name)
    if p is None:
        p = Person(name=" ".join(name.split())[:80])
    if relation:
        p.relation = relation[:60]
    if email:
        p.email = email.strip().lower()[:200]
    if note.strip():
        line = f"{today.isoformat()}: {note.strip()}"
        p.notes = f"{p.notes}\n{line}" if p.notes else line
        p.notes = p.notes[-20_000:]
    if contacted:
        p.last_contact = today
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def wish_add(s: Session, *, person: str, title: str, price_chf: float | None = None, url: str | None = None,
             note: str | None = None, source: str = "chat", capture_id: int | None = None) -> WishItem:
    """A gift idea on that person's wishlist (v8 B). The same link or title for the same person is kept once."""
    p = person_find(s, person)
    if p is None:
        p = Person(name=" ".join(person.split())[:80])
        s.add(p)
        s.commit()
        s.refresh(p)
    title = " ".join(title.split())[:200]
    for w in s.exec(select(WishItem).where(WishItem.person_id == p.id).where(WishItem.status == "open")).all():
        if (url and w.url == url) or w.title.lower() == title.lower():
            w.price_chf = price_chf if price_chf is not None else w.price_chf
            w.url = url or w.url
            s.add(w)
            s.commit()
            s.refresh(w)
            return w
    w = WishItem(person_id=p.id, title=title, price_chf=price_chf, url=url, note=(note or "").strip()[:1000] or None,
                 source=source, capture_id=capture_id)
    s.add(w)
    s.commit()
    s.refresh(w)
    return w


def wishlist(s: Session, person_id: int) -> list[WishItem]:
    return list(s.exec(select(WishItem).where(WishItem.person_id == person_id).where(WishItem.status == "open")
                       .order_by(WishItem.id)).all())


def wish_dict(w: WishItem) -> dict:
    return {"id": w.id, "title": w.title, "price_chf": w.price_chf, "url": w.url, "note": w.note, "source": w.source}


def next_birthday(b: dt.date, today: dt.date) -> dt.date:
    try:
        nb = b.replace(year=today.year)
    except ValueError:   # 29 February
        nb = dt.date(today.year, 3, 1)
    if nb < today:
        try:
            nb = b.replace(year=today.year + 1)
        except ValueError:
            nb = dt.date(today.year + 1, 3, 1)
    return nb


def person_dict(p: Person, today: dt.date, s: Session | None = None) -> dict:
    nb = next_birthday(p.birthday, today) if p.birthday else None
    overdue = bool(p.contact_every_days and (p.last_contact is None or (today - p.last_contact).days > p.contact_every_days))
    return {"id": p.id, "name": p.name, "relation": p.relation, "birthday": p.birthday.isoformat() if p.birthday else None,
            "birthday_in_days": (nb - today).days if nb else None, "notes": p.notes, "contact_every_days": p.contact_every_days,
            "last_contact": p.last_contact.isoformat() if p.last_contact else None, "reach_out": overdue,
            "email": p.email, "wishlist": [wish_dict(w) for w in wishlist(s, p.id)] if s is not None else []}


def people_reminders(s: Session, today: dt.date, days: int = 7) -> dict:
    out = {"birthdays": [], "reach_out": []}
    for p in s.exec(select(Person)).all():
        d = person_dict(p, today)
        if d["birthday_in_days"] is not None and d["birthday_in_days"] <= days:
            out["birthdays"].append({"name": p.name, "in_days": d["birthday_in_days"]})
        if d["reach_out"]:
            out["reach_out"].append({"name": p.name, "last_contact": d["last_contact"]})
    out["birthdays"].sort(key=lambda x: x["in_days"])
    return out


# ---------------------------------------------------------------- money (read only)

def add_transaction(s: Session, *, ts: dt.datetime, amount: float, currency: str = "CHF", merchant: str | None,
                    card: str | None = None, source: str = "wallet", ref: str | None = None) -> Transaction | None:
    """Stores one payment; None if it was already there."""
    merchant = " ".join((merchant or "").split())[:120] or None
    key = ref or f"{source}|{_aware(ts).isoformat(timespec='minutes')}|{amount:.2f}|{(merchant or '').lower()}"
    dedupe = hashlib.sha256(key.encode()).hexdigest()
    if s.exec(select(Transaction).where(Transaction.dedupe == dedupe)).first():
        return None
    t = Transaction(ts=_aware(ts), amount=round(amount, 2), currency=(currency or "CHF").upper()[:3], merchant=merchant,
                    category=categorize(merchant), source=source, card=(card or "")[:60] or None, dedupe=dedupe)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def parse_amount(value) -> float:
    """'CHF 12.50', '12,50', '-12.50 CHF', 12.5 -> 12.5 (spending is stored negative by the caller)."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:[.,']\d{3})*(?:[.,]\d{1,2})?", str(value or ""))
    if not m:
        raise ValueError("no amount")
    num = m.group(0).replace("'", "")
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".")
    return float(num)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, *path):
    cur = [el]
    for name in path:
        cur = [c for e in cur for c in e if _local(c.tag) == name]
        if not cur:
            return None
    return cur[0]


def import_camt053(s: Session, xml: bytes) -> dict:
    """UBS e-banking export (ISO 20022 camt.053, any version). Returns counts."""
    if len(xml) > MAX_CAMT:
        raise ValueError("file too big")
    if b"<!DOCTYPE" in xml[:2000] or b"<!ENTITY" in xml:
        raise ValueError("not a bank statement")   # no XML entity tricks
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"not readable XML: {exc}") from exc
    added = skipped = 0
    for ntry in (e for e in root.iter() if _local(e.tag) == "Ntry"):
        amt = _find(ntry, "Amt")
        ind = _find(ntry, "CdtDbtInd")
        day = None
        for path in (("BookgDt", "Dt"), ("ValDt", "Dt"), ("BookgDt", "DtTm")):   # Elements without children are falsy: no `or`
            day = _find(ntry, *path)
            if day is not None:
                break
        if amt is None or ind is None or day is None:
            skipped += 1
            continue
        amount = float(amt.text) * (-1 if ind.text == "DBIT" else 1)
        name = None
        for path in (("NtryDtls", "TxDtls", "RltdPties", "Cdtr", "Nm"), ("NtryDtls", "TxDtls", "RltdPties", "Cdtr", "Pty", "Nm"),
                     ("NtryDtls", "TxDtls", "RltdPties", "Dbtr", "Nm"), ("NtryDtls", "TxDtls", "RltdPties", "Dbtr", "Pty", "Nm"),
                     ("NtryDtls", "TxDtls", "RmtInf", "Ustrd"), ("AddtlNtryInf",)):
            el = _find(ntry, *path)
            if el is not None and (el.text or "").strip():
                name = el.text.strip()
                break
        ref_el = _find(ntry, "AcctSvcrRef")
        ts = dt.datetime.fromisoformat(day.text[:19]) if "T" in day.text else dt.datetime.combine(dt.date.fromisoformat(day.text[:10]), dt.time(12))
        ref = f"camt|{ref_el.text}" if ref_el is not None and ref_el.text else None
        row = add_transaction(s, ts=ts, amount=amount, currency=amt.get("Ccy") or "CHF", merchant=name, source="camt", ref=ref)
        if row is None:
            skipped += 1
        else:
            added += 1
    return {"added": added, "skipped": skipped}


def money_summary(s: Session, days: int, category: str | None = None, now: dt.datetime | None = None) -> dict:
    now = now or utcnow()
    since = now - dt.timedelta(days=days)
    q = select(Transaction).where(Transaction.ts >= since).order_by(Transaction.ts.desc())
    if category:
        q = q.where(Transaction.category == category)
    rows = s.exec(q).all()
    out_rows = [t for t in rows if t.amount < 0 and t.currency == "CHF"]
    by_cat: dict[str, float] = defaultdict(float)
    for t in out_rows:
        by_cat[t.category] += -t.amount
    merchants = Counter()
    for t in out_rows:
        merchants[t.merchant or "?"] += -t.amount
    return {"days": days, "spent_chf": round(sum(by_cat.values()), 2),
            "income_chf": round(sum(t.amount for t in rows if t.amount > 0 and t.currency == "CHF"), 2),
            "by_category": {k: round(v, 2) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
            "top_merchants": [{"merchant": m, "chf": round(v, 2)} for m, v in merchants.most_common(5)],
            "recent": [{"id": t.id, "ts": _aware(t.ts).isoformat(), "amount": t.amount, "currency": t.currency,
                        "merchant": t.merchant, "category": t.category, "source": t.source} for t in rows[:30]]}


# ---------------------------------------------------------------- health

HEALTH_FIELDS = {"steps": int, "sleep_h": float, "resting_hr": int, "active_kcal": int, "workout_min": int, "weight_kg": float}
HEALTH_LIMITS = {"steps": (0, 200_000), "sleep_h": (0, 24), "resting_hr": (20, 250), "active_kcal": (0, 20_000),
                 "workout_min": (0, 1440), "weight_kg": (20, 400)}


def health_put(s: Session, day: dt.date, values: dict) -> HealthDay:
    row = s.get(HealthDay, day) or HealthDay(date=day)
    for key, typ in HEALTH_FIELDS.items():
        if values.get(key) in (None, ""):
            continue
        try:
            v = typ(parse_amount(values[key]) if isinstance(values[key], str) else values[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key}: not a number")
        lo, hi = HEALTH_LIMITS[key]
        if not lo <= v <= hi:
            raise ValueError(f"{key}: {v} is out of range")
        setattr(row, key, v)
    row.updated_at = utcnow()
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def health_summary(s: Session, days: int, today: dt.date) -> dict:
    rows = s.exec(select(HealthDay).where(HealthDay.date > today - dt.timedelta(days=days)).order_by(HealthDay.date)).all()

    def avg(key):
        vals = [getattr(r, key) for r in rows if getattr(r, key) is not None]
        return round(statistics.mean(vals), 1) if vals else None

    return {"days": days, "avg": {k: avg(k) for k in HEALTH_FIELDS},
            "daily": [{"date": r.date.isoformat(), **{k: getattr(r, k) for k in HEALTH_FIELDS}} for r in rows]}
