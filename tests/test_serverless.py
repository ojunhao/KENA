"""Serverless hosting: DynamoDB store (via moto), Lambda handler, CloudFront origin check, request-driven follow-ups."""
import json
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

moto = pytest.importorskip("moto")
HEADERS = {"X-Demo-Customer": "demo-customer"}


@pytest.fixture
def dynamo(monkeypatch):
    import boto3
    for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="kena-test", BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])
        yield client


def test_full_journey_on_dynamodb(dynamo):
    client = TestClient(create_app(settings=Settings(dynamo_table="kena-test")))
    j = client.post("/api/events", json={"event_id": "e1", "preferred_channel": "WHATSAPP"}).json()
    assert client.post("/api/events", json={"event_id": "e1", "preferred_channel": "WHATSAPP"}).json()["id"] == j["id"]
    client.post("/api/events", json={"event_id": "e2"})
    j = client.post(f"/api/customer/{j['id']}/replies", headers=HEADERS, json={"message_id": "r1", "text": "not me"}).json()
    j = client.post(f"/api/customer/{j['id']}/confirm", headers=HEADERS,
                    json={"command_id": "c1", "token": j["proposal"]["token"], "approved_actions": ["pause_transfer", "create_scam_case"]}).json()
    assert j["transaction_status"] == "paused" and j["case"]
    dashboard = client.get("/api/dashboard").json()
    assert [x["event_id"] for x in dashboard["journeys"]] == ["e2", "e1"]
    assert dashboard["metrics"]["protected"] == 1
    assert "lock#journeys" not in json.dumps(dynamo.scan(TableName="kena-test")["Items"])


def test_origin_secret_and_lambda_handler(dynamo, monkeypatch):
    settings = Settings(dynamo_table="kena-test", origin_secret="s3cret", tick_on_request=True, worker_interval_seconds=0)
    client = TestClient(create_app(settings=settings))
    assert client.get("/healthz").status_code == 403
    assert client.get("/healthz", headers={"x-kena-origin": "s3cret"}).json()["status"] == "ok"
    assert client.get("/api/dashboard", headers={"x-kena-origin": "s3cret"}).status_code == 200
    import app.lambda_handler as entry
    assert callable(entry.handler)


def test_lambda_handler_serves_function_url_events(dynamo, monkeypatch):
    import importlib
    monkeypatch.setenv("KENA_DYNAMO_TABLE", "kena-test")
    monkeypatch.setenv("KENA_ORIGIN_SECRET", "s3cret")
    monkeypatch.setenv("KENA_BASIC_AUTH", "kena:pw")
    import app.main
    import app.lambda_handler
    importlib.reload(app.main)
    entry = importlib.reload(app.lambda_handler)

    def event(path, headers):
        return {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "", "headers": headers,
                "requestContext": {"http": {"method": "GET", "path": path, "protocol": "HTTP/1.1", "sourceIp": "1.2.3.4", "userAgent": "test"},
                                   "domainName": "abc.lambda-url.us-east-1.on.aws", "requestId": "r", "stage": "$default"},
                "isBase64Encoded": False}
    import base64
    auth = "Basic " + base64.b64encode(b"kena:pw").decode()
    assert entry.handler(event("/healthz", {}), None)["statusCode"] == 403
    assert entry.handler(event("/api/dashboard", {"x-kena-origin": "s3cret"}), None)["statusCode"] == 401
    ok = entry.handler(event("/api/dashboard", {"x-kena-origin": "s3cret", "authorization": auth}), None)
    assert ok["statusCode"] == 200 and json.loads(ok["body"])["journeys"] == []
    page = entry.handler(event("/device", {"x-kena-origin": "s3cret", "authorization": auth}), None)
    assert page["statusCode"] == 200 and "phone" in page["body"]
