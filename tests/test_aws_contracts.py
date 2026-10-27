import io
import json
from types import SimpleNamespace
import boto3
import pytest
from botocore.stub import Stubber
from app.agent import AgentRefusal, ClaudeAgent, LocalAgent
from app.agentcore import AgentCoreAgent, decide
from app.messaging import AwsCommunications, whatsapp_payload


def client(service):
    # Non-secret test placeholders ensure no metadata service or credential discovery.
    return boto3.client(service, region_name="ap-southeast-1", aws_access_key_id="TEST", aws_secret_access_key="TEST")


def communications(sms, ses, social=None, **kwargs):
    return AwsCommunications(sms, ses, social, sms_identity="pool-demo", rcs_identity="arn:aws:sms-voice:us-east-1:123456789012:rcs-agent/demo",
                             phone="+6590000000", email="customer@example.invalid", sender="bank@example.invalid",
                             wa_phone_number_id="phone-number-id-demo", **kwargs)


def test_live_transport_is_locked_by_default():
    with pytest.raises(ValueError, match="approval"):
        communications(None, None)


@pytest.mark.parametrize("channel", ["SMS", "RCS"])
def test_sms_rcs_sdk_contract(channel):
    sms, ses = client("pinpoint-sms-voice-v2"), client("sesv2")
    identity = "pool-demo" if channel == "SMS" else "arn:aws:sms-voice:us-east-1:123456789012:rcs-agent/demo"
    with Stubber(sms) as stub:
        stub.add_response("send_text_message", {"MessageId": "provider-1"}, {
            "DestinationPhoneNumber": "+6590000000", "OriginationIdentity": identity,
            "MessageBody": "Synthetic alert", "MessageType": "TRANSACTIONAL"})
        result = communications(sms, ses, approved=True).send({"channel": channel, "body": "Synthetic alert"})
        assert result == {"status": "accepted", "provider_id": "provider-1"}
        stub.assert_no_pending_responses()


def test_ses_sdk_contract():
    sms, ses = client("pinpoint-sms-voice-v2"), client("sesv2")
    with Stubber(ses) as stub:
        stub.add_response("send_email", {"MessageId": "ses-1"}, {
            "FromEmailAddress": "bank@example.invalid", "Destination": {"ToAddresses": ["customer@example.invalid"]},
            "Content": {"Simple": {"Subject": {"Data": "KENA: protection confirmation"}, "Body": {"Text": {"Data": "Paused"}}}}})
        assert communications(sms, ses, approved=True).send({"channel": "SES", "body": "Paused"})["status"] == "accepted"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("message,kind", [
    ({"body": "Hello"}, "text"),
    ({"body": "Did you make it?", "buttons": [{"id": "reply:verify", "title": "Yes, I made it"}]}, "interactive"),
    ({"body": "Alert", "template": {"name": "kena_transfer_alert", "params": ["S$8,000.00", "New payee"]}}, "template"),
])
def test_whatsapp_sdk_contract(message, kind):
    social = client("socialmessaging")
    payload = whatsapp_payload(message, "+6590000000")
    assert payload["type"] == kind and payload["to"] == "6590000000"
    with Stubber(social) as stub:
        stub.add_response("send_whatsapp_message", {"messageId": "wamid.demo"}, {
            "originationPhoneNumberId": "phone-number-id-demo", "message": json.dumps(payload).encode(), "metaApiVersion": "v20.0"})
        result = communications(None, None, social, approved=True).send({"channel": "WHATSAPP", **message})
        assert result == {"status": "accepted", "provider_id": "wamid.demo"}
        stub.assert_no_pending_responses()


def test_channels_reflect_configuration():
    transport = AwsCommunications(None, None, None, sms_identity=None, rcs_identity=None, phone="+6590000000",
                                  email="customer@example.invalid", sender="bank@example.invalid", approved=True)
    assert transport.channels == {"SES"}
    assert communications(None, None, object(), approved=True).channels == {"WHATSAPP", "SMS", "RCS", "SES"}


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


