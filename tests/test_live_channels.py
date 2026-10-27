import base64
import datetime
import json
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from app import inbound
from app.config import Settings
from app.main import create_app
from app.policy import screen_text

TOPIC = "arn:aws:sns:ap-southeast-1:123456789012:kena-events"
PHONE = "+65 9000 0000"
SETTINGS = Settings(customer_phone=PHONE, customer_email="alex@example.invalid", sns_topic_arn=TOPIC, no_response_seconds=60)


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now


class Recorder:
    """Transport that records sends and can be told which channels fail."""
    name = "recorder"
    def __init__(self, channels=("WHATSAPP", "SMS", "SES"), failing=()):
        self.channels, self.failing, self.sent = frozenset(channels), set(failing), []
    def send(self, message):
        if message["channel"] in self.failing:
            raise RuntimeError("provider down")
        self.sent.append(message)
        return {"status": "accepted", "provider_id": "wamid." + message["id"]}


@pytest.fixture
def setup(tmp_path):
    clock, transport = Clock(), Recorder()
    app = create_app(str(tmp_path / "live.db"), transport=transport, clock=clock, settings=SETTINGS,
                     sns_verifier=lambda envelope: None, sns_confirmer=lambda envelope: 200)
    return TestClient(app), app.state.service, transport, clock


def create(client, **kwargs):
    response = client.post("/api/events", json={"event_id": "event-1", "preferred_channel": "WHATSAPP", **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def whatsapp(message=None, status=None):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"}}
    if message:
        value["messages"] = [message]
    if status:
        value["statuses"] = [status]
    entry = {"id": "waba", "changes": [{"field": "messages", "value": value}]}
    return {"Type": "Notification", "MessageId": "sns-1", "TopicArn": TOPIC, "Timestamp": "2026-10-19T00:00:00Z",
            "Message": json.dumps({"context": {}, "whatsAppWebhookEntry": json.dumps(entry)})}


def text(body, wamid="wamid.in-1", sender="6590000000"):
    return {"from": sender, "id": wamid, "timestamp": "1", "type": "text", "text": {"body": body}}


def button(button_id, title, wamid="wamid.btn-1"):
    return {"from": "6590000000", "id": wamid, "timestamp": "1", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}}}


def post(client, envelope):
    return client.post("/webhooks/sns", content=json.dumps(envelope), headers={"content-type": "text/plain"})


def latest(client, j):
    return client.get(f"/api/journeys/{j['id']}").json()


def test_whatsapp_outreach_has_reply_buttons(setup):
    client, _, transport, _ = setup
    j = create(client)
    assert j["channel"] == "WHATSAPP"
    assert [b["id"] for b in transport.sent[0]["buttons"]] == ["reply:verify", "reply:dispute", "reply:coached"]


def test_whatsapp_dispute_and_button_consent_protects(setup):
    client, _, transport, _ = setup
    j = create(client)
    assert post(client, whatsapp(button("reply:dispute", "No, not me"))).json() == {"accepted": 1}
    j = latest(client, j)
    assert j["journey_status"] == "awaiting_confirmation"
    confirm_button = j["messages"][-1]["buttons"][0]
    assert confirm_button["id"].startswith("confirm:") and "token" not in j["proposal"]
    post(client, whatsapp(button(confirm_button["id"], "Pause & open case", "wamid.btn-2")))
    j = latest(client, j)
    assert j["transaction_status"] == "paused" and j["case"]
    assert [m["channel"] for m in j["messages"][-2:]] == ["WHATSAPP", "SES"]
    assert "Conversation:" in j["messages"][-1]["body"]
    assert j["protected_seconds"] == 0
    # A duplicate SNS delivery of the same tap is idempotent.
    post(client, whatsapp(button(confirm_button["id"], "Pause & open case", "wamid.btn-2")))
    assert len([a for a in latest(client, j)["audit"] if a["kind"] == "tool_executed"]) == 2


def test_typed_yes_or_stale_button_never_pauses(setup):
    client, _, _, _ = setup
    j = create(client)
    post(client, whatsapp(text("not me")))
    stale = latest(client, j)["messages"][-1]["buttons"][0]["id"]
    post(client, whatsapp(text("yes", "wamid.in-2")))
    post(client, whatsapp(text("not me", "wamid.in-3")))
    post(client, whatsapp(button(stale, "Pause & open case", "wamid.btn-3")))
    j = latest(client, j)
    assert j["transaction_status"] == "pending" and j["case"] is None
    assert "no longer" in j["messages"][-1]["body"] or "could not be applied" in j["messages"][-1]["body"]


