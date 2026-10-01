import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ..adapters.mock import MockBank
from ..domain.engine import ALLOWED, DomainError, allocate, calculate, digest, tax_target
from ..persistence.store import ROOT, Store, uid

TENANT = "tenant_demo"
CUSTOMER = "cus_demo_12"


class PaydayService:
    def __init__(self, data_dir):
        self.store = Store(Path(data_dir) / "payday.sqlite3")
        self.bank = MockBank(Path(data_dir) / "mock-bank.sqlite3")

    def now(self):
        with self.store.transaction() as db:
            values = self.store.all(db, "settings", TENANT)
            return values[0]["clock"] if values else "2026-10-01T09:00:00+00:00"

    def seed(self, tenant=TENANT, customer=CUSTOMER, scenario="supported"):
        fixture = json.loads((ROOT / "fixtures/customer.json").read_text())
        profile = {"id": customer, "customer_id": customer, "tenant": tenant,
                   "name": fixture["name"], "business_name": fixture["business_name"],
                   "business_account_id": customer + "_business_4821", "personal_account_id": customer + "_personal_0916",
                   "account_ownership_verified": True, "currency": "USD", "input_revision": 1,
                   "policy": fixture["policy"], "receipts": fixture["receipts"], "bills": fixture["bills"],
                   "invoices": fixture["invoices"], "buckets": {}, "fixture_version": fixture["fixture_version"]}
        available = 750000 if scenario == "late" else fixture["available_minor"]
        if scenario == "late":
            profile["receipts"].append({"id":"rcpt_equipment","provider_transaction_id":"txn_equipment","client":"Studio equipment","description":"Posted business purchase","amount_minor":-250000,"category":"expense","confirmed":True,"posted_at":"2026-09-29","evidence_id":"fixture:txn_equipment"})
        self.bank.seed(profile["business_account_id"], profile["personal_account_id"], customer, available)
        with self.store.transaction() as db:
            if not self.store.all(db, "profiles", tenant, customer):
                self.store.put(db, "profiles", profile, tenant, customer)
            if not self.store.all(db, "settings", TENANT):
                self.store.put(db, "settings", {"id":"clock", "clock":fixture["clock"]}, TENANT, "system")
        self.refresh(tenant, customer)
        return profile

    def profile(self, tenant, customer):
        with self.store.transaction() as db:
            return self.store.get(db, "profiles", customer, tenant)

    def case(self, tenant, case_id):
        with self.store.transaction() as db:
            return self.store.get(db, "cases", case_id, tenant)

    def _transition(self, db, case, state, actor, now, detail=None):
        previous = case["status"]
        if state not in ALLOWED.get(previous, set()):
            raise DomainError("Invalid workflow transition: " + previous + " → " + state)
        old_version = case["version"]
        case.update(status=state, version=old_version + 1, updated_at=now)
        self.store.put(db, "cases", case, case["tenant"], case["customer_id"])
        db.execute("INSERT INTO case_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (uid("evt"), case["tenant"], case["id"], case["version"], state, previous, state, actor, now, old_version, json.dumps(detail or {})))

    def create_case(self, tenant, customer, requested, period="2026-10"):
        now = self.now()
        with self.store.transaction() as db:
            self.store.get(db, "profiles", customer, tenant)
            case = {"id":uid("case"), "tenant":tenant, "customer_id":customer, "requested_minor":requested,
                    "period":period, "currency":"USD", "status":"collecting", "version":1,
                    "created_at":now, "updated_at":now, "completion_evidence":None}
            self.store.put(db, "cases", case, tenant, customer)
            db.execute("INSERT INTO case_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (uid("evt"), tenant, case["id"], 1, "collecting", None, "collecting", "customer", now, 0, '{}'))
        return case

    def _targets(self, p, now):
        tax, allocations = tax_target(p["receipts"], p["policy"]["provisional_tax_fraction_decimal"])
        horizon = (datetime.fromisoformat(now) + timedelta(days=p["policy"]["planning_horizon_days"])).date().isoformat()
        # Overdue unpaid commitments remain protected; each bill identity appears once.
        operating = sum(b["amount_minor"] for b in p["bills"] if b["confirmed"] and not b.get("paid") and b["due_date"] <= horizon)
        return {"tax":tax, "operating":operating, "emergency":p["policy"]["emergency_floor_minor"]}, allocations

    def _pending(self, db, tenant, customer):
        return sum(t["amount_minor"] for t in self.store.all(db, "payday_transfers", tenant, customer)
                   if t["status"] in ("reserved", "submitted", "outcome_unknown", "manual_review"))

    def _snapshot(self, p, bank, targets, reservations):
        return digest({"bank_version":bank["version"], "available_minor":bank["available_minor"],
                       "input_revision":p["input_revision"], "policy":p["policy"], "targets":targets,
                       "reservations":reservations, "destination":p["personal_account_id"], "ownership":p["account_ownership_verified"]})

    def _reallocate(self, db, p, bank, now, reference="cash_refresh"):
        targets, allocations = self._targets(p, now)
        reserved = self._pending(db, p["tenant"], p["id"])
        new = allocate(bank["available_minor"], targets, reserved)
        self.store.journal(db, p["tenant"], p["id"], uid("allocation"), reference, p["id"], now, p["buckets"], new)
        p.update(buckets=new, bank_snapshot={**bank, "retrieved_at":now}, tax_allocations=allocations,
                 snapshot_id=self._snapshot(p, bank, targets, reserved), protected_targets=targets)
        self.store.put(db, "profiles", p, p["tenant"], p["id"])
        return p

    def _verify_entries(self, transfer, result, entries, returned=False):
        p = result["payload"]
        if p != transfer["payload"] or result["ref"] != transfer["request_ref"]:
            raise DomainError("Provider evidence differs from the approved transfer.")
        expected = [(result.get("debit_ref"), p["source_account_id"], -p["amount_minor"], result["ref"]),
                    (result.get("credit_ref"), p["destination_account_id"], p["amount_minor"], result["ref"])]
        if returned:
            expected.extend([(result.get("return_debit_ref"), p["destination_account_id"], -p["amount_minor"], result["ref"] + ":return"),
                             (result.get("return_credit_ref"), p["source_account_id"], p["amount_minor"], result["ref"] + ":return")])
        for ident, account, amount, ref in expected:
            e = entries.get(ident)
            if not e or (e["account"], e["amount"], e["reference"]) != (account, amount, ref):
                raise DomainError("Both matching bank entries are required before reconciliation.")

    def _apply_result(self, db, p, transfer, result, entries, now):
        status = result.get("status")
        if status not in ("posted", "returned", "failed"):
            return
        case = self.store.get(db, "cases", transfer["case_id"], p["tenant"])
        action = self.store.get(db, "actions", transfer["action_id"], p["tenant"])
        if status in ("posted", "returned") and transfer["status"] not in ("posted", "reconciled", "returned"):
            self._verify_entries(transfer, result, entries, status == "returned")
            old, new = dict(p["buckets"]), dict(p["buckets"])
            new["reserved"] -= transfer["amount_minor"]
            if new["reserved"] < 0:
                raise DomainError("Transfer reservation is missing; manual review required.")
            self.store.journal(db, p["tenant"], p["id"], transfer["id"] + ":posted", "payout_posted", transfer["id"], now, old, new)
            p["buckets"] = new
            if case["status"] == "reserved":
                self._transition(db, case, "submitted", "reconciler", now)
            self._transition(db, case, "posted", "reconciler", now, {"debit_ref":result["debit_ref"], "credit_ref":result["credit_ref"]})
            case["completion_evidence"] = {"provider_ref":result["provider_ref"], "debit_ref":result["debit_ref"], "credit_ref":result["credit_ref"], "environment":"mock"}
            self._transition(db, case, "reconciled", "reconciler", now, case["completion_evidence"])
            transfer.update(status="reconciled", provider_transfer_ref=result["provider_ref"], evidence=case["completion_evidence"])
            action["status"] = "reconciled"
        if status == "returned" and transfer["status"] != "returned":
            self._verify_entries(transfer, result, entries, True)
            new = dict(p["buckets"])
            new["unallocated"] += transfer["amount_minor"]
            self.store.journal(db, p["tenant"], p["id"], transfer["id"] + ":return", "payout_returned", transfer["id"], now, p["buckets"], new)
            p["buckets"] = new
            self._transition(db, case, "returned", "reconciler", now, {"return_credit_ref":result["return_credit_ref"]})
            self._transition(db, case, "recovery_review", "reconciler", now, {"message":"Cash restored. Calculate a new plan and obtain fresh approval."})
            transfer.update(status="returned", return_evidence={"debit_ref":result["return_debit_ref"], "credit_ref":result["return_credit_ref"]})
            action["status"] = "returned"
        if status == "failed" and transfer["status"] not in ("failed", "returned", "reconciled"):
            if case["status"] == "reserved":
                self._transition(db, case, "submitted", "reconciler", now)
            self._transition(db, case, "failed", "reconciler", now, {"provider_ref":result["provider_ref"], "confirmed_non_execution":True})
            transfer.update(status="failed", provider_transfer_ref=result["provider_ref"])
            action["status"] = "failed"
        self.store.put(db, "payday_transfers", transfer, p["tenant"], p["id"])
        self.store.put(db, "actions", action, p["tenant"], p["id"])

    def refresh(self, tenant, customer):
        p = self.profile(tenant, customer)
        with self.store.transaction() as db:
            transfers = self.store.all(db, "payday_transfers", tenant, customer)
        # Provider reads/writes always occur outside an application transaction.
        # A posting between reads must not mix a pre-post status with post-debit cash.
        for attempt in range(3):
            before = [self.bank.get_available_cash(account)["version"] for account in (p["business_account_id"], p["personal_account_id"])]
            results = {t["id"]:self.bank.find_transfer(t["request_ref"]) for t in transfers}
            entries = {e["id"]:e for account in (p["business_account_id"], p["personal_account_id"])
                       for e in self.bank.get_posted_transactions(account)["transactions"]}
            bank = self.bank.get_available_cash(p["business_account_id"])
            personal = self.bank.get_available_cash(p["personal_account_id"])
            if before == [bank["version"], personal["version"]]:
                break
        else:
            raise DomainError("Bank cash is changing; retry reconciliation with a stable snapshot.")
        now = self.now()
        with self.store.transaction() as db:
            p = self.store.get(db, "profiles", customer, tenant)
            # Do not replace a newer observation with a stale concurrent read.
            if p.get("bank_snapshot", {}).get("version", 0) > bank["version"]:
                return p
            for stale in transfers:
                current = self.store.get(db, "payday_transfers", stale["id"], tenant)
                self._apply_result(db, p, current, results[stale["id"]], entries, now)
            p["personal_snapshot"] = personal
            return self._reallocate(db, p, bank, now)

    def calculate_case(self, tenant, case_id):
        case = self.case(tenant, case_id)
        p = self.refresh(tenant, case["customer_id"])
        now = self.now()
        with self.store.transaction() as db:
            case = self.store.get(db, "cases", case_id, tenant)
            p = self.store.get(db, "profiles", case["customer_id"], tenant)
            if case["status"] in ("reserved", "submitted", "outcome_unknown", "manual_review", "posted", "reconciled"):
                raise DomainError("This payout is already in progress or completed. Create the next payday case.")
            if any(not r["confirmed"] for r in p["receipts"]):
                raise DomainError("Confirm the unclassified receipt before preparing a payday.")
            if any(not b["confirmed"] for b in p["bills"]):
                raise DomainError("Confirm operating commitments before planning.")
            if case["status"] == "collecting":
                self._transition(db, case, "cash_reconciled", "planner", now)
            if case["status"] == "awaiting_approval":
                self._invalidate(db, p, now)
                case = self.store.get(db, "cases", case_id, tenant)
            self._transition(db, case, "planning", "planner", now)
            v = calculate(p["bank_snapshot"]["available_minor"], **{k:p["protected_targets"][k] for k in ("tax", "operating", "emergency")},
                          reservations=self._pending(db, tenant, p["id"]), requested=case["requested_minor"])
            if v["shortfall_minor"]:
                self._transition(db, case, "shortfall_review", "planner", now, {"shortfall_minor":v["shortfall_minor"]})
            expected = sum(i["amount_minor"] for i in p["invoices"] if i["status"] != "paid")
            v.update(id=uid("proposal"), case_id=case_id, snapshot_id=p["snapshot_id"], policy_version=p["policy"]["version"],
                     created_at=now, unpaid_invoices_minor=expected, status="proposal_ready", environment="mock",
                     explanation=("The unpaid invoice is excluded. " + ("Available cash supports a smaller payday while keeping your reserves protected." if v["shortfall_minor"] else "Your requested payday fits within verified cash. Your reserves stay protected.")),
                     scenarios=[{"label":"Client pays late", "executable":True, "capacity_minor":v["capacity_minor"]},
                                {"label":"If all invoices arrive", "executable":False, "capacity_minor":max(0, v["available_minor"] + expected - int(expected * __import__('decimal').Decimal(p["policy"]["provisional_tax_fraction_decimal"])) - sum(v["protected"].values()) - v["reservations_minor"])}])
            self.store.put(db, "payday_proposals", v, tenant, p["id"])
            case["proposal_id"] = v["id"]
            self._transition(db, case, "proposal_ready", "planner", now, {"proposal_id":v["id"], "snapshot_id":p["snapshot_id"]})
            return {"case":case, "proposal":v}

    def draft(self, tenant, case_id, proposal_id, key, mode="normal"):
        case = self.case(tenant, case_id)
        self.refresh(tenant, case["customer_id"])
        now = self.now()
        intent = digest({"case_id":case_id, "proposal_id":proposal_id, "mode":mode})
        with self.store.transaction() as db:
            for a in self.store.all(db, "actions", tenant):
                if a["idempotency_key"] == key:
                    if a["intent_hash"] != intent:
                        raise DomainError("Idempotency key reused with different content.")
                    return a
            case = self.store.get(db, "cases", case_id, tenant)
            p = self.store.get(db, "profiles", case["customer_id"], tenant)
            v = self.store.get(db, "payday_proposals", proposal_id, tenant)
            if v["case_id"] != case_id or case.get("proposal_id") != proposal_id or case["status"] != "proposal_ready" or v["snapshot_id"] != p["snapshot_id"]:
                raise DomainError("Cash or policy changed. Calculate a fresh proposal.")
            if not p["account_ownership_verified"] or v["feasible_minor"] <= 0:
                raise DomainError("A positive payout and verified same-owner destination are required.")
            payload = {"source_account_id":p["business_account_id"], "destination_account_id":p["personal_account_id"],
                       "amount_minor":v["feasible_minor"], "currency":"USD", "snapshot_id":v["snapshot_id"],
                       "policy_version":v["policy_version"], "simulation_mode":mode, "environment":"mock"}
            action = {"id":uid("action"), "case_id":case_id, "customer_id":p["id"], "type":"payday_transfer", "payload":payload,
                      "action_payload_hash":digest(payload), "intent_hash":intent, "idempotency_key":key,
                      "approval_challenge_id":uid("challenge"), "expires_at":(datetime.fromisoformat(now)+timedelta(minutes=15)).isoformat(),
                      "status":"awaiting_approval", "created_at":now}
            self._transition(db, case, "awaiting_approval", "customer", now, {"action_id":action["id"]})
            action["expected_case_version"] = case["version"]
            self.store.put(db, "actions", action, tenant, p["id"])
            return action

    def approve(self, tenant, action_id, body, actor):
        with self.store.transaction() as db:
            a = self.store.get(db, "actions", action_id, tenant)
        self.refresh(tenant, a["customer_id"])
        now = self.now()
        error = None
        with self.store.transaction() as db:
            a = self.store.get(db, "actions", action_id, tenant)
            case = self.store.get(db, "cases", a["case_id"], tenant)
            p = self.store.get(db, "profiles", a["customer_id"], tenant)
            if a["status"] != "awaiting_approval":
                raise DomainError("This approval is no longer available.")
            if body["action_payload_hash"] != a["action_payload_hash"] or body["approval_challenge_id"] != a["approval_challenge_id"]:
                raise DomainError("Approval challenge or payload does not match.")
            if body["expected_case_version"] != case["version"]:
                raise DomainError("Case version changed. Review the latest proposal.")
            if datetime.fromisoformat(now) >= datetime.fromisoformat(a["expires_at"]):
                self._transition(db, case, "expired", actor, now)
                a["status"] = "expired"
                error = "Approval expired. Calculate a fresh proposal."
            elif a["payload"]["snapshot_id"] != p["snapshot_id"]:
                self._transition(db, case, "stale_inputs", actor, now)
                a["status"] = "stale_inputs"
                error = "Cash or protected reserves changed. Calculate a fresh proposal."
            else:
                capacity = calculate(p["bank_snapshot"]["available_minor"], **p["protected_targets"], reservations=self._pending(db, tenant, p["id"]), requested=a["payload"]["amount_minor"])
                if capacity["feasible_minor"] < a["payload"]["amount_minor"]:
                    raise DomainError("Unreserved cash cannot support this transfer.")
                approval = {"id":uid("approval"), "action_id":a["id"], "actor":actor,
                            "action_payload_hash":a["action_payload_hash"], "expires_at":a["expires_at"], "approved_at":now,
                            "revoked_at":None, "consumed_at":None, "scope":"exact_same_owner_transfer"}
                self.store.put(db, "approvals", approval, tenant, p["id"])
                transfer = {"id":uid("transfer"), "case_id":case["id"], "action_id":a["id"], "proposal_id":case["proposal_id"],
                            "request_ref":uid("payday"), "amount_minor":a["payload"]["amount_minor"], "payload":a["payload"],
                            "status":"reserved", "provider_transfer_ref":None}
                self.store.put(db, "payday_transfers", transfer, tenant, p["id"])
                a.update(status="reserved", approval_id=approval["id"], transfer_id=transfer["id"], approved_revision=p["input_revision"], approved_bank_version=p["bank_snapshot"]["version"])
                self._transition(db, case, "reserved", actor, now, {"approval_id":approval["id"], "amount_minor":transfer["amount_minor"]})
                self._reallocate(db, p, p["bank_snapshot"], now, "payout_reserved")
                db.execute("INSERT INTO outbox(id,tenant,action_id) VALUES(?,?,?)", (uid("job"), tenant, a["id"]))
            self.store.put(db, "actions", a, tenant, p["id"])
        if error:
            raise DomainError(error)
        return a

    def cancel(self, tenant, action_id, actor):
        now = self.now()
        with self.store.transaction() as db:
            a = self.store.get(db, "actions", action_id, tenant)
            if a["status"] != "awaiting_approval":
                raise DomainError("Only an unapproved draft can be cancelled.")
            c = self.store.get(db, "cases", a["case_id"], tenant)
            self._transition(db, c, "cancelled", actor, now)
            a["status"] = "cancelled"
            self.store.put(db, "actions", a, tenant, a["customer_id"])
        return a

    def _invalidate(self, db, p, now):
        for a in self.store.all(db, "actions", p["tenant"], p["id"]):
            if a["status"] == "awaiting_approval":
                c = self.store.get(db, "cases", a["case_id"], p["tenant"])
                self._transition(db, c, "stale_inputs", "input_change", now)
                a["status"] = "stale_inputs"
                self.store.put(db, "actions", a, p["tenant"], p["id"])
        p["input_revision"] += 1

    def change_inputs(self, tenant, customer, update, actor="customer"):
        now = self.now()
        with self.store.transaction() as db:
            p = self.store.get(db, "profiles", customer, tenant)
            if self._pending(db, tenant, customer):
                raise DomainError("Resolve the pending payout before changing protected cash inputs.")
            update(p)
            self._invalidate(db, p, now)
            self.store.put(db, "profiles", p, tenant, customer)
        return self.refresh(tenant, customer)

    def confirm(self, tenant, case_id, confirmations):
        c = self.case(tenant, case_id)
        def update(p):
            for choice in confirmations:
                receipt = next((r for r in p["receipts"] if r["id"] == choice["transaction_id"]), None)
                if not receipt:
                    raise DomainError("Receipt not found.", 404)
                if receipt["amount_minor"] < 0 or receipt["category"] in ("refund", "expense"):
                    raise DomainError("Posted debits and linked refunds cannot be relabeled as income.")
                receipt.setdefault("interpretation_history", []).append({"category":receipt["category"], "confirmed":receipt["confirmed"]})
                receipt.update(category=choice["category"], confirmed=True)
        return self.change_inputs(tenant, c["customer_id"], update)

    def dashboard(self, tenant, customer):
        p = self.refresh(tenant, customer)
        with self.store.transaction() as db:
            result = {"profile":p, "clock":self.now_without_tx(db), "environment":"mock"}
            for kind in ("cases", "payday_proposals", "actions", "payday_transfers", "policy_reviews"):
                result[kind] = self.store.all(db, kind, tenant, customer)
            result["timeline"] = [dict(r) for r in db.execute("SELECT e.* FROM case_events e JOIN entities c ON c.id=e.case_id AND c.kind='cases' WHERE e.tenant=? AND c.customer=? ORDER BY e.rowid DESC LIMIT 100", (tenant, customer)).fetchall()]
            result["journals"] = [{**dict(r), "lines":json.loads(r["lines"])} for r in db.execute("SELECT * FROM bucket_journals WHERE tenant=? AND customer=? ORDER BY rowid DESC LIMIT 100", (tenant, customer)).fetchall()]
            result["tool_runs"] = [dict(r) for r in db.execute("SELECT * FROM tool_runs WHERE tenant=? ORDER BY rowid DESC LIMIT 30", (tenant,)).fetchall()]
            result["jobs"] = [dict(r) for r in db.execute("SELECT * FROM outbox WHERE tenant=? ORDER BY rowid DESC", (tenant,)).fetchall()]
        return result

    def now_without_tx(self, db):
        return self.store.get(db, "settings", "clock", TENANT)["clock"]

    def policy_review(self, tenant, customer, policy):
        now = self.now()
        p = self.profile(tenant, customer)
        review = {"id":uid("policy_review"), "customer_id":customer, "old_policy":p["policy"], "new_policy":policy,
                  "base_revision":p["input_revision"], "action_payload_hash":digest(policy), "approval_challenge_id":uid("challenge"),
                  "expires_at":(datetime.fromisoformat(now)+timedelta(minutes=15)).isoformat(), "status":"awaiting_approval"}
        with self.store.transaction() as db:
            self.store.put(db, "policy_reviews", review, tenant, customer)
        return review

    def approve_policy(self, tenant, review_id, body, actor):
        now = self.now()
        with self.store.transaction() as db:
            r = self.store.get(db, "policy_reviews", review_id, tenant)
            p = self.store.get(db, "profiles", r["customer_id"], tenant)
            if r["status"] != "awaiting_approval" or datetime.fromisoformat(now) >= datetime.fromisoformat(r["expires_at"]) or p["input_revision"] != r["base_revision"] or body["action_payload_hash"] != r["action_payload_hash"] or body["approval_challenge_id"] != r["approval_challenge_id"]:
                raise DomainError("Policy review expired or changed. Review it again.")
            if self._pending(db, tenant, p["id"]):
                raise DomainError("Resolve the pending payout before changing reserve policy.")
            p["policy"] = {**p["policy"], **r["new_policy"], "version":p["policy"]["version"]+1, "approved_at":now, "approved_by":actor}
            r.update(status="approved", actor=actor, approved_at=now)
            self._invalidate(db, p, now)
            self.store.put(db, "profiles", p, tenant, p["id"])
            self.store.put(db, "policy_reviews", r, tenant, p["id"])
        return self.refresh(tenant, p["id"])

    def advance(self, seconds):
        with self.store.transaction() as db:
            clock = self.store.get(db, "settings", "clock", TENANT)
            clock["clock"] = (datetime.fromisoformat(clock["clock"]) + timedelta(seconds=seconds)).isoformat()
            self.store.put(db, "settings", clock, TENANT, "system")
        return clock
