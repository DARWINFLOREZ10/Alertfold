import argparse
import hashlib
import logging
import os
import secrets
import uuid

from .db import Database
from .worker import run


def main():
    parser = argparse.ArgumentParser(description="RelayOps administration")
    parser.add_argument("command", choices=["bootstrap", "worker"])
    parser.add_argument("--tenant", default="demo")
    args = parser.parse_args()
    db = Database(os.getenv("RELAYOPS_DB", "data/relayops.db"))
    if args.command == "worker":
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
        run(db)
    else:
        tenant_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        webhook_secret = secrets.token_urlsafe(32)
        with db.transaction() as conn:
            conn.execute("INSERT INTO tenants VALUES (?,?,?)", (tenant_id, args.tenant, webhook_secret))
            conn.execute("INSERT INTO api_keys VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), tenant_id, "admin"))
        print(f"TENANT_ID={tenant_id}\nADMIN_TOKEN={token}\nWEBHOOK_SECRET={webhook_secret}")
        print("Save these values securely; the admin token cannot be recovered.")


if __name__ == "__main__":
    main()
