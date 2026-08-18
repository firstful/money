"""Best-effort order → Plaid-transaction correlator.

Matches an order (retailer, date, total) against the set of known Plaid
transactions.  There is NO clean foreign key (CC statements don't carry the
order number), so this is a scored fuzzy match:

  candidate = tx on/near order date (window) for the retailer (name/merchant)
  score  = merchant_match(0..1) + date_proximity(0..1) + amount_proximity(0..1)
           weighted.

Outcomes:
  - 'auto'    : exactly one candidate with score >= AUTO_THRESHOLD
  - 'review'  : candidates exist but ambiguous / below threshold → surface to Reed
  - 'unmatched': no candidate found → surface as 'unknown transaction'
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta


# Tuning knobs (wheel later against real data).
DATE_WINDOW_DAYS = 2          # order date ± N days to search tx dates
AMOUNT_TOL_FRAC = 0.02        # amount miss tolerance as a fraction of |total|
AMOUNT_TOL_ABS = 0.05         # ...and a floor in dollars
AUTO_THRESHOLD = 2.6          # out of 3.0 → auto-link without asking
MERCHANT_W = 1.0              # weights
DATE_W = 1.0
AMOUNT_W = 1.0


def _norm_merchant(s: str) -> str:
    return (s or "").strip().lower()


def merchant_match(retailer: str, tx_merchant: str, tx_name: str) -> float:
    r = _norm_merchant(retailer)
    for cand in (_norm_merchant(tx_merchant), _norm_merchant(tx_name)):
        if not cand:
            continue
        # exact merchant-name hit is a strong signal
        if r and (r == cand or r in cand or cand in r):
            return 1.0
        # contains the retailer's first token (e.g. 'WALMART' inside 'WAL-MART #1234')
        token = r.split()[0] if r.split() else r
        if len(token) >= 4 and token in cand:
            return 0.7
    # no merchant text at all → can't be confident
    return 0.0


def date_proximity(order_date: str, tx_date: str, window: int = DATE_WINDOW_DAYS) -> float:
    try:
        o = _date.fromisoformat(order_date[:10])
        t = _date.fromisoformat(tx_date[:10])
    except (ValueError, TypeError):
        return 0.0  # unknown date → no credit
    delta = abs((o - t).days)
    if delta == 0:
        return 1.0
    if delta <= window:
        return 1.0 - (delta / max(window, 1)) * 0.5
    return 0.0


def amount_proximity(order_total: float, tx_amount: float,
                     tol_frac: float = AMOUNT_TOL_FRAC, tol_abs: float = AMOUNT_TOL_ABS) -> float:
    tot = abs(order_total or 0.0)
    amt = abs(tx_amount or 0.0)
    if tot == 0 and amt == 0:
        return 1.0
    if tot == 0 or amt == 0:
        return 0.0
    tol = max(tot * tol_frac, tol_abs)
    if abs(tot - amt) <= tol:
        return 1.0
    return 0.0


def score_order(order: dict, txs: list[dict]) -> list[dict]:
    """Return ranked candidate lists [{score, tx, merchant_hit, date_score, amt_score}]."""
    candidates = []
    for tx in txs:
        m = merchant_match(order.get("retailer", ""), tx.get("merchant_name", ""), tx.get("name", ""))
        if m <= 0.0:
            continue  # wrong merchant → not a candidate
        ds = date_proximity(order.get("date", ""), tx.get("date", ""))
        a = amount_proximity(order.get("total", 0.0), tx.get("amount", 0.0))
        score = MERCHANT_W * m + DATE_W * ds + AMOUNT_W * a
        candidates.append({
            "tx_id": tx.get("tx_id"),
            "tx_date": tx.get("date"),
            "tx_name": tx.get("name"),
            "tx_merchant": tx.get("merchant_name"),
            "tx_amount": tx.get("amount"),
            "merchant_hit": round(m, 2),
            "date_score": round(ds, 2),
            "amount_score": round(a, 2),
            "score": round(score, 2),
        })
        candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def decide(order: dict, txs: list[dict], existing: set[str]) -> dict:
    """Run correlation for one order → correlation result to persist."""
    order_no = order.get("order_no")
    if order_no in existing:
        return {"order_no": order_no, "status": "skipped", "note": "already correlated"}
    cands = score_order(order, txs)
    if not cands:
        return {"order_no": order_no, "status": "unmatched", "candidates": []}

    top = cands[0]
    # Ambiguity guard: top vs runner-up too close → human review.
    runner = cands[1] if len(cands) > 1 else None
    ambiguous = runner and (top["score"] - runner["score"]) < 0.4

    if top["score"] >= AUTO_THRESHOLD and not ambiguous:
        split = _item_split(order, top.get("tx_amount") or order.get("total"))
        return {"order_no": order_no, "tx_id": top["tx_id"], "status": "auto",
                "score": top["score"], "split": split, "candidates": cands}
    return {"order_no": order_no, "tx_id": top["tx_id"], "status": "review",
            "score": top["score"], "candidates": cands}


def _item_split(order: dict, tx_amount: float) -> list[dict]:
    """Derive a line-item category split from the order's items.

    We don't yet have per-item Plaid categories, so this tags each item with a
    generic category from a tiny keyword map and proportions the CC line by item
    price. Replace the keyword map with real categories as order data matures.
    """
    simple = {
        "shirt": "Apparel", "jeans": "Apparel", "jacket": "Apparel", "shoe": "Apparel",
        "toy": "Toys & Games", "game": "Toys & Games", "lego": "Toys & Games",
        "tv": "Electronics", "computer": "Electronics", "laptop": "Electronics",
        "phone": "Electronics", "charger": "Electronics", "keyboard": "Electronics",
        "cable": "Electronics", "headphone": "Electronics", "battery": "Electronics",
        "cereal": "Groceries", "milk": "Groceries", "bread": "Groceries", "coffee": "Groceries",
        "detergent": "Household Goods", "soap": "Household Goods", "towel": "Household Goods",
        "pan": "Household Goods", "sheet": "Household Goods", "paper towel": "Household Goods",
        "book": "Books & Media", "halo": "Toys & Games",
    }

    def cat(name: str) -> str:
        n = (name or "").lower()
        for k, v in simple.items():
            if k in n:
                return v
        return "Other / General Merchandise"

    items = order.get("items") or []
    total = sum(float(it.get("price") or 0.0) for it in items)
    if not items or total <= 0:
        return [{"category": "Other / General Merchandise", "amount": round(tx_amount, 2),
                 "share": 1.0, "item": "(no line items)"}]
    split = []
    for it in items:
        price = float(it.get("price") or 0.0)
        share = price / total
        split.append({
            "item": it.get("name") or it.get("title") or "(item)",
            "category": cat(it.get("name") or it.get("title") or ""),
            "price": round(price, 2),
            "share": round(share, 4),
            "amount": round(tx_amount * share, 2),
        })
    return split
