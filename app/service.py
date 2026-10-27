import hashlib
import json
import logging
import secrets
import statistics
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from .agent import CLARIFY, NEXT_STATUS, AgentRefusal, LocalAgent, assess, money
from .config import Settings, digits
from .device_signals import call_context
from .messaging import CHANNELS, Simulator
from .models import Confirmation, Reply
from .policy import POLICY, channel_of, evaluate
from .store import Store

OPEN_STATES = {"awaiting_customer", "awaiting_confirmation"}
CONVERSATION_CHANNELS = ("WHATSAPP", "RCS", "SMS")
STATUS_ORDER = {"queued": 0, "simulated": 1, "accepted": 1, "sent": 2, "delivered": 3, "read": 4}
OUTREACH_BUTTONS = [{"id": "reply:verify", "title": "Yes, I made it"}, {"id": "reply:dispute", "title": "No, not me"},
                    {"id": "reply:coached", "title": "Someone told me to"}]
BUTTON_TEXT = {"reply:verify": "I made this transfer", "reply:dispute": "I did not make this transfer",
               "reply:coached": "someone told me to transfer this", "reply:cancel": "cancel"}
# Typed replies or WhatsApp template quick-reply titles that match a button are treated as that button.
TITLES = {"yes, i made it": "reply:verify", "yes, it was me": "reply:verify", "no, not me": "reply:dispute",
          "no, it wasn't me": "reply:dispute", "someone told me to": "reply:coached"}
