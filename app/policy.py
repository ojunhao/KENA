"""Server-side authority. Agent actions are proposals; only this module decides whether they may happen."""
import re

POLICY = {
    "version": "KENA-DEMO-2",
    "tools": ["pause_transfer", "create_scam_case"],
    "max_protection_cents": 10000000,
    "confirmation_ttl_seconds": 300,
    "rule": "Only a disputed, pending, flagged SGD transfer with authenticated customer confirmation may be paused. Never execute, release, refund, or reverse funds.",
}
ACTION_TYPES = {"send_message", "propose_protection", "record_verified", "escalate"}
OPEN_STATES = {"awaiting_customer", "awaiting_confirmation"}
CHANNEL_ALIASES = {"EMAIL": "SES"}
MAX_TEXT = 900

_SENSITIVE = re.compile(r"\b(otp|one[- ]time (pass(word|code)|pin)|passwords?|passcodes?|pins?|cvv|card numbers?|log ?in details|credentials?)\b", re.I)
_OUTCOME = re.compile(r"\b(refund(ed|s)?|recover(ed|y)?|revers(e|ed|al)|release(d)?)\b", re.I)
_NEGATION = re.compile(r"\b(never|not|cannot|can't|won't|don't|do not|does not|doesn't|no one|nobody)\b", re.I)
_LINK = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|sg|net|org|io|ly|me|co|info|link|xyz)\b", re.I)
_LONG_NUMBER = re.compile(r"\d[\d -]{6,}\d")
_REFERENCE = re.compile(r"\b[A-Z]+-[0-9a-f]{12}\b")


def channel_of(action, journey):
    channel = (action.get("channel") or journey["channel"]).upper()
    return CHANNEL_ALIASES.get(channel, channel)


def screen_text(text):
    """Reject agent-written text that could itself look like a scam: links, credential requests, outcome promises."""
    if not isinstance(text, str) or not text.strip():
        return "empty_text"
    if len(text) > MAX_TEXT:
        return "text_too_long"
    if _LINK.search(text):
        return "links_not_allowed"
    if _LONG_NUMBER.search(_REFERENCE.sub("", text)):
        return "phone_or_account_numbers_not_allowed"
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if _SENSITIVE.search(sentence) and not _NEGATION.search(sentence):
            return "credential_request_not_allowed"
        if _OUTCOME.search(sentence) and not _NEGATION.search(sentence):
            return "fund_outcome_claim_not_allowed"
    return None


def evaluate(journey, action, channels):
    """Return None if the action is permitted on the journey's current state, else a reason code."""
    kind = action.get("type")
    if kind not in ACTION_TYPES:
        return "unknown_action"
    if journey["journey_status"] == "not_flagged" or journey.get("deleted_at"):
        return "journey_not_active"
    if kind == "send_message":
        if channel_of(action, journey) not in channels:
            return "channel_unavailable"
        return screen_text(action.get("text"))
    if kind == "escalate":
        return None if journey["transaction_status"] == "pending" else "transfer_not_pending"
    if journey["transaction_status"] != "pending":
        return "transfer_not_pending"
    if kind == "record_verified":
        return None if journey["journey_status"] in OPEN_STATES | {"verified_review"} else "journey_not_open"
    # propose_protection: the agent may ask; consent and execution remain separate, deterministic steps.
    if journey["risk"]["level"] != "HIGH" or journey["event"]["currency"] != "SGD":
        return "not_eligible"
    if journey["event"]["amount_cents"] > POLICY["max_protection_cents"]:
        return "amount_exceeds_protection_cap"
    return None if journey["journey_status"] in OPEN_STATES | {"verified_review", "human_review"} else "journey_not_open"
