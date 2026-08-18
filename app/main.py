"""money.ffilt.us — FastAPI app: upload financial CSVs, render money-flow Sankey."""

from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Store
from .k8s import default_secret_store
from .parser import csv_to_transactions
from .sankey import DIMS, build_figure

app = FastAPI(title="money", version="0.1.0")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
store = Store(os.path.join(DATA_DIR, "money.db"))

PLAID_FIELDS = ("client_id", "client_secret_prd", "client_secret_tst")
secret_store = default_secret_store()

STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# ---------------- uploads ----------------
@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    source: str = Form(""),
    profile: str = Form(""),
    all_spend: bool = Form(False),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"decode failed: {e}")

    profile_cfg = _profile_spec(profile)
    try:
        txs = csv_to_transactions(
            text,
            source=source or file.filename or "upload",
            profile=profile_cfg,
            all_spend=all_spend,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    fid = store.add_file(file.filename or "upload.csv", txs[0]["source"], len(txs))
    store.bulk_insert(fid, txs)

    inc = sum(t["amount"] for t in txs if t["amount"] > 0)
    out = sum(-t["amount"] for t in txs if t["amount"] < 0)
    return {
        "file_id": fid,
        "parsed": len(txs),
        "source": txs[0]["source"],
        "inflow": round(inc, 2),
        "outflow": round(out, 2),
        "preview": txs[:5],
    }


_PROFILES = {
    # per-issuer column remaps if auto-detection isn't enough. Empty = trust auto-detect.
    "huntington": {},
    "chase": {},
    "capone": {},
    "capitalone": {},
    "amex": {"expense_positive": True},
    "bestegg": {},
    "samsclub": {},
    "syncovy": {},
}


def _profile_spec(name: str):
    return _PROFILES.get((name or "").strip().lower())


@app.get("/api/files")
def files():
    return store.list_files()


@app.delete("/api/files/{fid}")
def delete_file(fid: int):
    return {"deleted": store.delete_file(fid)}


@app.delete("/api/data")
def clear_all():
    store.delete_all()
    return {"deleted": True}


# ---------------- query ----------------
@app.get("/api/transactions")
def transactions(
    source: str = "", category: str = "", direction: str = "",
    start: str = "", end: str = "", description: str = "", limit: int = 1000,
):
    rows = store.query(source=source, category=category, direction=direction,
                       start=start, end=end, description=description)
    return rows[:limit]


@app.get("/api/facets")
def facets():
    return {
        "sources": store.distinct("source"),
        "categories": store.distinct("category"),
    }


@app.get("/api/summary")
def summary(source: str = "", start: str = "", end: str = ""):
    rows = store.query(source=source, start=start, end=end)
    inflow = sum(t["amount"] for t in rows if t["amount"] > 0)
    outflow = sum(-t["amount"] for t in rows if t["amount"] < 0)
    return {
        "transactions": len(rows),
        "inflow": round(inflow, 2),
        "outflow": round(outflow, 2),
    }


@app.get("/api/sankey")
def sankey(
    direction: str = "out", layers: str = "source,category,merchant",
    source: str = "", category: str = "", start: str = "", end: str = "",
    top: int = 0,
):
    rows = store.query(source=source, category=category, start=start, end=end)
    layer_list = [l for l in (layers or "").split(",") if l.strip()]
    fig = build_figure(
        rows, layers=layer_list, direction=direction, top_merchants=top
    )
    return fig


@app.get("/api/dims")
def dims():
    return {"dims": sorted(DIMS.keys()), "directions": ["out", "in"]}


# ---------------- Plaid secrets (write-only, stored as a k8s Secret) ----------------
class PlaidSecretsIn(BaseModel):
    client_id: str = ""
    client_secret_prd: str = ""
    client_secret_tst: str = ""


def _plaid_status(data: dict) -> dict:
    # NEVER echo the secret values — only whether each is configured.
    return {"secrets_set": {f: bool((data or {}).get(f)) for f in PLAID_FIELDS}}


@app.get("/api/secrets/plaid")
def plaid_secrets_status():
    try:
        data = secret_store.get("plaid-secrets")
    except Exception as e:
        raise HTTPException(503, f"k8s secrets unavailable: {e}")
    return _plaid_status(data)


@app.post("/api/secrets/plaid")
def plaid_secrets_set(payload: PlaidSecretsIn):
    try:
        existing = secret_store.get("plaid-secrets")
        merged = dict(existing)
        for f in PLAID_FIELDS:
            v = getattr(payload, f, "") or ""
            if v:
                merged[f] = v.strip()
        secret_store.set("plaid-secrets", {k: merged[k] for k in PLAID_FIELDS if merged.get(k)})
    except Exception as e:
        raise HTTPException(503, f"k8s secrets unavailable: {e}")
    return _plaid_status(merged)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
