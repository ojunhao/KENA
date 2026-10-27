"""Transport adapters. Simulator is the default; AwsCommunications requires explicit live mode."""
import json
from .config import digits

CHANNELS = {"WHATSAPP", "RCS", "SMS", "SES"}
DEFAULT_SUBJECT = "KENA: protection confirmation"


class Simulator:
    name = "simulator"
    channels = frozenset(CHANNELS)

    def send(self, message):
        return {"status": "simulated", "provider_id": "sim-" + message["id"]}


def whatsapp_payload(message, to, template_language="en"):
    """Meta Cloud API message object, passed through AWS End User Messaging Social unchanged."""
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to)}
    template = message.get("template")
    buttons = message.get("buttons") or []
    if template:
        payload["type"] = "template"
        payload["template"] = {"name": template["name"], "language": {"code": template_language},
                               "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in template["params"]]}]}
    elif buttons:
        # Reply buttons: at most three, titles at most 20 characters, ids returned on tap.
        payload["type"] = "interactive"
        payload["interactive"] = {"type": "button", "body": {"text": message["body"][:1024]},
                                  "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in buttons[:3]]}}
    else:
        payload["type"] = "text"
        payload["text"] = {"preview_url": False, "body": message["body"][:4096]}
    return payload


class AwsCommunications:
    """AWS CDS: End User Messaging Social (WhatsApp), End User Messaging SMS/RCS and SES. No credentials in config."""
    name = "aws-cds"

    def __init__(self, sms_client, ses_client, social_client=None, *, sms_identity, rcs_identity, phone, email, sender,
                 wa_phone_number_id=None, meta_api_version="v20.0", template_language="en", approved=False):
        if not approved:
            raise ValueError("Live AWS messaging requires explicit approval")
        self.sms, self.ses, self.social = sms_client, ses_client, social_client
        self.sms_identity, self.rcs_identity = sms_identity, rcs_identity
        self.phone, self.email, self.sender = phone, email, sender
        self.wa_phone_number_id, self.meta_api_version = wa_phone_number_id, meta_api_version
        self.template_language = template_language

    @property
    def channels(self):
        available = set()
        if self.social and self.wa_phone_number_id and self.phone:
            available.add("WHATSAPP")
        if self.sms_identity and self.phone:
            available.add("SMS")
        if self.rcs_identity and self.phone:
            available.add("RCS")
        if self.sender and self.email:
            available.add("SES")
        return frozenset(available)

    def send(self, message):
        channel = message["channel"]
        if channel == "SES":
            result = self.ses.send_email(
                FromEmailAddress=self.sender,
                Destination={"ToAddresses": [self.email]},
                Content={"Simple": {"Subject": {"Data": message.get("subject") or DEFAULT_SUBJECT},
                                    "Body": {"Text": {"Data": message["body"]}}}},
            )
        elif channel == "WHATSAPP":
            if "WHATSAPP" not in self.channels:
                raise ValueError("WhatsApp is not configured")
            result = self.social.send_whatsapp_message(
                originationPhoneNumberId=self.wa_phone_number_id,
                message=json.dumps(whatsapp_payload(message, self.phone, self.template_language)).encode(),
                metaApiVersion=self.meta_api_version,
            )
            result = {"MessageId": result["messageId"]}
        else:
            identity = self.rcs_identity if channel == "RCS" else self.sms_identity
            if not identity:
                raise ValueError("Requested channel has no configured origination identity")
            result = self.sms.send_text_message(
                DestinationPhoneNumber=self.phone, OriginationIdentity=identity,
                MessageBody=message["body"], MessageType="TRANSACTIONAL",
            )
        # Provider acceptance is not proof of handset/inbox delivery; receipts arrive separately.
        return {"status": "accepted", "provider_id": result["MessageId"]}


class HybridTransport:
    """Sends the selected channels through AWS and the rest to the device emulator (simulator)."""
    name = "hybrid"

    def __init__(self, live, live_channels, emulator=None):
        self.live, self.emulator = live, emulator or Simulator()
        self.live_channels = frozenset(live_channels) & live.channels

    @property
    def channels(self):
        return frozenset(CHANNELS)

    def send(self, message):
        return (self.live if message["channel"] in self.live_channels else self.emulator).send(message)


def build_transport(settings):
    if not settings.live:
        return Simulator()
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
    make = lambda service: boto3.client(service, region_name=settings.region, config=config)
    live = AwsCommunications(
        make("pinpoint-sms-voice-v2"), make("sesv2"), make("socialmessaging"),
        sms_identity=settings.sms_identity, rcs_identity=settings.rcs_identity,
        phone=settings.customer_phone, email=settings.customer_email, sender=settings.ses_sender,
        wa_phone_number_id=settings.wa_phone_number_id, meta_api_version=settings.meta_api_version,
        template_language=settings.wa_template_language, approved=True)
    return HybridTransport(live, settings.live_channels) if settings.live_channels else live
