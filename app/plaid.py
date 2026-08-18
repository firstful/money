"""Thin Plaid API client — stdlib urllib, AVX-portable (no numpy/pandas).

Reads credentials from the k8s Secret at call time (no restart on update).
Conventions:
  - env = "sandbox" (https://sandbox.plaid.com) or "production".
  - Plaid `amount`: positive = debit (money OUT), negative = credit (money IN).
    We keep that native sign in plaid_transactions; correlation compares |amount|.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .k8s import default_secret_store

_BASE = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


class PlaidError(RuntimeError):
    def __init__(self, code: int, body: str):
        self.code = code
        self.body = body
        try:
            d = json.loads(body)
            self.error_type = d.get("error_type")
            self.error_code = d.get("error_code")
            self.message = d.get("error_message")
            self.request_id = d.get("request_id")
            detail = " | ".join(x for x in [self.error_type, self.error_code, self.message] if x)
        except Exception:
            self.error_type = None
            self.error_code = None
            self.message = None
            self.request_id = None
            detail = body[:300]
        super().__init__(f"plaid {code}: {detail}")


class PlaidClient:
    def __init__(self, env: str = "sandbox", secret_store=None):
        if env not in _BASE:
            raise ValueError(f"unknown plaid env {env!r}")
        self.env = env
        self.secret_store = secret_store or default_secret_store()

    def _creds(self) -> dict:
        d = self.secret_store.get("plaid-secrets")
        client_id = (d.get("client_id") or "").strip()
        secret = (d.get("client_secret_tst") if self.env == "sandbox"
                  else d.get("client_secret_prd") or "").strip()
        if not client_id or not secret:
            raise PlaidError(503, "plaid-secrets not configured (client_id / secret missing)")
        return {"client_id": client_id, "secret": secret}

    def _post(self, path: str, payload: dict) -> dict:
        payload = {**self._creds(), **payload}
        req = urllib.request.Request(
            _BASE[self.env] + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise PlaidError(e.code, e.read().decode()) from None

    # ---- link / item lifecycle ----
    def link_token_create(self, client_user_id: str, products=None,
                          webhooks_url: str = ""):
        """Create a Link token (both sandbox + prod share this shape)."""
        payload = {
            "client_name": "money-ffilt-us",
            "products": products or ["transactions"],
            "country_codes": ["US"],
            "language": "en",
            "user": {"client_user_id": client_user_id},
        }
        if webhooks_url:
            payload["webhooks"] = {"transactions": webhooks_url}
        return self._post("/link/token/create", payload)

    def sandbox_public_token_create(self, institution_id: str = "ins_109508",
                                    products=None, start_date: str | None = None,
                                    end_date: str | None = None, webhooks_url: str = ""):
        """Create a *sandbox-only* Item directly (skips the Link UI for pilots).

        Default institution ins_109508 (First Platypus Bank) generates 90 days
        of seeded transactions. Returns {'public_token', 'item_id', ...}.
        """
        opt = {}
        if webhooks_url:
            opt["webhooks"] = {"transactions": webhooks_url}
        if start_date or end_date:
            opt["start_date"] = start_date
            opt["end_date"] = end_date
        payload = {
            "institution_id": institution_id,
            "initial_products": products or ["transactions"],
        }
        if opt:
            payload["options"] = opt
        return self._post("/sandbox/public_token/create", payload)

    def item_public_token_exchange(self, public_token: str) -> dict:
        """Exchange a public_token (real Link or sandbox) for an access_token."""
        return self._post("/item/public_token/exchange", {"public_token": public_token})

    def accounts_get(self, access_token: str) -> list[dict]:
        r = self._post("/accounts/get", {"access_token": access_token})
        return r.get("accounts", [])

    def item_get(self, access_token: str) -> dict:
        r = self._post("/item/get", {"access_token": access_token})
        return r.get("item", {})

    # ---- transactions (sync cursor semantics) ----
    def transactions_sync(self, access_token: str, cursor: str = "",
                          max_wait: float = 30.0, poll: float = 2.0) -> dict:
        """Sync with cursor pagination.

        A brand-new (sandbox or production) item reports
        `transactions_update_status: NOT_READY` on its first sync while the
        data generator backfills history asynchronously — so we poll until it
        flips to READY (or the wait budget runs out).
        """
        import time
        payload = {"access_token": access_token}
        if cursor:
            payload["cursor"] = cursor
        out = {"added": [], "modified": [], "removed": [], "next_cursor": cursor,
               "has_more": False, "status": "READY", "request_id": ""}
        deadline = time.monotonic() + max_wait
        while True:
            r = self._post("/transactions/sync", payload)
            out["added"] += r.get("added", [])
            out["modified"] += r.get("modified", [])
            out["removed"] += r.get("removed", [])
            out["next_cursor"] = r.get("next_cursor", out["next_cursor"])
            out["has_more"] = r.get("has_more", False)
            out["status"] = r.get("transactions_update_status", "READY")
            out["request_id"] = r.get("request_id", "")
            if out["has_more"]:
                payload["cursor"] = out["next_cursor"]
                continue
            # not ready yet → wait for the async generator to finish, then retry
            if out["status"] not in ("READY", "HISTORICAL_UPDATE_STARTED", "INITIAL_UPDATE_STARTED"):
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
                continue
            break
        return out
