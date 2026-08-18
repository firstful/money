"""Tests for the generic CSV transaction parser against representative exports."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parser import csv_to_transactions, parse_amount, parse_date

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def _load(name):
    with open(os.path.join(SAMPLES, name)) as f:
        return f.read()


def test_parse_amount():
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("(88.50)") == -88.5
    assert parse_amount("45.10") == 45.10
    assert parse_amount("1500.00-") == -1500.0
    assert parse_amount("") is None
    assert parse_amount("400.00 CR") == 400.0
    assert parse_amount("115.23 DR") == -115.23


def test_parse_date():
    assert parse_date("8/29/25") == "2025-08-29"
    assert parse_date("01/06/2026") == "2026-01-06"
    assert parse_date("2026-01-05") == "2026-01-05"
    assert parse_date("not a date") is None


def test_chase_personal_real_sample():
    txs = csv_to_transactions(_load("chase-personal.csv"), source="Chase Personal")
    assert len(txs) == 52
    assert txs[0]["date"] == "2025-08-29"
    assert txs[0]["direction"] == "in"
    assert txs[0]["category"] == "Transfer"
    assert txs[1]["direction"] == "out"
    assert txs[1]["amount"] == -3154.51
    inc = sum(t["amount"] for t in txs if t["amount"] > 0)
    out = sum(-t["amount"] for t in txs if t["amount"] < 0)
    assert round(inc, 2) == 40609.56
    assert round(out, 2) == 40592.57


def test_samsclub_merchant_all_spend():
    txs = csv_to_transactions(_load("samsclub-orders.csv"), source="Sams Club", all_spend=True)
    assert len(txs) == 5
    assert all(t["direction"] == "out" for t in txs)
    assert all(t["amount"] < 0 for t in txs)
    # first order 82.47 becomes -82.47
    assert txs[0]["amount"] == -82.47
    assert txs[0]["description"] == "SAMS CLUB #6381"


def test_amex_signed_amount_with_category():
    txs = csv_to_transactions(_load("amex.csv"), source="Amex", profile={"expense_positive": True})
    assert len(txs) == 4
    by_desc = {t["description"]: t for t in txs}
    assert by_desc["AMAZON.COM"]["direction"] == "out"
    assert by_desc["AMAZON.COM"]["amount"] == -48.21
    assert by_desc["AMAZON.COM"]["category"] == "Groceries"
    # payment (negative in Amex) is a credit -> flipped to income
    assert by_desc["PAYMENT THANK YOU"]["amount"] > 0
    assert by_desc["PAYMENT THANK YOU"]["direction"] == "in"


def test_chase_cc_signed_amount_type():
    txs = csv_to_transactions(_load("chase-cc.csv"), source="Chase CC")
    assert len(txs) == 3
    by_desc = {t["description"]: t for t in txs}
    assert by_desc["FREEDOM MORTGAGE PAYMENT"]["amount"] == -1500.0
    assert by_desc["DIRECT DEP PAYROLL"]["amount"] == 3200.0
    # category column wins over type column for categorization
    assert by_desc["FREEDOM MORTGAGE PAYMENT"]["category"] == "Mortgage & Rent"


def test_capitalone_debit_credit_columns():
    txs = csv_to_transactions(_load("capitalone.csv"), source="Capital One")
    assert len(txs) == 3
    by_desc = {t["description"]: t for t in txs}
    assert by_desc["WALMART #1234"]["amount"] == -115.23
    assert by_desc["WALMART #1234"]["direction"] == "out"
    assert by_desc["PAYMENT - THANK YOU"]["amount"] == 400.0
    assert by_desc["PAYMENT - THANK YOU"]["direction"] == "in"


def test_no_recognized_headers_raises():
    import pytest

    with pytest.raises(ValueError):
        csv_to_transactions("foo,bar,baz\n1,2,3\n")
