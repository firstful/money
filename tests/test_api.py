"""End-to-end API tests (upload -> parse -> store -> sankey) with a temp DB."""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = "/tmp/money-test-data"
# clean slate before the module-level Store is constructed at import
shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def test_healthz():
    assert client.get("/healthz").json()["ok"] is True


def test_upload_and_sankey():
    with open(os.path.join(SAMPLES, "huntington.csv"), "rb") as f:
        r = client.post(
            "/api/upload",
            files={"file": ("huntington.csv", f, "text/csv")},
            data={"source": "Huntington"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parsed"] == 52

    # facets
    facets = client.get("/api/facets").json()
    assert "Huntington" in facets["sources"]

    # sankey expenses
    s = client.get("/api/sankey?layers=source,category,merchant&direction=out").json()
    assert s["data"][0]["type"] == "sankey"
    assert len(s["data"][0]["link"]["source"]) > 0

    # summary
    summary = client.get("/api/summary").json()
    assert summary["transactions"] == 52
    assert round(summary["outflow"], 2) == 40592.57

    # transactions list sorted
    txs = client.get("/api/transactions").json()
    assert len(txs) == 52

    # delete
    files = client.get("/api/files").json()
    assert len(files) == 1
    d = client.delete(f"/api/files/{files[0]['id']}").json()
    assert d["deleted"] is True
    assert client.get("/api/files").json() == []


def test_upload_merchant_all_spend():
    with open(os.path.join(SAMPLES, "samsclub-orders.csv"), "rb") as f:
        r = client.post(
            "/api/upload",
            files={"file": ("sams.csv", f, "text/csv")},
            data={"source": "Sams Club", "all_spend": "true"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["parsed"] == 5


def test_upload_rejects_bad_headers():
    r = client.post(
        "/api/upload",
        files={"file": ("bad.csv", b"foo,bar,baz\n1,2,3\n", "text/csv")},
        data={"source": "Junk"},
    )
    assert r.status_code == 422


def test_plaid_secrets_write_only():
    # starts unconfigured
    s = client.get("/api/secrets/plaid").json()
    assert s["secrets_set"] == {
        "client_id": False, "client_secret_prd": False, "client_secret_tst": False,
    }

    # save all three
    r = client.post("/api/secrets/plaid", json={
        "client_id": "cid123", "client_secret_prd": "prdsecret", "client_secret_tst": "tstsecret",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["secrets_set"] == {
        "client_id": True, "client_secret_prd": True, "client_secret_tst": True,
    }
    # values are NEVER echoed
    assert "cid123" not in r.text
    assert "prdsecret" not in r.text
    assert "tstsecret" not in r.text

    # GET is also write-only
    g = client.get("/api/secrets/plaid").json()
    assert g["secrets_set"] == body["secrets_set"]

    # partial update leaves the others intact
    r2 = client.post("/api/secrets/plaid", json={"client_secret_prd": "prdsecret2"})
    assert "prdsecret2" not in r2.text
    assert r2.json()["secrets_set"] == {
        "client_id": True, "client_secret_prd": True, "client_secret_tst": True,
    }
