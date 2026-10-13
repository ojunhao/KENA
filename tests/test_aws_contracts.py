import io
import json
import boto3
import pytest
from botocore.stub import Stubber
from app.agent import BedrockAgent, LocalAgent
from app.agentcore import AgentCoreAgent, decide
from app.messaging import AwsCommunications


def client(service):
    # Non-secret test placeholders ensure no metadata service or credential discovery.
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="TEST", aws_secret_access_key="TEST")


def communications(sms, ses, **kwargs):
    return AwsCommunications(sms, ses, sms_identity="pool-demo", rcs_identity="arn:aws:sms-voice:us-east-1:123456789012:rcs-agent/demo", phone="+12065550100", email="customer@example.invalid", sender="bank@example.invalid", **kwargs)


def test_live_transport_is_locked_by_default():
    with pytest.raises(ValueError, match="approval"):
        communications(None, None)


@pytest.mark.parametrize("channel", ["SMS", "RCS"])
def test_sms_rcs_sdk_contract(channel):
    sms, ses = client("pinpoint-sms-voice-v2"), client("sesv2")
    identity = "pool-demo" if channel == "SMS" else "arn:aws:sms-voice:us-east-1:123456789012:rcs-agent/demo"
    with Stubber(sms) as stub:
        stub.add_response("send_text_message", {"MessageId": "provider-1"}, {
            "DestinationPhoneNumber": "+12065550100", "OriginationIdentity": identity,
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


class FakeBedrock:
    def __init__(self, text):
        self.text = text
        self.requests = []
    def converse(self, **request):
        self.requests.append(request)
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def test_bedrock_intent_validation():
    provider = FakeBedrock('{"intent":"release_transfer"}')
    agent = BedrockAgent(provider, "test-model")
    assert agent.classify("please release", {}) == "uncertain"
    assert "toolConfig" not in provider.requests[0]
    assert provider.requests[0]["modelId"] == "test-model"
    provider.text = '{"intent":"dispute"}'
    assert agent.classify("not me", {}) == "dispute"


def test_bedrock_sdk_contract():
    provider = client("bedrock-runtime")
    with Stubber(provider) as stub:
        stub.add_response("converse", {
            "output": {"message": {"role": "assistant", "content": [{"text": '{"intent":"dispute"}'}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}, "metrics": {"latencyMs": 1}})
        assert BedrockAgent(provider, "test-model").classify("not me", {}) == "dispute"
        stub.assert_no_pending_responses()


def test_agentcore_sdk_contract():
    provider = client("bedrock-agentcore")
    with Stubber(provider) as stub:
        stub.add_response("invoke_agent_runtime", {"response": io.BytesIO(b'{"intent":"dispute"}'), "contentType": "application/json", "statusCode": 200})
        assert AgentCoreAgent(provider, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/demo").classify("not me", {}) == "dispute"
        stub.assert_no_pending_responses()


def test_agentcore_entrypoint_local_decision():
    assert decide({"text": "not me", "context": {}}, LocalAgent()) == {"intent": "dispute"}
    with pytest.raises(ValueError):
        decide({"text": "", "context": {}}, LocalAgent())
