from datetime import datetime, timezone

from relayops.db import Database
from relayops.worker import dispatch_one


def test_worker_retries_then_delivers(tmp_path):
    db = Database(str(tmp_path / "worker.db"))
    with db.transaction() as conn:
        conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", ("m1", "t1", "i1", "incident_opened", '{"incident_id":"i1"}', "pending", 0, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", None))
    def fail(_id, _payload):
        raise RuntimeError("delivery unavailable")
    assert dispatch_one(db, fail)
    with db.connect() as conn:
        row = conn.execute("SELECT status,attempts FROM outbox").fetchone()
        assert tuple(row) == ("pending", 1)
    with db.transaction() as conn:
        conn.execute("UPDATE outbox SET due_at=?", (datetime.now(timezone.utc).isoformat(),))
    delivered = []
    assert dispatch_one(db, lambda id, payload: delivered.append((id, payload)))
    assert delivered == [("m1", {"incident_id": "i1"})]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "delivered"


def test_worker_moves_repeated_failures_to_dead(tmp_path):
    db = Database(str(tmp_path / "dead.db"))
    with db.transaction() as conn:
        conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", ("m2", "t1", "i1", "incident_opened", '{}', "pending", 0, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", None))
    for _ in range(5):
        assert dispatch_one(db, lambda _id, _payload: (_ for _ in ()).throw(RuntimeError("offline")))
        with db.transaction() as conn:
            conn.execute("UPDATE outbox SET due_at=?", (datetime.now(timezone.utc).isoformat(),))
    with db.connect() as conn:
        assert tuple(conn.execute("SELECT status,attempts FROM outbox").fetchone()) == ("dead", 5)
