"""Leased transactional outbox worker. Safe to restart at any boundary."""
import json
import os
import time
from datetime import datetime
from .workflows.service import PaydayService
from .domain.engine import DomainError, digest


def run_once(service):
    store, now = service.store, service.now()
    with store.transaction() as db:
        job = db.execute("SELECT * FROM outbox WHERE status IN ('pending','processing') AND lease_until<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        if not job:
            return None
        job = dict(job)
        db.execute("UPDATE outbox SET status='processing',lease_until=?,attempts=attempts+1 WHERE id=?", (time.time()+30, job["id"]))
        a = store.get(db, "actions", job["action_id"], job["tenant"])
        t = store.get(db, "payday_transfers", a["transfer_id"], job["tenant"])
    try:
        prior = service.bank.find_transfer(t["request_ref"])
        if prior["status"] == "not_found":
            service.refresh(job["tenant"], a["customer_id"])
            with store.transaction() as db:
                a = store.get(db, "actions", a["id"], job["tenant"])
                t = store.get(db, "payday_transfers", t["id"], job["tenant"])
                p = store.get(db, "profiles", a["customer_id"], job["tenant"])
                c = store.get(db, "cases", a["case_id"], job["tenant"])
                approval = store.get(db, "approvals", a["approval_id"], job["tenant"])
                valid = (not approval["revoked_at"] and datetime.fromisoformat(now) < datetime.fromisoformat(approval["expires_at"])
                         and approval["action_payload_hash"] == digest(a["payload"])
                         and a["payload"] == t["payload"] and p["account_ownership_verified"]
                         and a["approved_revision"] == p["input_revision"]
                         and a["approved_bank_version"] == p["bank_snapshot"]["version"])
                if not valid:
                    target = "stale_inputs" if c["status"] == "reserved" else "failed"
                    service._transition(db, c, target, "executor", now, {"reason":"Authority expired, revoked, or cash changed before execution.", "confirmed_non_execution":True})
                    t["status"], a["status"] = "failed", target
                    store.put(db, "payday_transfers", t, job["tenant"], p["id"])
                    store.put(db, "actions", a, job["tenant"], p["id"])
                    service._reallocate(db, p, p["bank_snapshot"], now, "reservation_released")
                    db.execute("UPDATE outbox SET status='done',lease_until=0 WHERE id=?", (job["id"],))
                    return {"status":target}
                if c["status"] in ("reserved", "manual_review"):
                    service._transition(db, c, "submitted", "executor", now, {"request_ref":t["request_ref"]})
                a["status"], t["status"] = "submitted", "submitted"
                approval["consumed_at"] = approval["consumed_at"] or now
                for kind, obj in (("actions", a), ("payday_transfers", t), ("approvals", approval)):
                    store.put(db, kind, obj, job["tenant"], p["id"])
            # Stable reference makes a repeat after a worker crash idempotent at the bank.
            try:
                prior = service.bank.submit_same_owner_transfer(t["payload"], t["request_ref"], now)
                if prior.get("status") not in ("accepted", "posted", "failed", "returned"):
                    raise TimeoutError("Malformed response; outcome is unknown until lookup.")
            except (TimeoutError, ConnectionError) as exc:
                with store.transaction() as db:
                    c = store.get(db, "cases", a["case_id"], job["tenant"])
                    if c["status"] == "submitted":
                        service._transition(db, c, "outcome_unknown", "executor", now, {"reason":str(exc), "request_ref":t["request_ref"]})
                    a["status"] = t["status"] = "outcome_unknown"
                    store.put(db, "actions", a, job["tenant"], a["customer_id"])
                    store.put(db, "payday_transfers", t, job["tenant"], a["customer_id"])
        service.refresh(job["tenant"], a["customer_id"])
        with store.transaction() as db:
            t = store.get(db, "payday_transfers", t["id"], job["tenant"])
            done = t["status"] in ("reconciled", "returned", "failed")
            db.execute("UPDATE outbox SET status=?,lease_until=?,last_error=NULL WHERE id=?",
                       ("done" if done else "pending", 0 if done else time.time()+2, job["id"]))
        return t
    except Exception as exc:
        with store.transaction() as db:
            held = job["attempts"] + 1 >= 3
            if held:
                c = store.get(db, "cases", a["case_id"], job["tenant"])
                current = store.get(db, "payday_transfers", t["id"], job["tenant"])
                action = store.get(db, "actions", a["id"], job["tenant"])
                if c["status"] in ("reserved", "submitted", "outcome_unknown"):
                    service._transition(db, c, "manual_review", "executor", now, {"reason":str(exc)[:200], "attempts":job["attempts"]+1, "request_ref":t["request_ref"]})
                    current["status"] = action["status"] = "manual_review"
                    store.put(db, "payday_transfers", current, job["tenant"], a["customer_id"])
                    store.put(db, "actions", action, job["tenant"], a["customer_id"])
            db.execute("UPDATE outbox SET status=?,lease_until=?,last_error=? WHERE id=?",
                       ("held" if held else "pending", time.time()+min(60,2**min(job["attempts"]+1,6)), str(exc)[:200], job["id"]))
        raise


def main():
    service = PaydayService(os.getenv("PAYDAY_DATA_DIR", "data"))
    service.seed()
    while True:
        try:
            run_once(service)
        except Exception as exc:
            print(json.dumps({"worker_error":type(exc).__name__, "message":str(exc)[:160]}), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
