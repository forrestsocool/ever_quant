"""Lossless, columnar wire format for the progressive browser dashboard.

The public snapshot/MCP formats remain unchanged. Column names travel once,
instead of repeating them for every ETF on every trading day.
"""
from __future__ import annotations


def columns(rows: list[dict]) -> dict:
    names = list(dict.fromkeys(key for row in rows for key in row))
    return {"columns": names, "rows": [[row.get(key) for key in names] for row in rows]}


def pack_dashboard(snapshot: dict, scope: str) -> dict:
    return {**snapshot,
            "series": columns(snapshot.get("series", [])),
            "etf_series": {code: columns(rows) for code, rows in snapshot.get("etf_series", {}).items()},
            "metadata": {**snapshot.get("metadata", {}), "benchmark": columns(snapshot.get("metadata", {}).get("benchmark", []))},
            "transport": {"format": "columns-v1", "scope": scope,
                          "review_policy": "历史联动按当前披露的数据版本回看；不重建当时可见数据。"}}
