"""REST contract tests for the FastAPI service (spec section 5)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api import create_app


@pytest.fixture
def client(make_pipeline, store):
    with TestClient(create_app(pipeline=make_pipeline(), store=store, instrument=False)) as c:
        yield c


@pytest.fixture
def secure_client(make_pipeline, store):
    with TestClient(create_app(pipeline=make_pipeline(), store=store, instrument=False, api_key="test-key")) as c:
        yield c


def _post(client, body: str, ref: str, **kw):
    return client.post("/api/v1/tickets/ingest", json={"external_ticket_ref": ref, "channel": "email", "customer_id": "CUST-7", "body": body} | kw)


def test_ingest_returns_full_contract_for_auto_reply(client):
    r = _post(client, "Every deployment fails at the health check stage.", "API-1", customer_tier="enterprise")
    j = r.json()
    assert r.status_code == 200 and j["routing_decision"]["action"] == "AUTO_REPLY"
    assert j["priority_routing"] == {"priority_level": "critical", "priority_score": 95, "queue_position": 1}
    assert j["ticket_ui_assist_payload"]["doc_link"] == "https://docs.cloudserve.io/kb/DOC-DEPLOY-001" and j["escalation_payload"]["structured_summary_markdown"] is None
    assert j["governance_metadata"]["outbound_pii_check"] == "PASSED" and j["governance_metadata"]["citation_verified"]


def test_escalation_hides_draft_and_attaches_summary(client):
    j = _post(client, "We saw unauthorized SSH logins from an unknown IP.", "API-2").json()
    assert j["routing_decision"]["action"] == "ESCALATE_MANDATORY_INTENT" and j["ticket_ui_assist_payload"]["draft_response"] is None
    assert j["escalation_payload"]["target_queue"] == "TIER_2_SPECIALIST" and "### Ticket Context" in j["escalation_payload"]["structured_summary_markdown"]


def test_queue_position_ranks_waiting_tickets(client):
    _post(client, "it is not working, please call us", "API-Q1", customer_tier="standard")
    j = _post(client, "We saw unauthorized SSH logins from an unknown IP.", "API-Q2", customer_tier="enterprise").json()
    assert j["priority_routing"]["queue_position"] == 1


def test_feedback_and_audit_round_trip(client):
    j = _post(client, "Every deployment fails at the health check stage.", "API-3").json()
    fb = {"decision_id": j["decision_id"], "agent_id": "sofia", "agent_tier": "TIER_1", "agent_action": "ACCEPTED_AS_IS", "customer_csat_score": 5}
    assert client.post(f"/api/v1/tickets/{j['ticket_id']}/feedback", json=fb).status_code == 200
    assert client.post(f"/api/v1/tickets/{j['ticket_id']}/feedback", json=fb | {"decision_id": str(uuid.uuid4())}).status_code == 404
    audit = client.get(f"/api/v1/governance/audit/{j['ticket_id']}").json()
    assert audit["hash_chain_verified"] and audit["audit_record"]["trace_id"] == j["trace_id"]
    assert client.get(f"/api/v1/governance/audit/{uuid.uuid4()}").status_code == 404


def test_request_validation_and_health(client):
    assert client.post("/api/v1/tickets/ingest", json={"external_ticket_ref": "X", "channel": "fax", "customer_id": "C"}).status_code == 422
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["calibration_version"] == "cal_test"


def test_api_key_protects_business_endpoints(secure_client, client):
    body = {"external_ticket_ref": "SEC-1", "channel": "email", "customer_id": "CUST-7",
            "body": "Every deployment fails at the health check stage."}
    assert secure_client.post("/api/v1/tickets/ingest", json=body).status_code == 401
    assert secure_client.post("/api/v1/tickets/ingest", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    ok = secure_client.post("/api/v1/tickets/ingest", json=body, headers={"X-API-Key": "test-key"})
    assert ok.status_code == 200
    tid = ok.json()["ticket_id"]
    assert secure_client.get(f"/api/v1/governance/audit/{tid}").status_code == 401
    assert secure_client.get(f"/api/v1/governance/audit/{tid}", headers={"X-API-Key": "test-key"}).status_code == 200
    assert secure_client.get("/health").status_code == 200 and client.post("/api/v1/tickets/ingest", json=body).status_code == 200
