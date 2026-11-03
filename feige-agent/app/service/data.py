"""Read-only business data adapter. Live connectors implement this contract later."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Protocol


class DataSource(Protocol):
    store_id: str
    demo: bool
    business: dict
    products: list[dict]

    def order(self, order_id: str, authorized_ids: list[str]) -> dict | None:
        ...


def checked_fact(value, path: str, source: str, demo: bool = False) -> tuple[object, dict]:
    evidence = {"path": path, "source": source, "demo": demo, "snapshot": True}
    if isinstance(value, dict) and "value" in value:
        evidence.update({key: value[key] for key in ("source", "updated_at", "expires_at", "effective_from") if key in value})
        if value.get("effective_from"):
            try:
                start = datetime.fromisoformat(str(value["effective_from"]).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < start:
                    return None, dict(evidence, error="资料尚未生效")
            except (ValueError, TypeError):
                return None, dict(evidence, error="资料生效时间无效")
        if value.get("expires_at"):
            try:
                expiry = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                # Date-only validity includes that entire UTC day.
                if len(str(value["expires_at"])) == 10:
                    from datetime import timedelta
                    expiry += timedelta(days=1)
                if datetime.now(timezone.utc) >= expiry:
                    return None, dict(evidence, error="资料已过期")
            except (ValueError, TypeError):
                return None, dict(evidence, error="资料有效期无效")
        value = value.get("value")
    if value is None or value == "" or value == [] or value == {}:
        return None, dict(evidence, error="资料缺失")
    if isinstance(value, float) and not math.isfinite(value):
        return None, dict(evidence, error="数值无效")
    return value, evidence


class SnapshotDataSource:
    """Local confirmed snapshots only; never presented as a live inventory feed."""

    def __init__(self, store_id: str, business: dict, products: list[dict],
                 orders: dict | None = None, demo: bool = False, provenance: dict | None = None):
        self.store_id = store_id
        self.business = business
        self.products = products
        self.orders = orders or {}
        self.provenance = provenance or {}
        self.demo = demo or any(p.get("sample_data") or p.get("source") == "sample" for p in products)

    def order(self, order_id: str, authorized_ids: list[str]) -> dict | None:
        # Knowing an order number is not authorization. The conversation adapter
        # supplies this allowlist independently of user/model-provided text.
        if order_id not in authorized_ids:
            return None
        return self.orders.get(order_id)
