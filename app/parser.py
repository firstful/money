"""Generic CSV transaction parser with header/alias auto-detection.

Design: no pandas/numpy — pure stdlib. Works on any bank/CC/merchant export by
matching column headers against canonical aliases, then resolving an amount + a
direction (in/out). Source *profiles* provide per-issuer hints; auto-detection
falls back gracefully so an unknown format still yields canonical rows.

Canonical row:
    {
        "date": "yyyy-mm-dd",
        "amount": float,          # signed: negative = money out, positive = money in
        "direction": "in"|"out",
        "description": str,       # normalized merchant/payee/description
        "category": str,          # may be ""
        "source": str,            # account/label the user assigned
        "raw_date": str,
        "raw_amount": str,
    }
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Canonical fields + the column headers that map to each.
# ---------------------------------------------------------------------------

# canonical field -> list of header aliases (lowercased, stripped)
ALIASES: dict[str, list[str]] = {
    "date": [
        "date", "transaction date", "post date", "posted date", "posting date",
        "payment date", "order date", "trade date", "trans date", "activity date",
        "transaction_date", "posted", "transaction date & time", "date posted",
        "invoice date", "settlement date",
    ],
    "amount": [
        "amount", "total", "order total", "payment amount", "invoice amount",
        "transaction amount", "amt", "amount due", "total amount", "charge",
        "charges", "amount (usd)", "amount in usd", "amount  ", "value", "sum",
    ],
    "debit": [
        "debit", "withdrawal", "debit amount", "withdrawals", "money out",
        "debited", "debit/credit", "payment", "withdrawals  ",
    ],
    "credit": [
        "credit", "deposit", "credit amount", "deposits", "money in", "credited",
        "credits", "refund", "deposit amount",
    ],
    "description": [
        "description", "merchant", "payee", "payee name", "name", "memo",
        "vendor", "narration", "details", "transaction description",
        "merchant name", "payee/merchant", "store name", "remark", "particulars",
        "transaction", "payee/description", "memo/description", "description",
    ],
    "category": [
        "category", "spend category", "merchant category", "expense category",
        "category (merchant)", "category  ", "category/type", "budget category",
    ],
    # `type` is deliberately NOT a category alias — in many exports it means
    # Debit/Credit/Payment (a direction signal), not a spend category.
    "type": [
        "type", "transaction type", "txn type", "payment type", "action",
        "transaction kind",
    ],
    "balance": ["balance", "running balance", "account balance", "current balance"],
    "txid": ["transaction id", "txn id", "id", "reference", "reference no", "order #", "order no", "invoice #", "check no", "check #"],
}

# ---------------------------------------------------------------------------
# Direction-carrying words that appear in a `type` / `action` column.
# ---------------------------------------------------------------------------
_OUT_WORDS = {
    "payment", "debit", "withdrawal", "withdrawal/purchase", "purchase", "sale",
    "fee", "service fee", "atm", "charge", "online transfer out", "transfer out",
    "card payment", "purchasepayment", "check", "debit card", "withdraw",
    "payment/courtesy pay", "returned", "pos", "outgoing", "expense",
}
_IN_WORDS = {
    "credit", "deposit", "refund", "interest", "interest earned", "transfer in",
    "online transfer in", "reversal", "adjustment credit", "income", "incoming",
    "redeem", "reward", "cash back",
}

# ---------------------------------------------------------------------------
# Date formats to try, in order.
# ---------------------------------------------------------------------------
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%b-%d-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%Y.%m.%d",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %H:%M",
]

_DATE_TOKEN = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,}\s+\d{2,4}|[A-Za-z]{3,}\s+\d{1,2},?\s+\d{2,4}")


def parse_amount(raw: str) -> Optional[float]:
    """Parse an amount string to float (signed). Handles $ , ( ), -suffix, DR/CR."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    neg = False
    up = s.upper()
    # trailing DR / CR (bank bookkeeping)
    if up.endswith("DR"):
        neg, s = True, s[:-2]
    elif up.endswith("CR"):
        s = s[:-2]
    # trailing minus or parentheses = negative
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace(",", "").replace("$", "").replace(" ", "").replace("\u00a0", "")
    if s in ("", "-", "--"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(raw: str) -> Optional[str]:
    """Parse a date string to ISO yyyy-mm-dd, or None if unrecognized."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # strip a time-of-day component and timezone-ish suffixes
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", str(h).strip().lower())


def detect_delimiter(text: str) -> str:
    """Detect the delimiter of a CSV-ish blob. Fallback: comma."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        return dialect.delimiter
    except Exception:
        # count candidate delimiters outside of quotes
        best, best_count = ",", 0
        for d in (",", "\t", ";", "|"):
            c = sample.count(d)
            if c > best_count:
                best, best_count = d, c
        return best


def _read_rows(text: str, delimiter: str) -> list[list[str]]:
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delimiter):
        # normalize ragged rows; skip fully-empty
        if not r or all(not c.strip() for c in r):
            continue
        rows.append([c.strip() for c in r])
    return rows


