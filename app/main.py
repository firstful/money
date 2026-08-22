"""money.ffilt.us — FastAPI app: upload financial CSVs, render money-flow Sankey."""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Store
from .k8s import default_secret_store
from .parser import csv_to_transactions
from .sankey import DIMS, build_figure
from .plaid import PlaidClient, PlaidError
from . import correlate as corr

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


@app.get("/privacy")
def privacy():
    return FileResponse(
        os.path.join(STATIC, "privacy.html"),
        media_type="text/html",
    )


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


# ---------------- Plaid pilot: sandbox item + sync + correlations ----------------
SANDBOX_INSTITUTION = "ins_109508"  # First Platypus Bank (seeded tx history)


class SandboxItemIn(BaseModel):
    institution_id: str = SANDBOX_INSTITUTION
    products: list[str] = ["transactions"]


def _plaid(env: str) -> PlaidClient:
    return PlaidClient(env=env, secret_store=secret_store)


def _resolve_institution(client: PlaidClient, institution_id: str) -> tuple[str, str]:
    """Best-effort ins_* -> (name, products-json). Never raises (link must not
    fail because a metadata lookup failed)."""
    try:
        inst = client.institutions_get_by_id(institution_id)
        return (inst.get("name") or "", json.dumps(inst.get("products") or []))
    except PlaidError:
        return "", ""



def _plaid_http(exc: PlaidError):
    return JSONResponse(status_code=exc.code if 400 <= exc.code < 600 else 502,
                        content={"error_type": getattr(exc, "error_type", None),
                                 "error_code": getattr(exc, "error_code", None),
                                 "message": getattr(exc, "message", None) or str(exc)})


@app.get("/api/plaid/status")
def plaid_status():
    try:
        data = secret_store.get("plaid-secrets")
    except Exception as e:
        raise HTTPException(503, f"k8s secrets unavailable: {e}")
    return {
        "env": "sandbox" if data.get("client_secret_tst") else "production",
        "secrets_set": _plaid_status(data)["secrets_set"],
        "items": store.list_plaid_items(),
    }


class LinkTokenIn(BaseModel):
    env: str = "production"          # 'production' | 'sandbox'
    products: list[str] = ["transactions"]


@app.post("/api/plaid/link/token")
def plaid_link_token(payload: LinkTokenIn):
    """Create a Link token for the given env (production = real bank login)."""
    env = payload.env if payload.env in ("production", "sandbox") else "production"
    client = _plaid(env)
    try:
        r = client.link_token_create(client_user_id="reedsutliff", products=payload.products)
    except PlaidError as e:
        return _plaid_http(e)
    return {"link_token": r["link_token"], "expiration": r.get("expiration"), "env": env}


class ExchangeIn(BaseModel):
    public_token: str
    env: str = "production"


@app.post("/api/plaid/item/exchange")
def plaid_item_exchange(payload: ExchangeIn):
    """Exchange a public_token (from Link, production or sandbox) for an access_token,
    then store the item so `POST /api/plaid/sync?env=<env>` picks it up."""
    env = payload.env if payload.env in ("production", "sandbox") else "production"
    client = _plaid(env)
    try:
        exch = client.item_public_token_exchange(payload.public_token.strip())
        accounts = client.accounts_get(exch["access_token"])
        item = client.item_get(exch["access_token"])
    except PlaidError as e:
        return _plaid_http(e)
    store.upsert_plaid_item(exch["item_id"], exch["access_token"],
                            item.get("institution_id", ""), accounts, env=env,
                            *(_resolve_institution(client, item.get("institution_id", ""))))
    return {
        "item_id": exch["item_id"],
        "env": env,
        "institution_id": item.get("institution_id", ""),
        "institution_name": (store.get_plaid_item(exch["item_id"]) or {}).get("institution_name", ""),
        "accounts": [{"id": a.get("account_id"), "name": a.get("name"),
                      "mask": a.get("mask"), "type": a.get("type"),
                      "subtype": a.get("subtype")} for a in accounts],
    }


@app.post("/api/plaid/sandbox/item")
def plaid_sandbox_item(payload: SandboxItemIn):
    """Create a sandbox item directly (no Link UI) and store its access token."""
    client = _plaid("sandbox")
    try:
        pub = client.sandbox_public_token_create(
            institution_id=payload.institution_id,
            products=payload.products,
        )
        exch = client.item_public_token_exchange(pub["public_token"])
    except PlaidError as e:
        return _plaid_http(e)
    item_id = exch["item_id"]
    access_token = exch["access_token"]
    accounts = client.accounts_get(access_token)
    store.upsert_plaid_item(item_id, access_token, payload.institution_id,
                            accounts, env="sandbox",
                            *(_resolve_institution(client, payload.institution_id)))
    return {
        "item_id": item_id,
        "institution_id": payload.institution_id,
        "institution_name": (store.get_plaid_item(item_id) or {}).get("institution_name", ""),
        "accounts": [{"id": a.get("account_id"), "name": a.get("name"),
                      "mask": a.get("mask"), "type": a.get("type"),
                      "subtype": a.get("subtype")} for a in accounts],
    }


