"""In-cluster Kubernetes Secret access — Plaid credentials for money.

Plaid CLIENT_ID / CLIENT_SECRET_PRD / CLIENT_SECRET_TST are stored in a k8s
Secret (`money/plaid-secrets`, type Opaque) so they never touch the SQLite DB
or any git repo, and stay write-only (the API never returns their values).

The app writes the Secret itself via the in-cluster API (self-service through
the UI) — requires a Role granting get/create/update on secrets + egress to the
kube-apiserver in the NetworkPolicy. `K8sSecretStore` reads the service-account
token from the standard mount; `FakeSecretStore` backs unit tests.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Dict

SVC_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API_HOST = "https://kubernetes.default.svc"

_SECRETS_BASE = "plaid-secrets"


class SecretStore:
    def get(self, name: str) -> Dict[str, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, name: str, plain: Dict[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError


class K8sSecretStore(SecretStore):
    def __init__(self, svc_dir: str = SVC_DIR, api_host: str = API_HOST):
        self.namespace = open(os.path.join(svc_dir, "namespace")).read().strip()
        self.token = open(os.path.join(svc_dir, "token")).read().strip()
        self.api_host = api_host
        ctx = ssl.create_default_context(
            cafile=os.path.join(svc_dir, "ca.crt")
        )
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

    def _request(self, method: str, path: str, body: bytes | None = None):
        req = urllib.request.Request(self.api_host + path, data=body, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        with self._opener.open(req) as r:
            return r.status, r.read()

    def _single(self, name: str) -> str:
        return f"/api/v1/namespaces/{self.namespace}/secrets/{name}"

    def get(self, name: str) -> Dict[str, str]:
        try:
            _, body = self._request("GET", self._single(name))
        except urllib.error.HTTPError as e:
            if e.code == 403 or e.code == 404:
                return {}
            raise
        doc = json.loads(body)
        return {k: base64.b64decode(v).decode() for k, v in doc.get("data", {}).items()}

    def set(self, name: str, plain: Dict[str, str]) -> None:
        enc = {k: base64.b64encode(v.encode()).decode() for k, v in plain.items() if v}
        if not enc:
            # nothing to store (all blank) — nothing to do
            return
        doc = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": self.namespace},
            "type": "Opaque",
            "data": enc,
        }
        body = json.dumps(doc).encode()
        base_path = f"/api/v1/namespaces/{self.namespace}/secrets"
        try:
            self._request("POST", base_path, body)  # create
        except urllib.error.HTTPError as e:
            if e.code == 409:  # exists → replace whole secret
                self._request("PUT", self._single(name), body)
            else:
                raise


class FakeSecretStore(SecretStore):
    """In-memory store for tests."""

    def __init__(self):
        self._data: Dict[str, Dict[str, str]] = {}

    def get(self, name: str) -> Dict[str, str]:
        return dict(self._data.get(name, {}))

    def set(self, name: str, plain: Dict[str, str]) -> None:
        merged = dict(self._data.get(name, {}))
        merged.update({k: v for k, v in plain.items() if v})
        self._data[name] = merged


def default_secret_store() -> SecretStore:
    if os.path.exists(os.path.join(SVC_DIR, "token")):
        try:
            return K8sSecretStore()
        except Exception:
            pass
    return FakeSecretStore()
