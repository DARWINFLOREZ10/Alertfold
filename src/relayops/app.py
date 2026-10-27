import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .db import Database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class EventIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    fingerprint: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    severity: str = Field(pattern="^(low|medium|high|critical)$")


class TransitionIn(BaseModel):
    version: int = Field(ge=1)


def create_app(database_path: str | None = None) -> FastAPI:
    app = FastAPI(title="Alertfold", version="0.1.0")
    db = Database(database_path or os.getenv("RELAYOPS_DB", "data/relayops.db"))
    app.state.db = db

    def principal(authorization: str = Header(default="")) -> dict:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with db.connect() as conn:
            row = conn.execute("SELECT tenant_id,role FROM api_keys WHERE token_hash=?", (token_hash,)).fetchone()
        if row is None:
            raise HTTPException(401, "Invalid token")
        return dict(row)

    def require(role: str):
        def checker(user: dict = Depends(principal)) -> dict:
            if {"viewer": 0, "responder": 1, "admin": 2}[user["role"]] < {"viewer": 0, "responder": 1, "admin": 2}[role]:
                raise HTTPException(403, "Insufficient role")
            return user
        return checker

    @app.get("/health")
    def health():
        with db.connect() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/v1/services", status_code=201)
    def create_service(body: ServiceIn, user: dict = Depends(require("admin"))):
        service_id = new_id()
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO services VALUES (?,?,?)", (service_id, user["tenant_id"], body.name))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Service name already exists")
        return {"id": service_id, "name": body.name}

    @app.get("/v1/services")
    def list_services(user: dict = Depends(require("viewer"))):
        with db.connect() as conn:
            rows = conn.execute("SELECT id,name FROM services WHERE tenant_id=? ORDER BY name", (user["tenant_id"],)).fetchall()
        return [dict(row) for row in rows]

    @app.post("/v1/services/{service_id}/events", status_code=202)
    async def ingest(service_id: str, request: Request, x_relay_signature: str = Header(default="")):
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, "Event body too large")
        with db.connect() as conn:
            row = conn.execute("SELECT t.id,t.webhook_secret FROM services s JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?", (service_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Service not found")
        expected = "sha256=" + hmac.new(row["webhook_secret"].encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_relay_signature):
            raise HTTPException(401, "Invalid signature")
        try:
            event = EventIn.model_validate_json(raw)
        except Exception:
            raise HTTPException(422, "Invalid event")
        tenant_id = row["id"]
        timestamp = now()
        event_id = new_id()
        incident_id = new_id()
        with db.transaction() as conn:
            existing = conn.execute("SELECT id FROM events WHERE tenant_id=? AND external_id=?", (tenant_id, event.external_id)).fetchone()
            if existing:
                return {"event_id": existing["id"], "duplicate": True}
            conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (event_id, tenant_id, service_id, event.external_id, event.fingerprint, raw.decode(), timestamp))
            incident = conn.execute("SELECT id FROM incidents WHERE tenant_id=? AND service_id=? AND fingerprint=? AND status!='resolved'", (tenant_id, service_id, event.fingerprint)).fetchone()
            if incident:
                incident_id = incident["id"]
                action = "event_attached"
            else:
                conn.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?)", (incident_id, tenant_id, service_id, event.fingerprint, event.title, event.severity, "open", 1, timestamp, timestamp))
                conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", (new_id(), tenant_id, incident_id, "incident_opened", json.dumps({"incident_id": incident_id}), "pending", 0, timestamp, timestamp, None))
                action = "opened"
            conn.execute("INSERT INTO audit(tenant_id,incident_id,actor,action,occurred_at,detail) VALUES (?,?,?,?,?,?)", (tenant_id, incident_id, "webhook", action, timestamp, event.external_id))
        return {"event_id": event_id, "incident_id": incident_id, "duplicate": False}

    @app.get("/v1/incidents")
    def list_incidents(status: str | None = None, limit: int = 50, user: dict = Depends(require("viewer"))):
        if status not in (None, "open", "acknowledged", "resolved") or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid filter")
        query = "SELECT id,service_id,title,severity,status,version,created_at,updated_at FROM incidents WHERE tenant_id=?"
        args: list = [user["tenant_id"]]
        if status:
            query += " AND status=?"
            args.append(status)
        query += " ORDER BY created_at DESC,id DESC LIMIT ?"
        args.append(limit)
        with db.connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    @app.post("/v1/incidents/{incident_id}/{action}")
    def transition(incident_id: str, action: str, body: TransitionIn, user: dict = Depends(require("responder"))):
        targets = {"acknowledge": ("open", "acknowledged"), "resolve": ("acknowledged", "resolved")}
        if action not in targets:
            raise HTTPException(404, "Unknown action")
        source, target = targets[action]
        timestamp = now()
        with db.transaction() as conn:
            row = conn.execute("SELECT status,version FROM incidents WHERE id=? AND tenant_id=?", (incident_id, user["tenant_id"])).fetchone()
            if row is None:
                raise HTTPException(404, "Incident not found")
            if row["version"] != body.version or row["status"] != source:
                raise HTTPException(409, "Incident changed; reload before retrying")
            conn.execute("UPDATE incidents SET status=?,version=version+1,updated_at=? WHERE id=? AND tenant_id=?", (target, timestamp, incident_id, user["tenant_id"]))
            conn.execute("INSERT INTO audit(tenant_id,incident_id,actor,action,occurred_at,detail) VALUES (?,?,?,?,?,?)", (user["tenant_id"], incident_id, user["role"], action, timestamp, ""))
            conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", (new_id(), user["tenant_id"], incident_id, "incident_" + target, json.dumps({"incident_id": incident_id}), "pending", 0, timestamp, timestamp, None))
        return {"id": incident_id, "status": target, "version": body.version + 1}

    @app.post("/v1/outbox/{message_id}/requeue")
    def requeue(message_id: str, user: dict = Depends(require("admin"))):
        timestamp = now()
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT status, incident_id FROM outbox WHERE id=? AND tenant_id=?",
                (message_id, user["tenant_id"]),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Message not found")
            if row["status"] != "dead":
                raise HTTPException(409, "Only dead messages can be requeued")
            conn.execute(
                "UPDATE outbox SET status='pending', attempts=0, due_at=?, delivered_at=NULL WHERE id=? AND tenant_id=? AND status='dead'",
                (timestamp, message_id, user["tenant_id"]),
            )
            conn.execute(
                "INSERT INTO audit(tenant_id,incident_id,actor,action,occurred_at,detail) VALUES (?,?,?,?,?,?)",
                (user["tenant_id"], row["incident_id"], user["role"], "outbox_requeued", timestamp, message_id),
            )
        return {"id": message_id, "status": "pending"}

    @app.get("/v1/incidents/{incident_id}/audit")
    def audit(incident_id: str, user: dict = Depends(require("viewer"))):
        with db.connect() as conn:
            exists = conn.execute("SELECT 1 FROM incidents WHERE id=? AND tenant_id=?", (incident_id, user["tenant_id"])).fetchone()
            if not exists:
                raise HTTPException(404, "Incident not found")
            rows = conn.execute("SELECT actor,action,occurred_at,detail FROM audit WHERE incident_id=? AND tenant_id=? ORDER BY id", (incident_id, user["tenant_id"])).fetchall()
        return [dict(row) for row in rows]

    return app


app = create_app()
