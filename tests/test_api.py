import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from relayops.app import create_app


@pytest.fixture
def context(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    db = app.state.db
    with db.transaction() as conn:
        conn.execute("INSERT INTO tenants VALUES (?,?,?)", ("t1", "One", "secret-one"))
        conn.execute("INSERT INTO tenants VALUES (?,?,?)", ("t2", "Two", "secret-two"))
        for tenant, role in [("t1", "admin"), ("t2", "viewer")]:
            conn.execute("INSERT INTO api_keys VALUES (?,?,?)", (hashlib.sha256((tenant + role).encode()).hexdigest(), tenant, role))
        conn.execute("INSERT INTO services VALUES (?,?,?)", ("s1", "t1", "Payments"))
    with TestClient(app) as client:
        yield client, db


def auth(tenant="t1", role="admin"):
    return {"Authorization": f"Bearer {tenant}{role}"}


def ingest(client, external_id="e1", fingerprint="payment-errors", secret="secret-one"):
    raw = ('{"external_id":"' + external_id + '","fingerprint":"' + fingerprint + '","title":"Payment failures","severity":"high"}').encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/v1/services/s1/events", content=raw, headers={"X-Relay-Signature": signature, "Content-Type": "application/json"})


def test_signed_ingest_deduplicates_and_groups(context):
    client, db = context
    first = ingest(client).json()
    assert first["duplicate"] is False
    assert ingest(client).json()["duplicate"] is True
    second = ingest(client, external_id="e2").json()
    assert first["incident_id"] == second["incident_id"]
    assert len(client.get("/v1/incidents", headers=auth()).json()) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_auth_tenant_isolation_and_state_conflict(context):
    client, _ = context
    assert client.post("/v1/services/s1/events", json={}, headers={"X-Relay-Signature": "bad"}).status_code == 401
    incident_id = ingest(client).json()["incident_id"]
    assert client.get("/v1/incidents", headers=auth("t2", "viewer")).json() == []
    assert client.get(f"/v1/incidents/{incident_id}/audit", headers=auth("t2", "viewer")).status_code == 404
    assert client.post(f"/v1/incidents/{incident_id}/acknowledge", headers=auth("t2", "viewer"), json={"version": 1}).status_code == 403
    response = client.post(f"/v1/incidents/{incident_id}/acknowledge", headers=auth(), json={"version": 1})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert client.post(f"/v1/incidents/{incident_id}/resolve", headers=auth(), json={"version": 1}).status_code == 409
    assert client.post(f"/v1/incidents/{incident_id}/resolve", headers=auth(), json={"version": 2}).status_code == 200
    assert ingest(client, external_id="e3").json()["incident_id"] != incident_id


def test_service_permissions(context):
    client, _ = context
    assert client.post("/v1/services", json={"name": "Billing"}, headers=auth("t2", "viewer")).status_code == 403
    assert client.post("/v1/services", json={"name": "Billing"}, headers=auth()).status_code == 201
    assert client.post("/v1/services", json={"name": "Billing"}, headers=auth()).status_code == 409
