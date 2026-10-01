"""Durable local provider simulator. Its own transactions never overlap case writes."""
import json
from typing import Protocol
from app.domain.documents import digest

class TaxFilingAdapter(Protocol):
    def validate_package(self, package: dict) -> dict: ...
    def submit_mock_return(self, package: dict, request_ref: str) -> dict: ...
    def find_submission(self, request_ref: str) -> dict | None: ...
    def get_financial_outcome(self, submission_ref: str) -> dict: ...

class MockTaxFilingAdapter:
    environment = "mock"
    capabilities = {"lookup_by_request_ref": True, "live_filing": False, "refund_destination_changes": False}
    def __init__(self, store):
        self.store = store
    def validate_package(self, package):
        if package["environment"] != "mock" or package["destination"] != "Mock Federal Filing Service":
            raise ValueError("Only the mock filing destination is available")
        return {"valid": True, "environment": "mock"}
    def find_submission(self, request_ref):
        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM provider_submissions WHERE request_ref=?", (request_ref,)).fetchone()
            return json.loads(row["data"]) if row else None
    def submit_mock_return(self, package, request_ref):
        self.validate_package(package)
        with self.store.transaction() as db:
            prior = db.execute("SELECT data FROM provider_submissions WHERE request_ref=?", (request_ref,)).fetchone()
            if prior:
                result = json.loads(prior["data"])
                if result["package_hash"] != digest(package):
                    raise ValueError("Idempotency key reused with different content")
                return result
            ref = "efile_mock_" + request_ref[-16:]
            result = {"submission_ref": ref, "request_ref": request_ref, "package_hash": digest(package), "environment": "mock", "status": "received", "case_id": package["case_id"], "refund_minor": package["calculation"]["refund_minor"], "amount_due_minor": package["calculation"]["amount_due_minor"], "submitted_day": package["clock_day"], "scenario": package["scenario"], "revision": package["revision"]}
            prior_case = any(json.loads(row["data"])["case_id"] == package["case_id"] for row in db.execute("SELECT data FROM provider_submissions").fetchall())
            result["reject_identity"] = package["scenario"] == "rejection" and not prior_case
            if package["scenario"] == "timeout":
                result["status"] = "accepted"
            db.execute("INSERT INTO provider_submissions(request_ref,submission_ref,data) VALUES (?,?,?)", (request_ref, ref, json.dumps(result)))
        if package["scenario"] == "timeout":
            raise TimeoutError("Mock connection dropped after provider acceptance")
        if package["scenario"] == "malformed":
            return {"environment": "mock", "unexpected": "malformed fixture"}
        return result
    def get_financial_outcome(self, submission_ref):
        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM provider_submissions WHERE submission_ref=?", (submission_ref,)).fetchone()
            if not row:
                raise KeyError("Submission not found")
            data = json.loads(row["data"])
            return {**data, "financial_status": data.get("financial_status", "pending_account_evidence")}
    def events(self, submission, day):
        elapsed = day - submission["submitted_day"]
        delayed = submission["scenario"] == "delayed"
        reject = submission.get("reject_identity", submission["scenario"] == "rejection" and submission["revision"] == 1)
        schedule = [(3 if delayed else 1, "rejected" if reject else "accepted")]
        if not reject and submission["refund_minor"]:
            schedule += [(5 if delayed else 2, "refund_notice"), (7 if delayed else 3, "account_credit")]
        output = []
        for when, kind in schedule:
            if elapsed >= when:
                output.append({"id": submission["submission_ref"] + ":" + kind, "case_id": submission["case_id"], "submission_ref": submission["submission_ref"], "type": kind, "environment": "mock", "amount_minor": submission["refund_minor"], "account_event_ref": "acct_mock_" + submission["submission_ref"] if kind == "account_credit" else None, "code": "R0000-500-01" if kind == "rejected" else "MOCK-ACCEPTED" if kind == "accepted" else None})
        if output:
            latest = dict(submission)
            latest["status"] = "rejected" if reject else "accepted"
            kinds = {e["type"] for e in output}
            latest["financial_status"] = "account_credit_reported" if "account_credit" in kinds else "refund_notice" if "refund_notice" in kinds else "pending_account_evidence"
            with self.store.transaction() as db:
                db.execute("UPDATE provider_submissions SET data=? WHERE request_ref=?", (json.dumps(latest), submission["request_ref"]))
        return output
