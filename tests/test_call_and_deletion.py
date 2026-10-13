from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.device_signals import CallStateObservation, normalize_telephony_state

HEADERS = {"X-Demo-Customer": "demo-customer"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "calls.db")))


def transfer(client, **fields):
    r = client.post("/api/events", json={"event_id": "event-1", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def test_active_call_increases_score_and_targets_outreach(client):
    baseline = transfer(client, active_call=False, unusual_device=False, recent_payee_count=0)
    on_call = transfer(client, event_id="event-2", active_call=True, unusual_device=False, recent_payee_count=0)
    assert baseline["risk"]["score"] == 65
    assert on_call["risk"]["score"] == 90
    assert on_call["call_context"]["state"] == "active"
    assert on_call["call_context"]["source"] == "simulator"
    assert "someone on the call instructing" in on_call["messages"][0]["body"]
    assert "Active phone call" in " ".join(on_call["risk"]["signals"])
    assert on_call["transaction_status"] == "pending"
    assert on_call["case"] is None


def test_call_can_cross_intervention_threshold(client):
    baseline = transfer(client, amount_cents=400000, active_call=False)
    on_call = transfer(client, event_id="event-2", amount_cents=400000, active_call=True, rcs_available=False)
    assert baseline["risk"]["score"] == 45
    assert baseline["messages"] == []
    assert on_call["risk"]["score"] == 70
    assert on_call["journey_status"] == "awaiting_customer"
    assert on_call["messages"][0]["channel"] == "SMS"


def test_call_alone_is_not_fraud(client):
    j = transfer(client, amount_cents=10000, new_payee=False, unusual_device=False, recent_payee_count=0, active_call=True)
    assert j["risk"]["score"] == 25
    assert j["journey_status"] == "not_flagged"
    assert j["messages"] == []


@pytest.mark.parametrize("value,state", [(None, "unknown"), (False, "inactive"), (True, "active")])
def test_three_state_call_context(client, value, state):
    assert transfer(client, active_call=value)["call_context"]["state"] == state


def test_missing_call_signal_is_unknown(client):
    assert transfer(client)["call_context"]["state"] == "unknown"


@pytest.mark.parametrize("value", ["true", "false", 1, 0, {}, []])
def test_call_signal_requires_boolean(client, value):
    assert client.post("/api/events", json={"event_id": "e", "active_call": value}).status_code == 422


def test_score_is_capped(client):
    j = transfer(client, active_call=True)
    assert j["risk"]["raw_score"] == 105
    assert j["risk"]["score"] == 100


def test_call_does_not_bypass_explicit_consent(client):
    j = transfer(client, active_call=True)
    disputed = client.post(f"/api/customer/{j['id']}/replies", headers=HEADERS, json={"message_id": "reply", "text": "someone told me to transfer this"}).json()
    assert disputed["transaction_status"] == "pending"
    assert disputed["case"] is None
    assert disputed["proposal"] is not None
    protected = client.post(f"/api/customer/{j['id']}/confirm", headers=HEADERS, json={"command_id": "confirm", "token": disputed["proposal"]["token"], "approved_actions": ["pause_transfer", "create_scam_case"]}).json()
    assert protected["transaction_status"] == "paused"


def test_minimal_signal_boundary_rejects_call_content(client):
    for field in ["call_audio", "phone_number", "call_transcript", "call_history"]:
        assert client.post("/api/events", json={"event_id": "e", "active_call": True, field: "should never be collected"}).status_code == 422


def test_device_observation_freshness():
    at = datetime(2026, 10, 13, tzinfo=timezone.utc)
    observation = CallStateObservation(True, at, "android_telephony")
    assert observation.fresh_value(at + timedelta(seconds=30)) is True
    assert observation.fresh_value(at + timedelta(seconds=31)) is None
    assert observation.fresh_value(at - timedelta(seconds=1)) is None
    assert CallStateObservation(None, at, "android_telephony").fresh_value(at) is None
    with pytest.raises(ValueError):
        observation.fresh_value(at.replace(tzinfo=None))


@pytest.mark.parametrize("state,permission,expected", [(0, True, False), (2, True, True), (1, True, None), (2, False, None), (None, True, None), (9, True, None), (True, True, None)])
def test_android_normalization_keeps_unavailable_unknown(state, permission, expected):
    assert normalize_telephony_state(state, permission_granted=permission) is expected


def test_amount_preserves_cents(client):
    j = transfer(client, amount_cents=812345, active_call=True)
    assert j["event"]["amount_cents"] == 812345
    assert "S$8,123.45" in j["messages"][0]["body"]


def test_delete_removes_views_retains_audit_and_is_idempotent(client):
    j = transfer(client)
    path = f"/api/journeys/{j['id']}"
    assert client.delete(path).json() == {"id": j["id"], "deleted": True}
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404
    assert client.get(f"/api/customer/{j['id']}", headers=HEADERS).status_code == 404
    assert client.get("/api/dashboard").json()["journeys"] == []
    assert client.get("/api/dashboard").json()["metrics"]["flagged"] == 0
    store = client.app.state.service.store
    with store.connection() as db:
        archived = store.get(db, j["id"])
        assert archived["deleted_at"]
        assert archived["audit"][-1]["kind"] == "demo_transfer_deleted"
        assert len([a for a in archived["audit"] if a["kind"] == "demo_transfer_deleted"]) == 1
    assert client.post("/api/events", json={"event_id": "event-1"}).status_code == 409


def test_deleted_proposal_cannot_execute(client):
    j = transfer(client)
    j = client.post(f"/api/customer/{j['id']}/replies", headers=HEADERS, json={"message_id": "reply", "text": "not me"}).json()
    token = j["proposal"]["token"]
    client.delete(f"/api/journeys/{j['id']}")
    assert client.post(f"/api/customer/{j['id']}/confirm", headers=HEADERS, json={"command_id": "c", "token": token, "approved_actions": ["pause_transfer", "create_scam_case"]}).status_code == 404


def test_deleted_transfer_stays_removed_after_restart(tmp_path):
    path = str(tmp_path / "deleted.db")
    first = TestClient(create_app(path))
    j = transfer(first)
    first.delete(f"/api/journeys/{j['id']}")
    assert TestClient(create_app(path)).get("/api/dashboard").json()["journeys"] == []


def test_missing_delete_does_not_affect_other_transfer(client):
    j = transfer(client)
    assert client.delete("/api/journeys/not-found").status_code == 404
    assert client.get(f"/api/journeys/{j['id']}").status_code == 200
