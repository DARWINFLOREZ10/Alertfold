"""Outbox dispatcher. Delivery is at least once; receivers must deduplicate by ID."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import Database

logger = logging.getLogger("relayops.worker")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dispatch_one(db: Database, deliver: Callable[[str, dict], None]) -> bool:
    timestamp = utcnow()
    with db.transaction() as conn:
        row = conn.execute("""SELECT id,payload,attempts FROM outbox
            WHERE status IN ('pending','processing') AND due_at<=?
            ORDER BY due_at,id LIMIT 1""", (timestamp.isoformat(),)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE outbox SET status='processing',due_at=? WHERE id=?", ((timestamp + timedelta(seconds=30)).isoformat(), row["id"]))
        message = dict(row)
    try:
        deliver(message["id"], json.loads(message["payload"]))
    except Exception:
        attempts = message["attempts"] + 1
        delay = min(2 ** attempts, 300)
        status = "dead" if attempts >= 5 else "pending"
        with db.transaction() as conn:
            conn.execute("UPDATE outbox SET attempts=?,status=?,due_at=? WHERE id=? AND status='processing'", (attempts, status, (utcnow() + timedelta(seconds=delay)).isoformat(), message["id"]))
        logger.exception("delivery_failed", extra={"message_id": message["id"], "attempts": attempts})
    else:
        with db.transaction() as conn:
            conn.execute("UPDATE outbox SET status='delivered',delivered_at=? WHERE id=? AND status='processing'", (utcnow().isoformat(), message["id"]))
    return True


def log_delivery(message_id: str, payload: dict) -> None:
    logger.info("notification", extra={"message_id": message_id, "payload": payload})


def run(db: Database, interval: float = 1.0) -> None:
    while True:
        if not dispatch_one(db, log_delivery):
            time.sleep(interval)
