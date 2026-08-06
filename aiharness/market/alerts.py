"""Local price alerts (checked when the user queries the market panel)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

AlertOp = Literal[">=", "<=", ">", "<"]

ALERTS_REL = Path(".aiharness") / "alerts.json"


@dataclass
class PriceAlert:
    id: str
    symbol: str
    op: AlertOp
    price: float
    note: str = ""
    armed: bool = True
    created_at: float = 0.0

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def triggered(self, last: float) -> bool:
        if self.op == ">=":
            return last >= self.price
        if self.op == "<=":
            return last <= self.price
        if self.op == ">":
            return last > self.price
        if self.op == "<":
            return last < self.price
        return False


def _path(workspace: Path) -> Path:
    return workspace / ALERTS_REL


def load_alerts(workspace: Path) -> list[PriceAlert]:
    path = _path(workspace)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: list[PriceAlert] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        op = str(row.get("op") or ">=")
        if op not in {">=", "<=", ">", "<"}:
            op = ">="
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        items.append(
            PriceAlert(
                id=str(row.get("id") or uuid.uuid4().hex[:8]),
                symbol=str(row["symbol"]).upper(),
                op=op,  # type: ignore[arg-type]
                price=price,
                note=str(row.get("note") or ""),
                armed=bool(row.get("armed", True)),
                created_at=float(row.get("created_at") or 0),
            )
        )
    return items


def save_alerts(workspace: Path, alerts: list[PriceAlert]) -> None:
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([a.public() for a in alerts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_alert(
    workspace: Path,
    symbol: str,
    price: float,
    *,
    op: str = ">=",
    note: str = "",
) -> PriceAlert:
    alerts = load_alerts(workspace)
    if op not in {">=", "<=", ">", "<"}:
        op = ">="
    item = PriceAlert(
        id=uuid.uuid4().hex[:8],
        symbol=symbol.strip().upper(),
        op=op,  # type: ignore[arg-type]
        price=float(price),
        note=note.strip(),
        armed=True,
        created_at=time.time(),
    )
    alerts.append(item)
    save_alerts(workspace, alerts)
    return item


def delete_alert(workspace: Path, alert_id: str) -> bool:
    alerts = load_alerts(workspace)
    kept = [a for a in alerts if a.id != alert_id]
    if len(kept) == len(alerts):
        return False
    save_alerts(workspace, kept)
    return True


def check_alerts(
    workspace: Path, symbol: str, last: float
) -> list[tuple[PriceAlert, float]]:
    """Return armed alerts that fire for ``symbol`` at ``last``; disarm them."""
    alerts = load_alerts(workspace)
    fired: list[tuple[PriceAlert, float]] = []
    changed = False
    target = symbol.strip().upper()
    for alert in alerts:
        if not alert.armed or alert.symbol != target:
            continue
        if alert.triggered(last):
            fired.append((alert, last))
            alert.armed = False
            changed = True
    if changed:
        save_alerts(workspace, alerts)
    return fired
