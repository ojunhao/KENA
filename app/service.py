import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from .agent import INTENTS, POLICY, LocalAgent, assess
from .messaging import Simulator
from .store import Store
from .device_signals import call_context


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:12]


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def audit(journey, kind, detail):
    journey["audit"].append({"id": identifier("audit"), "at": now(), "kind": kind,
                             "detail": detail, "policy_version": POLICY["version"]})


def outbound(journey, body, channel=None):
    journey["messages"].append({"id": identifier("msg"), "at": now(), "direction": "outbound",
        "channel": channel or journey["channel"], "body": body, "status": "queued", "provider_id": None})


class JourneyService:
    def __init__(self, store: Store, agent=None, transport=None, clock=time.time):
        self.store = store
        self.agent = agent or LocalAgent()
        self.transport = transport or Simulator()
        self.clock = clock

    def load(self, db, journey_id):
        j = self.store.get(db, journey_id)
        if not j or j.get("deleted_at"):
            raise HTTPException(404, "Journey not found")
        return j

    @staticmethod
    def public(j, customer=False):
        result = json.loads(json.dumps(j))
        result.pop("event_fingerprint", None)
        result.pop("commands", None)
        if result["proposal"] and not customer:
            result["proposal"].pop("token", None)
        return result

    def create(self, event):
        payload = event.model_dump()
        digest = fingerprint(payload)
        with self.store.write() as db:
            row = db.execute("SELECT id FROM journeys WHERE event_id=?", (event.event_id,)).fetchone()
            if row:
                if self.store.get(db, row["id"]).get("deleted_at"):
                    raise HTTPException(409, "This event belongs to a deleted transfer")
                j = self.load(db, row["id"])
                if j["event_fingerprint"] != digest:
                    raise HTTPException(409, "Event ID reused with different payload")
                return self.public(j)
            risk = assess(payload)
            channel = "RCS" if event.preferred_channel == "RCS" and event.rcs_available else "SMS"
            j = {"id": identifier("txn"), "event_id": event.event_id, "event_fingerprint": digest,
                 "created_at": now(), "customer_id": event.customer_id, "event": payload,
                 "transaction_status": "pending", "journey_status": "awaiting_customer" if risk["level"] == "HIGH" else "not_flagged",
                 "risk": risk, "channel": channel, "case": None, "proposal": None,
                 "messages": [], "audit": [], "commands": {}, "agent_mode": self.agent.name,
                 "call_context": call_context(event.active_call), "deleted_at": None}
            audit(j, "risk_assessed", {"score": risk["score"], "signals": risk["signals"], "call_state": j["call_context"]["state"], "call_signal_source": "simulator"})
            if event.preferred_channel == "RCS" and channel == "SMS":
                audit(j, "channel_fallback", {"from": "RCS", "to": "SMS", "reason": "Customer RCS unavailable"})
            if risk["level"] == "HIGH":
                amount = event.amount_cents / 100
                question = ("Call context suggests you may be on a phone call. Is someone on the call instructing you to transfer this money?"
                            if event.active_call is True else "Did you authorise it, or did someone instruct you to send it?")
                outbound(j, f"KENA: We noticed a S${amount:,.2f} transfer to {event.payee}. {question} Reply in your own words. We will never ask for passwords or OTPs.")
            self.store.put(db, j)
        self.dispatch(j["id"])
        return self.read(j["id"])

    def read(self, journey_id, customer=False):
        with self.store.connection() as db:
            return self.public(self.load(db, journey_id), customer)

    def delete(self, journey_id):
        """Remove a synthetic transfer from active views, retaining the audit and event tombstone."""
        with self.store.write() as db:
            j = self.store.get(db, journey_id)
            if not j:
                raise HTTPException(404, "Journey not found")
            if not j.get("deleted_at"):
                j["deleted_at"] = now()
                j["proposal"] = None
                audit(j, "demo_transfer_deleted", {"scope": "Removed from demo views; no bank action or message recall"})
                self.store.put(db, j)
        return {"id": journey_id, "deleted": True}

    def customer(self, journey_id, customer_id):
        j = self.read(journey_id, True)
        if customer_id != j["customer_id"]:
            raise HTTPException(403, "Customer does not own this journey")
        return j

    def replay(self, j, key, digest):
        if key not in j["commands"]:
            return False
        if j["commands"][key] != digest:
            raise HTTPException(409, "Idempotency key reused with different payload")
        return True

    def reply(self, journey_id, customer_id, reply):
        digest = fingerprint(reply.model_dump())
        with self.store.write() as db:
            j = self.load(db, journey_id)
            if customer_id != j["customer_id"]:
                raise HTTPException(403, "Customer does not own this journey")
            key = "reply:" + reply.message_id
            if self.replay(j, key, digest):
                return self.public(j, True)
            if j["journey_status"] == "not_flagged":
                raise HTTPException(409, "This transfer has no outreach journey")
            j["commands"][key] = digest
            j["messages"].append({"id": reply.message_id, "at": now(), "direction": "inbound",
                "channel": j["channel"], "body": reply.text, "status": "received", "provider_id": None})
            try:
                intent = self.agent.classify(reply.text, {"amount_cents": j["event"]["amount_cents"], "risk": j["risk"], "journey_status": j["journey_status"], "call_context": j.get("call_context", {"state": "unknown"})})
                if intent not in INTENTS:
                    intent = "uncertain"
            except Exception:
                intent = "uncertain"
                audit(j, "agent_unavailable", {"fallback": "Clarify with no tool action"})
            audit(j, "reply_classified", {"intent": intent, "agent": self.agent.name})
            if j["transaction_status"] == "paused":
                outbound(j, f"This transfer remains paused. Case {j['case']['id']} is open for the fraud team. KENA cannot release or reverse funds.")
            elif intent == "dispute":
                if j["event"]["amount_cents"] > POLICY["max_protection_cents"]:
                    j["proposal"] = None
                    j["journey_status"] = "human_review"
                    audit(j, "policy_escalation", {"reason": "Amount exceeds automatic protection cap"})
                    outbound(j, "This transfer exceeds the automatic-protection limit. No banking action has been taken. Contact the bank through its official app for urgent human review.")
                else:
                    self.propose(j, customer_id, journey_id)
            elif intent == "cancel":
                j["proposal"] = None
                j["journey_status"] = "human_review"
                outbound(j, "Protection was not confirmed. No banking action was taken. The transfer is still pending; please contact the bank through its official app for assistance.")
            elif intent == "verify":
                j["proposal"] = None
                j["journey_status"] = "verified_review"
                audit(j, "customer_verified", {"funds_executed": False})
                outbound(j, "Thank you for confirming you initiated this transfer. Because the risk signals remain, it stays pending for bank review. KENA cannot release funds. If anyone pressured you to send money, tell us.")
            elif intent == "question":
                outbound(j, "This transfer was flagged because: " + "; ".join(j["risk"]["signals"]) + ". These signals are not proof of fraud. Did you make the transfer, or do you dispute it?")
            else:
                outbound(j, "I want to avoid guessing. You can say 'I made this transfer', 'I did not make this transfer', or 'someone told me to transfer this'. No banking action has been taken from this reply. A simple yes does not authorize protective actions.")
            self.store.put(db, j)
        self.dispatch(journey_id)
        return self.read(journey_id, True)

    def propose(self, j, customer_id, journey_id):
        j["journey_status"] = "awaiting_confirmation"
        j["proposal"] = {"token": secrets.token_urlsafe(32), "actions": POLICY["tools"].copy(),
            "expires_at": self.clock() + POLICY["confirmation_ttl_seconds"], "customer_id": customer_id,
            "transaction_id": journey_id, "policy_version": POLICY["version"]}
        audit(j, "protection_proposed", {"actions": POLICY["tools"], "expires_in_seconds": POLICY["confirmation_ttl_seconds"]})
        outbound(j, "I can pause this pending transfer and open a scam-review case. Nothing has been changed yet. Confirm both actions using the confirmation control, or reply cancel. If someone claimed to be your bank or the police, stop communicating with them and contact your bank through its official app.")

    def recover_queued(self):
        # Only safe for the side-effect-free local simulator. No AWS retry on startup.
        if not isinstance(self.transport, Simulator):
            return 0
        count = 0
        for j in self.store.all():
            if not j.get("deleted_at") and any(m["direction"] == "outbound" and m["status"] == "queued" for m in j["messages"]):
                self.dispatch(j["id"])
                count += 1
        return count

    def confirm(self, journey_id, customer_id, confirmation):
        digest = fingerprint(confirmation.model_dump())
        with self.store.write() as db:
            j = self.load(db, journey_id)
            if customer_id != j["customer_id"]:
                raise HTTPException(403, "Customer does not own this journey")
            key = "confirm:" + confirmation.command_id
            if self.replay(j, key, digest):
                return self.public(j, True)
            proposal = j["proposal"]
            if not proposal or not secrets.compare_digest(proposal["token"], confirmation.token):
                raise HTTPException(409, "Confirmation does not match the current proposal")
            if self.clock() >= proposal["expires_at"]:
                raise HTTPException(409, "Confirmation expired; dispute again to request a new proposal")
            if (len(confirmation.approved_actions) != 2 or set(confirmation.approved_actions) != set(POLICY["tools"])):
                raise HTTPException(422, "Explicit approval of both actions is required")
            if (j["journey_status"] != "awaiting_confirmation" or j["transaction_status"] != "pending"
                or j["risk"]["level"] != "HIGH" or j["event"]["currency"] != "SGD"
                or j["event"]["amount_cents"] > POLICY["max_protection_cents"]
                or proposal["transaction_id"] != journey_id or proposal["customer_id"] != customer_id
                or proposal["policy_version"] != POLICY["version"]):
                raise HTTPException(409, "Policy blocks this protection request; human review required")
            # Mock banking tools execute atomically with the case, audit and outbox.
            j["commands"][key] = digest
            audit(j, "customer_confirmed", {"actions": confirmation.approved_actions, "customer_id": customer_id})
            j["transaction_status"] = "paused"
            audit(j, "tool_executed", {"tool": "pause_transfer", "result": "paused", "mock": True})
            j["case"] = {"id": identifier("SCAM"), "status": "open", "created_at": now(), "reason": "Customer disputed suspicious transfer", "mock": True}
            audit(j, "tool_executed", {"tool": "create_scam_case", "case_id": j["case"]["id"], "mock": True})
            j["journey_status"] = "protected"
            j["proposal"] = None
            body = f"Your S${j['event']['amount_cents']/100:,.2f} transfer is paused. Scam-review case {j['case']['id']} is open. The fraud team will review it; this does not mean funds have been recovered. No passwords or OTPs are required."
            outbound(j, body)
            outbound(j, body, "SES")
            self.store.put(db, j)
        self.dispatch(journey_id)
        return self.read(journey_id, True)

    def dispatch(self, journey_id):
        # Simulator only in the HTTP app. Real transport needs an async worker and delivery receipts.
        with self.store.write() as db:
            j = self.load(db, journey_id)
            for msg in j["messages"]:
                if msg["direction"] != "outbound" or msg["status"] != "queued":
                    continue
                try:
                    msg.update(self.transport.send(msg))
                except Exception:
                    msg["status"] = "failed"
                    audit(j, "message_failed", {"message_id": msg["id"], "channel": msg["channel"], "reason": "Transport unavailable; no automatic retry"})
                else:
                    audit(j, "message_dispatched", {"message_id": msg["id"], "channel": msg["channel"], "status": msg["status"]})
            self.store.put(db, j)

    def snapshot(self):
        journeys = [self.public(j) for j in self.store.all() if not j.get("deleted_at")]
        return {"mode": "synthetic-local-demo", "agent": self.agent.name, "transport": self.transport.name,
                "policy": POLICY, "journeys": journeys,
                "metrics": {"flagged": sum(j["risk"]["level"] == "HIGH" for j in journeys),
                            "protected": sum(j["transaction_status"] == "paused" for j in journeys),
                            "cases": sum(j["case"] is not None for j in journeys),
                            "paused_value_cents": sum(j["event"]["amount_cents"] for j in journeys if j["transaction_status"] == "paused")}}