MAX_FOLLOW_UPS = 2
log = logging.getLogger("kena")


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:12]


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def proposal_ref(token):
    """Non-secret handle put on the WhatsApp Confirm button; the token itself never leaves the server."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def audit(journey, kind, detail):
    journey["audit"].append({"id": identifier("audit"), "at": now(), "kind": kind,
                             "detail": detail, "policy_version": POLICY["version"]})


def outbound(journey, body, channel=None, **extra):
    message = {"id": identifier("msg"), "at": now(), "direction": "outbound", "channel": channel or journey["channel"],
               "body": body, "status": "queued", "provider_id": None, **extra}
    journey["messages"].append(message)
    return message


class JourneyService:
    def __init__(self, store: Store, agent=None, transport=None, clock=time.time, settings=None):
        self.store = store
        self.agent = agent or LocalAgent()
        self.transport = transport or Simulator()
        self.clock = clock
        self.settings = settings or Settings()
        declared = getattr(self.transport, "channels", None)
        self.channels = frozenset(CHANNELS if declared is None else declared)
        # Only transports that declare their channels get automatic SMS failover.
        self.failover = declared is not None

    def load(self, db, journey_id):
        j = self.store.get(db, journey_id)
        if not j or j.get("deleted_at"):
            raise HTTPException(404, "Journey not found")
        return j

    def save(self, db, j):
        j["revision"] = j.get("revision", 0) + 1
        self.store.put(db, j)

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
            channel = event.preferred_channel
            if channel == "RCS" and not event.rcs_available:
                channel = "SMS"
            j = {"id": identifier("txn"), "event_id": event.event_id, "event_fingerprint": digest,
                 "created_at": now(), "created_ts": self.clock(), "customer_id": event.customer_id, "event": payload,
                 "transaction_status": "pending", "journey_status": "awaiting_customer" if risk["level"] == "HIGH" else "not_flagged",
                 "risk": risk, "channel": channel, "case": None, "proposal": None,
                 "messages": [], "audit": [], "commands": {}, "agent_mode": self.agent.name,
                 "call_context": call_context(event.active_call), "deleted_at": None,
                 "escalation_level": 0, "last_outbound_at": None, "last_inbound_at": None,
                 "first_reply_seconds": None, "protected_seconds": None}
            audit(j, "risk_assessed", {"score": risk["score"], "signals": risk["signals"], "call_state": j["call_context"]["state"], "call_signal_source": "simulator"})
            if event.preferred_channel == "RCS" and channel == "SMS":
                audit(j, "channel_fallback", {"from": "RCS", "to": "SMS", "reason": "Customer RCS unavailable"})
            if channel not in self.channels:
                available = [c for c in CONVERSATION_CHANNELS if c in self.channels]
                if available:
                    audit(j, "channel_fallback", {"from": channel, "to": available[0], "reason": "Channel not configured"})
                    j["channel"] = available[0]
            if risk["level"] == "HIGH":
                amount = money(event.amount_cents)
                question = ("Call context suggests you may be on a phone call. Is someone on the call instructing you to transfer this money?"
                            if event.active_call is True else "Did you authorise it, or did someone instruct you to send it?")
                extra = {}
                if j["channel"] in {"WHATSAPP", "RCS"}:
                    # WhatsApp reply buttons / RCS suggested replies.
                    extra["buttons"] = OUTREACH_BUTTONS
                    if j["channel"] == "WHATSAPP" and self.settings.wa_template:
                        # Business-initiated WhatsApp needs an approved template; replies then open the session window.
                        extra["template"] = {"name": self.settings.wa_template, "params": [amount, event.payee]}
                outbound(j, f"KENA: We noticed a {amount} transfer to {event.payee}. {question} Reply in your own words. We will never ask for passwords or OTPs.", **extra)
                if self.settings.alert_email and "SES" in self.channels:
                    # A second, formal channel: the alert reaches the customer even if the chat message is missed.
                    app_name = {"WHATSAPP": "WhatsApp", "RCS": "Messages (RCS)"}.get(j["channel"], "SMS")
                    outbound(j, f"Dear {self.settings.customer_name},\n\nKENA flagged a {amount} transfer to {event.payee}. "
                                f"It is still pending and has not been sent.\n\nWhy it was flagged: {'; '.join(risk['signals'])}.\n\n"
                                f"We have also messaged you on {app_name}. If you did not make this transfer, or someone is telling you to make it, "
                                "reply there or contact the bank using the number on the back of your card. Do not share passwords or OTPs with anyone; "
                                "the bank will never ask for them.\n\nKENA Bank security team", "SES",
                             subject=f"KENA security alert: {amount} transfer pending your confirmation")
            self.save(db, j)
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
                self.save(db, j)
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

    def reply(self, journey_id, customer_id, reply, channel=None):
        digest = fingerprint(reply.model_dump(exclude_none=True))
        # Phase 1: record the customer's message atomically and take a snapshot for the agent.
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
            channel = channel or j["channel"]
            j["messages"].append({"id": reply.message_id, "at": now(), "direction": "inbound",
                "channel": channel, "body": reply.text, "status": "received", "provider_id": None})
            if j.get("first_reply_seconds") is None and j.get("created_ts") is not None:
                j["first_reply_seconds"] = round(self.clock() - j["created_ts"], 1)
            j["last_inbound_at"] = self.clock()
            if channel != j["channel"] and channel in CONVERSATION_CHANNELS and channel in self.channels:
                # Continue where the customer chose to engage; the journey context carries over.
                audit(j, "channel_switched", {"from": j["channel"], "to": channel, "reason": "Customer replied on this channel"})
                j["channel"] = channel
            self.save(db, j)
            snapshot = self.public(j)
        # Phase 2: the agent plans outside the database lock (model calls can take seconds).
        actions, failure = self.plan(snapshot, "customer_reply", BUTTON_TEXT.get(reply.button_id or "", reply.text))
        # Phase 3: every action is re-validated against the current state and committed atomically.
        with self.store.write() as db:
            j = self.load(db, journey_id)
            self.apply(j, actions, failure, "customer_reply")
            self.save(db, j)
        self.dispatch(journey_id)
        return self.read(journey_id, True)

    def plan(self, journey, trigger, text):
        context = {"trigger": trigger, "text": text, "journey": journey, "channels": sorted(self.channels),
                   "customer_name": self.settings.customer_name}
        try:
            actions = self.agent.respond(context)
            if not isinstance(actions, list):
                raise TypeError("Agent must return a list of actions")
            return actions, None
        except AgentRefusal:
            return [], "refusal"
        except Exception:
            return [], "error"

    def apply(self, j, actions, failure, trigger):
        if failure:
            audit(j, "agent_unavailable", {"fallback": "Clarify with no tool action", "reason": failure})
            actions = [{"type": "send_message", "text": CLARIFY}] if trigger == "customer_reply" else []
        accepted, answered = [], False
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("type") == "knowledge_lookup":
                audit(j, "playbook_consulted", {"query": str(action.get("query", ""))[:200],
                                                "matches": [str(m)[:60] for m in action.get("matches", [])][:3]})
                continue
            reason = evaluate(j, action, self.channels)
            if reason:
                audit(j, "agent_action_rejected", {"action": str(action.get("type"))[:40], "reason": reason})
                continue
            kind = action["type"]
            accepted.append(kind)
            if kind == "send_message":
                channel = channel_of(action, j)
                extra = {"subject": "KENA: action needed on a pending transfer"} if channel == "SES" else {}
                outbound(j, action["text"].strip(), channel, **extra)
                answered = answered or channel != "SES"
            elif kind == "propose_protection":
                self.propose(j, action.get("reason", ""))
                answered = True
            elif kind == "record_verified":
                j["proposal"] = None
                j["journey_status"] = NEXT_STATUS[kind]
                audit(j, "customer_verified", {"funds_executed": False})
            elif kind == "escalate":
                j["proposal"] = None
                j["journey_status"] = NEXT_STATUS[kind]
                audit(j, "human_escalation", {"reason": str(action.get("reason", ""))[:200], "priority": action.get("priority", "normal")})
        audit(j, "agent_decision", {"agent": self.agent.name, "trigger": trigger, "actions": accepted})
        if trigger == "customer_reply" and not answered:
            outbound(j, CLARIFY)
        # The consent request goes last, so its buttons are on the newest message the customer sees.
        consent = next((m for m in reversed(j["messages"]) if any(b["id"].startswith("confirm:") for b in m.get("buttons") or [])), None)
        if consent and consent["status"] == "queued" and j["messages"][-1] is not consent:
            j["messages"].remove(consent)
            j["messages"].append(consent)

    def propose(self, j, reason=""):
        j["journey_status"] = "awaiting_confirmation"
        token = secrets.token_urlsafe(32)
        j["proposal"] = {"token": token, "ref": proposal_ref(token), "actions": POLICY["tools"].copy(),
            "expires_at": self.clock() + POLICY["confirmation_ttl_seconds"], "customer_id": j["customer_id"],
            "transaction_id": j["id"], "policy_version": POLICY["version"]}
        audit(j, "protection_proposed", {"actions": POLICY["tools"], "expires_in_seconds": POLICY["confirmation_ttl_seconds"], "reason": str(reason)[:200]})
        # Consent wording is fixed server text, never model-generated.
        outbound(j, "I can pause this pending transfer and open a scam-review case. Nothing has been changed yet. "
                    "Tap 'Pause & open case' to approve both actions, or reply cancel. If someone claimed to be your bank or the police, "
                    "stop communicating with them and contact your bank through its official app.",
                 buttons=[{"id": "confirm:" + proposal_ref(token), "title": "Pause & open case"}, {"id": "reply:cancel", "title": "Cancel"}])

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

    def confirm(self, journey_id, customer_id, confirmation, channel=None):
        digest = fingerprint(confirmation.model_dump(exclude_none=True))
        channel = channel or confirmation.channel
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
            if channel:
                j["messages"].append({"id": confirmation.command_id, "at": now(), "direction": "inbound", "channel": channel,
                                      "body": "Tapped: Pause & open case", "status": "received", "provider_id": None})
            audit(j, "customer_confirmed", {"actions": confirmation.approved_actions, "customer_id": customer_id, "channel": channel or "app"})
            j["transaction_status"] = "paused"
            audit(j, "tool_executed", {"tool": "pause_transfer", "result": "paused", "mock": True})
            j["case"] = {"id": identifier("SCAM"), "status": "open", "created_at": now(), "reason": "Customer disputed suspicious transfer", "mock": True}
            audit(j, "tool_executed", {"tool": "create_scam_case", "case_id": j["case"]["id"], "mock": True})
            j["journey_status"] = "protected"
            j["proposal"] = None
            if j.get("created_ts") is not None:
                j["protected_seconds"] = round(self.clock() - j["created_ts"], 1)
            body = f"Your {money(j['event']['amount_cents'])} transfer is paused. Scam-review case {j['case']['id']} is open. The fraud team will review it; this does not mean funds have been recovered. No passwords or OTPs are required."
            outbound(j, body)
            outbound(j, body + "\n\n" + self.summary(j), "SES", subject="KENA: protection confirmation")
            self.save(db, j)
        self.dispatch(journey_id)
        return self.read(journey_id, True)

    def summary(self, j):
        """Formal record for the email channel: the whole cross-channel conversation in one place."""
        lines = [f"Case summary for {self.settings.customer_name}",
                 f"Transfer: {money(j['event']['amount_cents'])} to {j['event']['payee']} ({j['transaction_status']})",
                 f"Risk score: {j['risk']['score']}/100. Signals: {'; '.join(j['risk']['signals'])}",
                 f"Case: {j['case']['id'] if j['case'] else 'none'}", "", "Conversation:"]
        for m in j["messages"]:
            if m["channel"] != "SES":
                who = "You" if m["direction"] == "inbound" else "KENA"
                lines.append(f"- {m['at'][11:19]} UTC, {who} via {m['channel']}: {m['body']}")
        lines += ["", "This email is a record of the actions taken. KENA will never ask for your password or OTP."]
        return "\n".join(lines)

    def dispatch(self, journey_id):
        # Sends happen after the state commit; failures are recorded, never blindly retried.
        with self.store.write() as db:
            j = self.load(db, journey_id)
            index = 0
            while index < len(j["messages"]):
                msg = j["messages"][index]
                index += 1
                if msg["direction"] != "outbound" or msg["status"] != "queued":
                    continue
                try:
                    msg.update(self.transport.send(msg))
                except Exception as error:
                    # The reason goes to the server log only; provider errors can carry internal details.
                    log.warning("Send failed on %s for %s: %s: %s", msg["channel"], journey_id, type(error).__name__, error)
                    msg["status"] = "failed"
                    audit(j, "message_failed", {"message_id": msg["id"], "channel": msg["channel"], "reason": "Transport unavailable; no automatic retry"})
                    self.fail_over(j, msg)
                else:
                    j["last_outbound_at"] = self.clock()
                    audit(j, "message_dispatched", {"message_id": msg["id"], "channel": msg["channel"], "status": msg["status"]})
            self.save(db, j)

    def fail_over(self, j, msg):
        """A failed WhatsApp/RCS message is re-sent once as SMS, if SMS is configured."""
        if self.failover and msg["channel"] in {"WHATSAPP", "RCS"} and "SMS" in self.channels and not msg.get("failover_of"):
            outbound(j, msg["body"], "SMS", failover_of=msg["id"])
            audit(j, "channel_failover", {"from": msg["channel"], "to": "SMS", "message_id": msg["id"]})
            return True
        return False

    def record_status(self, provider_id, status, error=None):
        """Delivery receipts from the channel provider (sent, delivered, read, failed)."""
        if not provider_id or status not in STATUS_ORDER and status != "failed":
            return {"ignored": "unknown_status"}
        for candidate in self.store.all():
            if candidate.get("deleted_at") or not any(m.get("provider_id") == provider_id for m in candidate["messages"]):
                continue
            with self.store.write() as db:
                j = self.load(db, candidate["id"])
                msg = next(m for m in j["messages"] if m.get("provider_id") == provider_id)
                if status == "failed" and msg["status"] != "failed":
                    msg["status"] = "failed"
                    audit(j, "message_failed", {"message_id": msg["id"], "channel": msg["channel"], "reason": str(error or "Provider reported failure")[:200]})
                    self.fail_over(j, msg)
                elif msg["status"] != "failed" and STATUS_ORDER.get(status, 0) > STATUS_ORDER.get(msg["status"], 0):
                    msg["status"] = status
                    audit(j, "message_receipt", {"message_id": msg["id"], "channel": msg["channel"], "status": status})
                self.save(db, j)
            self.dispatch(candidate["id"])
            return {"journey": candidate["id"], "status": status}
        return {"ignored": "unknown_message"}

    def customer_for(self, sender):
        expected = digits(self.settings.customer_phone)
        return "demo-customer" if expected and digits(sender) == expected else None

    def active_journey(self, customer_id):
        journeys = [j for j in self.store.all() if not j.get("deleted_at") and j["customer_id"] == customer_id and j["journey_status"] != "not_flagged"]
        open_journeys = [j for j in journeys if j["journey_status"] in OPEN_STATES]
        return (open_journeys or journeys or [None])[0]

    def handle_inbound(self, event):
        """Route a provider event (WhatsApp or two-way SMS) into the customer's current journey."""
        if event["kind"] == "status":
            return self.record_status(event.get("provider_id"), event.get("status"), event.get("error"))
        customer_id = self.customer_for(event.get("sender"))
        if not customer_id:
            return {"ignored": "unknown_sender"}
        j = self.active_journey(customer_id)
        if not j:
            return {"ignored": "no_journey"}
        message_id = "in-" + hashlib.sha256(str(event.get("message_id") or uuid.uuid4()).encode()).hexdigest()[:32]
        text = (event.get("text") or "").strip()
        button = event.get("button_id") or TITLES.get(text.lower(), "")
        try:
            if button.startswith("confirm:"):
                return self.confirm_button(j, button.split(":", 1)[1], message_id, event["channel"])
            if button in BUTTON_TEXT:
                reply = Reply(message_id=message_id, text=(text or BUTTON_TEXT[button])[:1000], button_id=button)
            elif text:
                reply = Reply(message_id=message_id, text=text[:1000])
            else:
                return {"ignored": "unsupported_message"}
            return self.reply(j["id"], customer_id, reply, channel=event["channel"])
        except HTTPException as exc:
            return {"ignored": exc.detail}

    def confirm_button(self, j, ref, message_id, channel):
        proposal = j.get("proposal")
        if proposal and secrets.compare_digest(ref, proposal_ref(proposal["token"])):
            try:
                return self.confirm(j["id"], j["customer_id"], Confirmation(command_id=message_id, token=proposal["token"],
                                    approved_actions=list(POLICY["tools"])), channel=channel)
            except HTTPException:
                pass  # Expired, replaced or blocked by policy: tell the customer instead of failing silently.
        with self.store.write() as db:
            current = self.load(db, j["id"])
            if "confirm:" + message_id in current["commands"]:
                return {"ignored": "duplicate"}
            current["commands"]["confirm:" + message_id] = "rejected"
            outbound(current, "That confirmation could not be applied (it may have expired or been replaced), so no banking action was taken. "
                              "Reply 'not me' to request a new one.", channel)
            self.save(db, current)
        self.dispatch(j["id"])
        return {"ignored": "stale_confirmation"}

    def tick(self):
        """No-response orchestration: let the agent decide on a follow-up channel or human escalation."""
        handled = 0
        for j in self.store.all():
            if j.get("deleted_at") or j["journey_status"] not in OPEN_STATES or j.get("escalation_level", 0) >= MAX_FOLLOW_UPS:
                continue
            last = max(j.get("last_outbound_at") or 0, j.get("last_inbound_at") or 0)
            if not last or self.clock() - last < self.settings.no_response_seconds:
                continue
            if j.get("last_inbound_at") and j["last_inbound_at"] >= (j.get("last_outbound_at") or 0):
                continue  # The customer spoke last; nothing to chase.
            actions, failure = self.plan(self.public(j), "no_response", None)
            with self.store.write() as db:
                current = self.load(db, j["id"])
                if current.get("revision") != j.get("revision"):
                    continue  # The customer acted while the agent was planning.
                self.apply(current, actions, failure, "no_response")
                current["escalation_level"] = current.get("escalation_level", 0) + 1
                current["last_outbound_at"] = self.clock()
                self.save(db, current)
            self.dispatch(j["id"])
            handled += 1
        return handled

    def snapshot(self):
        journeys = [self.public(j) for j in self.store.all() if not j.get("deleted_at")]
        flagged = [j for j in journeys if j["risk"]["level"] == "HIGH"]
        replied = [j["first_reply_seconds"] for j in flagged if j.get("first_reply_seconds") is not None]
        protected = [j["protected_seconds"] for j in flagged if j.get("protected_seconds") is not None]
        median = lambda values: round(statistics.median(values), 1) if values else None
        return {"mode": "live-aws-cds" if self.settings.live else "synthetic-local-demo", "agent": self.agent.name,
                "transport": self.transport.name, "channels": sorted(self.channels),
                "channel_modes": self.settings.channel_modes(self.channels), "policy": POLICY, "journeys": journeys,
                "metrics": {"flagged": len(flagged),
                            "protected": sum(j["transaction_status"] == "paused" for j in journeys),
                            "cases": sum(j["case"] is not None for j in journeys),
                            "paused_value_cents": sum(j["event"]["amount_cents"] for j in journeys if j["transaction_status"] == "paused")},
                "outcomes": {"response_rate": round(len(replied) / len(flagged), 3) if flagged else None,
                             "median_first_reply_seconds": median(replied),
                             "median_seconds_to_protect": median(protected),
                             "escalated_to_human": sum(j["journey_status"] == "human_review" for j in journeys)}}