def _pick_header(rows: list[list[str]]) -> tuple[Optional[int], dict[str, int]]:
    """Find the header row and map canonical field -> column index.

    Scores each of the first 30 rows by how many cells match a canonical alias.
    Returns (header_row_index, colmap). colmap only has entries that matched.
    """
    # build alias lookup: normalized header -> canonical field
    alias_lookup: dict[str, str] = {}
    for field, headers in ALIASES.items():
        for h in headers:
            alias_lookup[_norm_header(h)] = field

    best_idx, best_score, best_map = None, -1, {}
    for i, row in enumerate(rows[:30]):
        score = 0
        colmap: dict[str, int] = {}
        for ci, cell in enumerate(row):
            f = alias_lookup.get(_norm_header(cell))
            if f and f not in colmap:
                colmap[f] = ci
                score += 1
        if score > best_score:
            best_idx, best_score, best_map = i, score, colmap
    if best_idx is None or best_score <= 0:
        return None, {}
    return best_idx, best_map


def _resolve_amount(colmap: dict[str, int], row: list[str]) -> tuple[Optional[float], str]:
    """Resolve a signed amount from a row given the available mapped columns.

    Priority: explicit `amount` col -> debit/credit cols -> type-word direction.
    Returns (signed_amount, how_it_was_resolved).
    """
    def val(field):
        ci = colmap.get(field)
        if ci is None or ci >= len(row):
            return None
        return row[ci]

    amt_raw = val("amount")
    amt = parse_amount(amt_raw) if amt_raw is not None else None
    if amt is not None:
        return amt, "amount"

    debit_raw, credit_raw = val("debit"), val("credit")
    debit = parse_amount(debit_raw) if debit_raw is not None else None
    credit = parse_amount(credit_raw) if credit_raw is not None else None
    if debit is not None or credit is not None:
        if debit:
            return -debit, "debit"
        return credit, "credit"

    typ = (val("type") or "").strip().lower()
    if typ:
        if typ in _OUT_WORDS:
            amt = parse_amount(val("amount") or "")
            # some exports only give the amount numeric in a debit/credit col we missed
            return None, "type-out"
    return None, "none"


def _clean_description(descs: list[str]) -> str:
    seen = {}
    parts = []
    for d in descs:
        d = (d or "").strip()
        if not d or d.lower() in seen:
            continue
        seen[d.lower()] = True
        parts.append(d)
    return " / ".join(parts)


def csv_to_transactions(
    text: str,
    source: str = "",
    profile: Optional[dict] = None,
    all_spend: bool = False,
) -> list[dict]:
    """Parse CSV text into canonical transaction rows.

    source      : label for the account/export (e.g. "Huntington Checking").
    profile     : optional per-issuer overrides.
    all_spend   : treat every row as an outflow (merchant order-history exports).
    """
    profile = profile or {}
    delimiter = detect_delimiter(text)
    rows = _read_rows(text, delimiter)
    if not rows:
        raise ValueError("empty file")

    header_idx, colmap = _pick_header(rows)
    if header_idx is None:
        raise ValueError(
            "could not detect headers in this file — recognized columns: "
            + ", ".join(sorted(set(ALIASES) - {"type", "balance", "txid"}))
        )

    # profile can remap aliases: merge into a per-call alias lookup
    alias_lookup: dict[str, str] = {}
    for field, headers in ALIASES.items():
        for h in headers:
            alias_lookup[_norm_header(h)] = field
    for field, headers in (profile.get("aliases") or {}).items():
        for h in headers:
            alias_lookup[_norm_header(h)] = field
    # rebuild colmap honoring profile additions
    rebuilt = {}
    for ci, cell in enumerate(rows[header_idx]):
        f = alias_lookup.get(_norm_header(cell))
        if f and f not in rebuilt:
            rebuilt[f] = ci
    colmap = rebuilt

    if "date" not in colmap or ("amount" not in colmap and "debit" not in colmap and "credit" not in colmap):
        raise ValueError(
            "headers detected but missing required columns "
            f"(date={colmap.get('date')}, amount cols={[k for k in ('amount','debit','credit') if k in colmap]})"
        )

    # which columns carry description-ish text
    desc_fields = [f for f in ("description", "category") if f in colmap]

    out: list[dict] = []
    for row in rows[header_idx + 1 :]:
        date = parse_date(row[colmap["date"]]) if colmap["date"] < len(row) else None
        if not date:
            # skip summarizing/continuation rows that aren't transactions
            continue
        amount, how = _resolve_amount(colmap, row)
        if amount is None:
            continue
        # Some issuers (e.g. Amex) write purchases positive / payments negative —
        # flip to the canonical convention: negative = money out.
        if profile.get("expense_positive"):
            amount = -amount
        if all_spend:
            amount = -abs(amount)

        direction = "in" if amount > 0 else "out"

        desc = ""
        if "description" in colmap:
            desc = row[colmap["description"]] if colmap["description"] < len(row) else ""
        category = ""
        if "category" in colmap:
            category = row[colmap["category"]] if colmap["category"] < len(row) else ""

        out.append({
            "date": date,
            "amount": round(amount, 4),
            "direction": direction,
            "description": desc,
            "category": category,
            "source": source,
            "raw_date": row[colmap["date"]] if colmap["date"] < len(row) else "",
            "raw_amount": how,
        })

    if not out:
        raise ValueError("no parseable transaction rows found")
    return out
