"""Inbound channel events delivered by Amazon SNS: signature verification and WhatsApp / two-way SMS parsing."""
import base64
import json
import re
import urllib.parse
import urllib.request
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
}


class InvalidSignature(ValueError):
    pass


def trusted_sns_url(url):
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme == "https" and bool(SNS_HOST.match(parsed.hostname or ""))


def string_to_sign(envelope):
    keys = FIELDS.get(envelope.get("Type"))
    if not keys:
        raise InvalidSignature("Unsupported SNS message type")
    # Subject is signed only when present.
    return "".join(f"{key}\n{envelope[key]}\n" for key in keys if key in envelope and (key != "Subject" or envelope[key] is not None))


@lru_cache(maxsize=8)
def _certificate(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return x509.load_pem_x509_certificate(response.read())


def verify(envelope, certificate_loader=_certificate):
    """Check an SNS envelope's signature against Amazon's signing certificate (SignatureVersion 1 or 2)."""
    url = envelope.get("SigningCertURL") or envelope.get("SigningCertUrl")
    if not trusted_sns_url(url) or not urllib.parse.urlparse(url).path.endswith(".pem"):
        raise InvalidSignature("Untrusted signing certificate URL")
    algorithm = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(envelope.get("SignatureVersion")))
    if algorithm is None:
        raise InvalidSignature("Unsupported signature version")
    try:
        certificate_loader(url).public_key().verify(base64.b64decode(envelope["Signature"]), string_to_sign(envelope).encode(),
                                                    padding.PKCS1v15(), algorithm)
    except InvalidSignature:
        raise
    except Exception as exc:
        raise InvalidSignature("Signature verification failed") from exc


def confirm_subscription(envelope, opener=urllib.request.urlopen):
    url = envelope.get("SubscribeURL")
    if not trusted_sns_url(url):
        raise InvalidSignature("Untrusted subscription URL")
    with opener(url, timeout=5) as response:
        return response.status


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_notification(message):
    """Turn one SNS Notification 'Message' into KENA events.

    End User Messaging Social publishes the Meta webhook entry in `whatsAppWebhookEntry`;
    End User Messaging SMS publishes two-way replies with `originationNumber` / `messageBody`.
    """
    message = _maybe_json(message)
    if not isinstance(message, dict):
        return []
    if "whatsAppWebhookEntry" in message:
        return parse_whatsapp(_maybe_json(message["whatsAppWebhookEntry"]))
    if "originationNumber" in message and "messageBody" in message:
        return [{"kind": "message", "channel": "SMS", "sender": message["originationNumber"],
                 "message_id": message.get("inboundMessageId") or message.get("messageId"), "text": message["messageBody"]}]
    return []


def parse_whatsapp(entry):
    events = []
    if not isinstance(entry, dict):
        return events
    for change in entry.get("changes", []):
        value = change.get("value", {}) if isinstance(change, dict) else {}
        for msg in value.get("messages", []):
            kind = msg.get("type")
            event = {"kind": "message", "channel": "WHATSAPP", "sender": msg.get("from"), "message_id": msg.get("id"), "text": ""}
            if kind == "text":
                event["text"] = msg.get("text", {}).get("body", "")
            elif kind == "interactive":
                reply = msg.get("interactive", {}).get("button_reply") or msg.get("interactive", {}).get("list_reply") or {}
                event["button_id"], event["text"] = reply.get("id", ""), reply.get("title", "")
            elif kind == "button":
                # Template quick-reply buttons return their text (and payload).
                event["text"] = msg.get("button", {}).get("text") or msg.get("button", {}).get("payload", "")
            events.append(event)
        for status in value.get("statuses", []):
            errors = status.get("errors") or []
            events.append({"kind": "status", "channel": "WHATSAPP", "provider_id": status.get("id"), "status": status.get("status"),
                           "error": (errors[0].get("title") or errors[0].get("message")) if errors else None})
    return events