class FakeClaude:
    """Stands in for AnthropicBedrockMantle: returns scripted responses and records requests."""
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.messages = self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def journey(**overrides):
    base = {"id": "txn-demo", "event": {"amount_cents": 800000, "payee": "New payee • 4821", "currency": "SGD"},
            "transaction_status": "pending", "journey_status": "awaiting_customer", "risk": {"level": "HIGH", "score": 80, "signals": ["New payee"]},
            "channel": "WHATSAPP", "case": None, "messages": [], "call_context": {"state": "active"}}
    return {**base, **overrides}


def context(text="someone from the police told me to move my money", **overrides):
    return {"trigger": "customer_reply", "text": text, "journey": journey(**overrides), "channels": ["SES", "WHATSAPP"], "customer_name": "Alex"}


def test_claude_agent_tool_loop_and_policy_feedback():
    fake = FakeClaude(
        SimpleNamespace(stop_reason="tool_use", content=[block("tool_use", id="t1", name="search_scam_playbook", input={"query": "police safe account"})]),
        SimpleNamespace(stop_reason="tool_use", content=[
            block("tool_use", id="t2", name="send_message", input={"text": "Please send me your OTP now."}),
            block("tool_use", id="t3", name="send_message", input={"text": "Thank you for telling me. Police never ask you to move money.", "channel": "SMS"}),
            block("tool_use", id="t4", name="send_message", input={"text": "Thank you for telling me. Police never ask you to move money."}),
            block("tool_use", id="t5", name="propose_protection", input={"reason": "Government impersonation pattern"})]),
        SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Done.")]),
    )
    actions = ClaudeAgent(fake, "anthropic.claude-opus-5-5").respond(context())
    assert [a["type"] for a in actions] == ["knowledge_lookup", "send_message", "propose_protection"]
    assert actions[0]["matches"][0] == "government-impersonation"
    assert fake.requests[0]["model"] == "anthropic.claude-opus-5-5"
    assert "tool_choice" not in fake.requests[0] and "output_config" not in fake.requests[0]
    playbook = json.loads(fake.requests[1]["messages"][-1]["content"][0]["content"])
    assert playbook["matches"][0]["id"] == "government-impersonation"
    results = [json.loads(r["content"]) for r in fake.requests[2]["messages"][-1]["content"]]
    assert results[0] == {"ok": False, "reason": "credential_request_not_allowed"}
    assert results[1] == {"ok": False, "reason": "channel_unavailable"}
    assert results[2]["ok"] and results[3]["ok"]


def test_claude_agent_cannot_propose_above_cap_or_after_pause():
    propose = SimpleNamespace(stop_reason="tool_use", content=[block("tool_use", id="t1", name="propose_protection", input={"reason": "x"})])
    end = SimpleNamespace(stop_reason="end_turn", content=[])
    assert ClaudeAgent(FakeClaude(propose, end), "m").respond(context(event={"amount_cents": 20000000, "payee": "p", "currency": "SGD"})) == []
    assert ClaudeAgent(FakeClaude(propose, end), "m").respond(context(transaction_status="paused", journey_status="protected")) == []


def test_claude_agent_refusal_and_effort():
    fake = FakeClaude(SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(AgentRefusal):
        ClaudeAgent(fake, "m", effort="low").respond(context())
    assert fake.requests[0]["output_config"] == {"effort": "low"}


def test_agentcore_sdk_contract():
    provider = client("bedrock-agentcore")
    actions = [{"type": "propose_protection", "reason": "dispute"}]
    with Stubber(provider) as stub:
        stub.add_response("invoke_agent_runtime", {"response": io.BytesIO(json.dumps({"actions": actions}).encode()), "contentType": "application/json", "statusCode": 200})
        assert AgentCoreAgent(provider, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/demo").respond(context()) == actions
        stub.assert_no_pending_responses()


def test_agentcore_entrypoint_local_decision():
    assert decide({"context": context("not me")}, LocalAgent()) == {"actions": [{"type": "propose_protection", "reason": "Customer disputed the transfer"}]}
    with pytest.raises(ValueError):
        decide({"context": {"trigger": "other"}}, LocalAgent())