@app.post("/api/plaid/sync")
def plaid_sync(env: str = "sandbox", item_id: str = ""):
    """Run /transactions/sync for every stored item (or one) and store results."""
    client = _plaid(env)
    if item_id:
        it = store.get_plaid_item(item_id)
        items = [it] if it else []
    else:
        items = store.list_plaid_items_for_sync(env)
    totals = {"items": 0, "added": 0, "modified": 0, "removed": 0, "errors": []}
    for it in items:
        if not it:
            continue
        # Backfill institution metadata once (linked-before-this-build items).
        if not it.get("institution_name") and it.get("institution_id"):
            name, prods = _resolve_institution(client, it["institution_id"])
            if name:
                store.set_plaid_institution(it["item_id"], name, prods)
        try:
            res = client.transactions_sync(it["access_token"], it.get("cursor") or "")
            for tr in res.get("added", []):
                if store.upsert_plaid_tx(tr, it["item_id"]):
                    totals["added"] += 1
            for tr in res.get("modified", []):
                store.upsert_plaid_tx(tr, it["item_id"])
                totals["modified"] += 1
            for tr in res.get("removed", []):
                store.delete_plaid_tx(tr.get("transaction_id", ""))
                totals["removed"] += 1
            store.set_plaid_cursor(it["item_id"], res.get("next_cursor") or "")
            totals["items"] += 1
        except PlaidError as e:
            totals["errors"].append({"item_id": it["item_id"], "detail": str(e)})
    return totals


@app.get("/api/plaid/transactions")
def plaid_transactions(item_id: str = "", merchant: str = "",
                       start: str = "", end: str = "", limit: int = 2000):
    return store.query_plaid(item_id=item_id, merchant=merchant,
                             start=start, end=end, limit=limit)


# ---------------- order ingest + correlation ----------------
class OrderIn(BaseModel):
    order_no: str
    retailer: str
    date: str
    total: float
    items: list[dict] = []
    source: str = "manual"


@app.post("/api/orders")
def add_order(payload: OrderIn):
    oid = store.add_order(payload.order_no.strip(), payload.retailer.strip(),
                          payload.date, payload.total, payload.items, payload.source)
    return {"id": oid, **payload.model_dump()}


@app.get("/api/orders")
def list_orders(retailer: str = ""):
    return store.list_orders(retailer=retailer)


@app.post("/api/correlate")
def run_correlation():
    """Best-effort: link each order to a Plaid transaction; surface unknowns."""
    orders = store.list_orders()
    txs = store.query_plaid(limit=5000)
    existing = {c["order_no"] for c in store.query_correlations()}
    results = {"auto": [], "review": [], "unmatched": [], "skipped": []}
    for order in orders:
        if order["order_no"] in existing:
            results["skipped"].append(order["order_no"])
            continue
        d = corr.decide(order, txs, existing)
        st = d["status"]
        if st == "auto":
            store.upsert_correlation(d["tx_id"], d["order_no"], d["score"], "auto", d.get("split"))
            results["auto"].append(d)
        elif st == "review":
            store.upsert_correlation(d["tx_id"], d["order_no"], d["score"], "review")
            results["review"].append({"order_no": d["order_no"], "top": d.get("candidates", [None])[0]})
        else:
            results["unmatched"].append(d["order_no"])
    return results


@app.get("/api/correlations")
def correlations(status: str = ""):
    return store.query_correlations(status=status)


@app.get("/api/needs-review")
def needs_review():
    """What Reed must disambiguate: correlations in 'review' + unmatched orders
    (order with no candidate)."""
    reviews = store.query_correlations(status="review")
    all_orders = store.list_orders()
    correlated = {c["order_no"] for c in store.query_correlations()}
    unmatched_orders = [o for o in all_orders if o["order_no"] not in correlated]
    return {"review_correlations": reviews, "unmatched_orders": unmatched_orders}


class ResolveIn(BaseModel):
    tx_id: str
    order_no: str
    status: str  # 'confirmed' | 'rejected'


@app.post("/api/correlations/resolve")
def resolve_correlation(payload: ResolveIn):
    corr_row = store.get_correlation(payload.tx_id, payload.order_no)
    if not corr_row:
        raise HTTPException(404, "correlation not found")
    store.resolve_correlation(corr_row["id"],
                              "confirmed" if payload.status == "confirmed" else "rejected",
                              "reed")
    return {"id": corr_row["id"], "status": payload.status}


@app.post("/api/correlations/clear")
def clear_correlations():
    store.clear_correlations()
    return {"cleared": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
