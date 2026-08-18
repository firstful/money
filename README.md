# money

Upload bank / credit-card / merchant CSVs and view your money flows as a
Sankey diagram.

**money.ffilt.us** — external via Cloudflare Access (Google login). Namespace
`money`. Python FastAPI + stdlib-csv parser + Plotly.js Sankey, SQLite on a
Longhorn PVC.

## How it works

1. Upload a CSV export (bank, CC, or merchant order history).
2. The **generic auto-detect parser** sniffs the delimiter + headers and
   normalizes every file to a canonical row: `{date, amount, direction,
   description, category, source}`. One parser covers Huntington / Chase /
   Capital One / Amex / BestEgg / Sams Club / Syncovy AND merchant exports
   (Sams, Lowes, Home Depot, eBay).
3. Rows land in SQLite (source, category, description preserved).
4. The UI renders a **Sankey** of money flows (`source → category → merchant`
   by default), filterable by direction, source, category, top-N merchants.

### Canonical convention

- **amount** is signed: *negative = money out (expense)*, *positive = money in
  (income)* — the same convention for every source.
- Direction is derived from the sign, except issuer profiles flip it when the
  export is sign-inverted. **Amex** exports purchases positive / payments
  negative, so the `amex` profile sets `expense_positive: true` (the parser
  flips to the canonical convention).

## Local dev / tests

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt fastapi python-multipart httpx pytest
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m uvicorn app.main:app --port 8000
```

## Deploy

- Image: `192.168.1.2:3000/reeds/money:<sha8>` (Gitea Actions `build-python`).
- Manifests: `deploy/k8s/` (interim `kubectl apply -k`); adopted into
  `reeds/sutliff-cluster` `apps/money` once the image is verified.
- Data: Longhorn PVC `money-data` mounted at `/data` (SQLite `money.db`).
