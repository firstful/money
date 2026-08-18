"""Unit tests for the order→Plaid-transaction correlator (pure logic, no API)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import correlate as corr  # noqa: E402


def _tx(tx_id, name, merchant, date, amount):
    return {"tx_id": tx_id, "name": name, "merchant_name": merchant,
            "date": date, "amount": amount}


def _order(order_no="ORD-1", retailer="Walmart", date="2026-08-10", total=100.0,
           items=None):
    return {"order_no": order_no, "retailer": retailer, "date": date, "total": total,
            "items": items or [{"name": "LCD TV", "price": 60.0},
                               {"name": "Coffee", "price": 40.0}]}


def test_exact_match_auto():
    order = _order()
    txs = [
        _tx("t1", "WALMART SUPERCENTER #1234", "Walmart", "2026-08-10", 99.97),
        _tx("t2", "AMAZON MKTPLACE", "Amazon", "2026-08-10", 500.0),
    ]
    d = corr.decide(order, txs, set())
    assert d["status"] == "auto", d
    assert d["tx_id"] == "t1"
    assert d["score"] >= corr.AUTO_THRESHOLD


def test_wrong_merchant_unmatched():
    order = _order(retailer="Walmart")
    txs = [_tx("t1", "AMAZON MKTPLACE", "Amazon", "2026-08-10", 99.97)]
    d = corr.decide(order, txs, set())
    assert d["status"] == "unmatched", d


def test_date_drift_reduces_score_to_review():
    # merchant + amount match, but date 10 days off → should NOT auto-link
    order = _order(date="2026-08-10")
    txs = [_tx("t1", "WALMART", "Walmart", "2026-08-20", 100.0)]
    d = corr.decide(order, txs, set())
    assert d["status"] == "review", d


def test_ambiguous_two_candidates_review():
    # two Walmart txs same amount same date → ambiguous → review
    order = _order()
    txs = [
        _tx("t1", "WALMART", "Walmart", "2026-08-10", 100.0),
        _tx("t2", "WALMART", "Walmart", "2026-08-10", 100.0),
    ]
    d = corr.decide(order, txs, set())
    assert d["status"] == "review", d


def test_line_item_split_proportions():
    split = corr._item_split(_order(total=100.0), 99.97)
    assert len(split) == 2
    # shares sum to 1.0
    assert abs(sum(s["share"] for s in split) - 1.0) < 1e-6
    # 60% of the CC line goes to the TV
    tv = next(s for s in split if s["item"] == "LCD TV")
    assert abs(tv["amount"] - 99.97 * 0.6) < 0.01
    assert tv["category"] == "Electronics"


def test_skip_existing():
    order = _order()
    d = corr.decide(order, [_tx("t1", "WALMART", "Walmart", "2026-08-10", 100.0)],
                    existing={"ORD-1"})
    assert d["status"] == "skipped"
