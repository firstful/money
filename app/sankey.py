"""Build a Plotly-Sankey-compatible figure from normalized transactions.

Layers are chosen from the canonical fields: source, category, description.
Default money-flow view:  source -> category -> merchant(description).
Expenses (direction=out) and income (direction=in) are computed separately.
"""

from __future__ import annotations

from collections import defaultdict

DIMS = {
    "source": "source",
    "account": "source",
    "category": "category",
    "merchant": "description",
    "description": "description",
}

# node colors by layer (soft palette)
_LAYER_COLORS = ["#3b6ea5", "#5f9e6e", "#c98a3d", "#8a6fb5", "#b56576"]


def _key(part: str) -> str:
    return (part or "").strip() or "(none)"


def build_figure(
    txs: list[dict],
    layers: list[str] | None = None,
    direction: str = "out",
    top_merchants: int = 0,
    chain: bool = False,
) -> dict:
    """Produce a {data:[sankey], layout:{}} dict for plotly.js.

    txs            : canonical transaction rows.
    layers         : field names for successive node layers (default source->category->description).
    direction      : 'out' (expenses) or 'in' (income).
    top_merchants  : keep only this many merchant nodes by value, lump the rest under
                     "(other)" (0 = keep all).
    chain          : if True, keep multi-hop chains; else only use the node's own value.
    """
    if not layers:
        layers = ["source", "category", "description"]
    layers = [DIMS.get(l.strip().lower(), l.strip()) for l in layers]
    layers = [l for l in layers if l in DIMS.values()] or ["source", "description"]

    sign = -1 if direction == "out" else 1
    # filter to the requested direction; value = magnitude
    rows = [t for t in txs if (t["amount"] < 0) == (direction == "out")]
    if not rows:
        return _empty(layers)

    # aggregate node values
    node_val: dict[tuple[int, str], float] = defaultdict(float)
    for t in rows:
        for i, layer in enumerate(layers):
            node_val[(i, _key(t.get(layer)))] += abs(t["amount"])

    # aggregate links between consecutive layers
    link_val: dict[tuple[tuple[str, str], tuple[str, str]], float] = defaultdict(float)
    for t in rows:
        chain_keys = [(_key(t.get(l))) for l in layers]
        for i in range(len(chain_keys) - 1):
            link_val[((i, chain_keys[i]), (i + 1, chain_keys[i + 1]))] += abs(t["amount"])

    # build per-layer node label lists (optionally top-N by value)
    layer_nodes: dict[int, list[str]] = {}
    for i, layer in enumerate(layers):
        vmap = {k[1]: node_val[k] for k in node_val if k[0] == i}
        vmap = {k: v for k, v in vmap.items()}
        if top_merchants and layer == layers[-1] and len(vmap) > top_merchants:
            sorted_items = sorted(vmap.items(), key=lambda kv: -kv[1])
            keep = dict(sorted_items[:top_merchants])
            rest = sum(v for k, v in sorted_items[top_merchants:])
            vmap = keep
            if rest:
                vmap["(other)"] = rest
        ordered = sorted(vmap.items(), key=lambda kv: -kv[1])
        layer_nodes[i] = [k for k, _ in ordered]

    # layer offset so node ids are globally unique
    node_label: list[str] = []
    node_idx: dict[tuple[int, str], int] = {}
    for i in range(len(layers)):
        for label in layer_nodes[i]:
            node_idx[(i, label)] = len(node_label)
            node_label.append(_display(label, layers[i], i))

    # links
    sources, targets, values = [], [], []
    for (lhs, rhs), v in link_val.items():
        (li, llabel), (rj, rlabel) = lhs, rhs
        src = node_idx.get(lhs)
        tgt = node_idx.get(rhs)
        if src is None or tgt is None:
            continue
        sources.append(src)
        targets.append(tgt)
        values.append(round(v, 2))

    node_color = [
        _LAYER_COLORS[li % len(_LAYER_COLORS)]
        for li in range(len(layers))
        for _ in layer_nodes[li]
    ]

    return {
        "data": [
            {
                "type": "sankey",
                "orientation": "h",
                "node": {
                    "pad": 18,
                    "thickness": 22,
                    "line": {"color": "rgba(0,0,0,0.3)", "width": 0.5},
                    "label": node_label,
                    "color": node_color,
                },
                "link": {"source": sources, "target": targets, "value": values},
            }
        ],
        "layout": {
            "title": {
                "text": f"Money flows — {direction} · {len(rows)} txns",
                "font": {"size": 16},
            },
            "font": {"family": "system-ui, sans-serif"},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "margin": {"l": 20, "r": 20, "t": 60, "b": 20},
            "height": max(420, 40 * len(node_label) + 120),
            "autosize": True,
        },
    }


def _display(label: str, field: str, layer: int) -> str:
    if field == "description" and label.lower() in ("(none)", ""):
        return "(no merchant)"
    return label[:48]


def _empty(layers) -> dict:
    return {
        "data": [{"type": "sankey", "node": {"label": []}, "link": {"source": [], "target": [], "value": []}}],
        "layout": {"title": {"text": "No transactions in this view"}, "height": 300},
    }