def test_unknown_sender_and_bad_topic_are_ignored(setup):
    client, service, _, _ = setup
    j = create(client)
    post(client, whatsapp(text("not me", sender="6511111111")))
    assert latest(client, j)["journey_status"] == "awaiting_customer"
    bad = whatsapp(text("not me"))
    bad["TopicArn"] = "arn:aws:sns:ap-southeast-1:999999999999:other"
    assert post(client, bad).status_code == 403


def test_reply_on_sms_switches_channel_and_keeps_context(setup):
    client, service, _, _ = setup
    j = create(client)
    sms = {"Type": "Notification", "MessageId": "sns-2", "TopicArn": TOPIC, "Timestamp": "t",
           "Message": json.dumps({"originationNumber": "+6590000000", "destinationNumber": "+6580000000", "messageBody": "Why was this flagged?", "inboundMessageId": "sms-1"})}
    post(client, sms)
    j = latest(client, j)
    assert j["channel"] == "SMS" and j["messages"][-1]["channel"] == "SMS"
    assert "flagged because" in j["messages"][-1]["body"]
    assert any(a["kind"] == "channel_switched" for a in j["audit"])


def test_delivery_receipts_progress_and_failure_fails_over(setup):
    client, _, transport, _ = setup
    j = create(client)
    provider = latest(client, j)["messages"][0]["provider_id"]
    post(client, whatsapp(status={"id": provider, "status": "read", "recipient_id": "6590000000"}))
    post(client, whatsapp(status={"id": provider, "status": "delivered", "recipient_id": "6590000000"}))
    assert latest(client, j)["messages"][0]["status"] == "read"
    j2 = create(client, event_id="event-2")
    provider = latest(client, j2)["messages"][0]["provider_id"]
    post(client, whatsapp(status={"id": provider, "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}))
    j2 = latest(client, j2)
    assert j2["messages"][0]["status"] == "failed"
    assert j2["messages"][-1]["channel"] == "SMS" and j2["messages"][-1]["failover_of"] == j2["messages"][0]["id"]


def test_send_failure_fails_over_to_sms_once(tmp_path):
    transport = Recorder(failing={"WHATSAPP", "SMS"})
    client = TestClient(create_app(str(tmp_path / "f.db"), transport=transport, settings=SETTINGS))
    j = create(client)
    assert [(m["channel"], m["status"]) for m in j["messages"]] == [("WHATSAPP", "failed"), ("SMS", "failed")]


def test_no_response_follow_up_then_human_escalation(setup):
    client, service, transport, clock = setup
    j = create(client)
    assert service.tick() == 0
    clock.now += 61
    assert service.tick() == 1
    assert transport.sent[-1]["channel"] == "SMS"
    clock.now += 30
    assert service.tick() == 0
    clock.now += 31
    assert service.tick() == 1
    j = latest(client, j)
    assert j["journey_status"] == "human_review" and j["escalation_level"] == 2
    assert transport.sent[-1]["channel"] == "SES" and "Alex" in transport.sent[-1]["body"]
    clock.now += 600
    assert service.tick() == 0
    assert j["transaction_status"] == "pending"


def test_no_follow_up_after_customer_replies(setup):
    client, service, _, clock = setup
    j = create(client)
    post(client, whatsapp(text("Why was this flagged?")))
    clock.now += 30
    assert service.tick() == 0
    outcomes = client.get("/api/dashboard").json()["outcomes"]
    assert outcomes["response_rate"] == 1.0


def test_sns_signature_verification_and_subscription():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    envelope = whatsapp(text("hi"))
    envelope.update({"SignatureVersion": "2", "SigningCertURL": "https://sns.ap-southeast-1.amazonaws.com/SimpleNotificationService-x.pem"})
    envelope["Signature"] = base64.b64encode(key.sign(inbound.string_to_sign(envelope).encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    inbound.verify(envelope, certificate_loader=lambda url: cert)
    with pytest.raises(inbound.InvalidSignature):
        inbound.verify({**envelope, "Message": "tampered"}, certificate_loader=lambda url: cert)
    with pytest.raises(inbound.InvalidSignature):
        inbound.verify({**envelope, "SigningCertURL": "https://evil.example/cert.pem"}, certificate_loader=lambda url: cert)
    with pytest.raises(inbound.InvalidSignature):
        inbound.confirm_subscription({"SubscribeURL": "https://evil.example/confirm"})


def test_webhook_rejects_invalid_signature_and_unconfigured_topic(tmp_path):
    def reject(envelope):
        raise inbound.InvalidSignature("bad")
    client = TestClient(create_app(str(tmp_path / "s.db"), settings=SETTINGS, sns_verifier=reject))
    assert post(client, whatsapp(text("not me"))).status_code == 403
    unconfigured = TestClient(create_app(str(tmp_path / "u.db")))
    assert post(unconfigured, whatsapp(text("not me"))).status_code == 403


def test_template_quick_reply_and_parsing():
    events = inbound.parse_whatsapp({"changes": [{"value": {"messages": [
        {"from": "65", "id": "w1", "type": "button", "button": {"text": "No, it wasn't me", "payload": "No, it wasn't me"}},
        {"from": "65", "id": "w2", "type": "image", "image": {}}]}}]})
    assert [e["text"] for e in events] == ["No, it wasn't me", ""]
    assert inbound.parse_notification("not json") == []


def test_basic_auth_protects_dashboard_not_webhook(tmp_path):
    settings = Settings(basic_auth="reviewer:secret", sns_topic_arn=TOPIC, customer_phone=PHONE)
    client = TestClient(create_app(str(tmp_path / "a.db"), settings=settings, sns_verifier=lambda e: None))
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/dashboard", auth=("reviewer", "secret")).status_code == 200
    assert client.get("/healthz").status_code == 200
    assert post(client, whatsapp(text("hello"))).status_code == 200


@pytest.mark.parametrize("message,reason", [
    ("Please share the OTP we just sent.", "credential_request_not_allowed"),
    ("Log in at www.example-bank.com to secure your funds.", "links_not_allowed"),
    ("Call us on +65 6123 4567.", "phone_or_account_numbers_not_allowed"),
    ("Your money will be refunded.", "fund_outcome_claim_not_allowed"),
    ("We will never ask for your OTP. Did you make this transfer?", None),
    ("Case SCAM-123456789012 is open; this does not mean funds were recovered.", None),
    ("Call the ScamShield Helpline 1799 if you are unsure.", None),
])
def test_agent_text_screening(message, reason):
    assert screen_text(message) == reason


def test_env_file_loading(tmp_path, monkeypatch):
    from app.config import load_env_file
    (tmp_path / ".env").write_text("# comment\nKENA_X_ONE=1      # trailing comment\nKENA_X_TWO='kena:pa#ss'\nKENA_X_SHELL=file\nBROKEN LINE\n")
    monkeypatch.setenv("KENA_X_SHELL", "shell")
    for key in ("KENA_X_ONE", "KENA_X_TWO"):
        monkeypatch.delenv(key, raising=False)
    load_env_file(tmp_path / ".env")
    import os
    assert (os.environ["KENA_X_ONE"], os.environ["KENA_X_TWO"], os.environ["KENA_X_SHELL"]) == ("1", "kena:pa#ss", "shell")
    for key in ("KENA_X_ONE", "KENA_X_TWO"):
        monkeypatch.delenv(key)


def test_hybrid_transport_sends_only_live_channels(tmp_path):
    from app.messaging import HybridTransport
    live = Recorder(channels=("SES",))
    transport = HybridTransport(live, {"SES", "WHATSAPP"})
    assert transport.live_channels == {"SES"}
    settings = Settings(live=True, live_channels=frozenset({"SES"}), customer_phone=PHONE)
    client = TestClient(create_app(str(tmp_path / "h.db"), transport=transport, settings=settings))
    j = create(client, preferred_channel="RCS")
    assert j["channel"] == "RCS" and j["messages"][0]["status"] == "simulated" and j["messages"][0]["buttons"]
    j = client.post(f"/api/customer/{j['id']}/replies", headers={"X-Demo-Customer": "demo-customer"},
                    json={"message_id": "r1", "text": "No, not me", "button_id": "reply:dispute"}).json()
    assert j["messages"][1]["body"] == "No, not me" and j["proposal"]["ref"]
    j = client.post(f"/api/customer/{j['id']}/confirm", headers={"X-Demo-Customer": "demo-customer"},
                    json={"command_id": "c1", "token": j["proposal"]["token"], "approved_actions": ["pause_transfer", "create_scam_case"], "channel": "RCS"}).json()
    assert j["transaction_status"] == "paused" and any(m["body"] == "Tapped: Pause & open case" for m in j["messages"])
    assert [m["channel"] for m in live.sent] == ["SES"]
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["channel_modes"] == {"RCS": "emulated", "SES": "live", "SMS": "emulated", "WHATSAPP": "emulated"}
    assert "phone" in client.get("/device").text


def test_live_channels_setting(monkeypatch):
    monkeypatch.setenv("KENA_LIVE", "1")
    monkeypatch.setenv("KENA_LIVE_CHANNELS", "ses, whatsapp")
    settings = Settings.from_env()
    assert settings.live_channels == {"SES", "WHATSAPP"}
    assert Settings().channel_modes({"SES"}) == {"SES": "emulated"}
    assert Settings(live=True).channel_modes({"SES", "SMS"}) == {"SES": "live", "SMS": "live"}


def test_playbook_lookup_is_audited_not_executed(tmp_path):
    class Planner:
        name = "planner"
        def respond(self, context):
            return [{"type": "knowledge_lookup", "query": "police safe account", "matches": ["government-impersonation"]},
                    {"type": "send_message", "text": "Police never ask you to move money. Did someone call you?"}]
    client = TestClient(create_app(str(tmp_path / "p.db"), agent=Planner(), settings=SETTINGS))
    j = create(client)
    j = client.post(f"/api/customer/{j['id']}/replies", headers={"X-Demo-Customer": "demo-customer"},
                    json={"message_id": "r1", "text": "the police told me to move it"}).json()
    consulted = [a for a in j["audit"] if a["kind"] == "playbook_consulted"]
    assert consulted[0]["detail"]["matches"] == ["government-impersonation"]
    assert not any(a["kind"] == "agent_action_rejected" for a in j["audit"])
    assert j["messages"][-1]["body"].startswith("Police never ask")


def test_flagged_transfer_also_sends_alert_email(tmp_path):
    transport = Recorder()
    settings = Settings(alert_email=True, customer_name="Junhao", customer_phone=PHONE)
    client = TestClient(create_app(str(tmp_path / "e.db"), transport=transport, settings=settings))
    j = create(client)
    assert [m["channel"] for m in transport.sent] == ["WHATSAPP", "SES"]
    email = transport.sent[1]
    assert email["subject"].startswith("KENA security alert: S$8,000.00") and "Dear Junhao" in email["body"]
    assert "WhatsApp" in email["body"] and "still pending" in email["body"]
    low = create(client, event_id="low", amount_cents=1000, new_payee=False, payee="Known payee • 1930", unusual_device=False, recent_payee_count=0)
    assert low["messages"] == [] and len(transport.sent) == 2


def test_consent_message_is_last_even_if_agent_talks_after_proposing(tmp_path):
    class Chatty:
        name = "chatty"
        def respond(self, context):
            return [{"type": "propose_protection", "reason": "police impersonation"},
                    {"type": "send_message", "text": "This looks like a scam. Real police never ask you to move money."}]
    client = TestClient(create_app(str(tmp_path / "o.db"), agent=Chatty(), settings=SETTINGS))
    j = create(client)
    j = client.post(f"/api/customer/{j['id']}/replies", headers={"X-Demo-Customer": "demo-customer"},
                    json={"message_id": "r1", "text": "the police told me to move it"}).json()
    assert j["messages"][-2]["body"].startswith("This looks like a scam")
    assert j["messages"][-1]["buttons"][0]["id"] == "confirm:" + j["proposal"]["ref"]


def test_send_failure_reason_is_logged_not_exposed(tmp_path, caplog):
    transport = Recorder(failing={"SES"})
    client = TestClient(create_app(str(tmp_path / "l.db"), transport=transport, settings=Settings(alert_email=True, customer_phone=PHONE)))
    with caplog.at_level("WARNING", logger="kena"):
        j = create(client)
    assert [m["status"] for m in j["messages"]] == ["accepted", "failed"]
    assert "provider down" in caplog.text and "provider down" not in str(j)
