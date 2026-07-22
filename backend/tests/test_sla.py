from datetime import datetime, timedelta, timezone

from app import crud, sla
from app.models import CustomerTier, Ticket, TicketChannel, TicketPriority, TicketStatus, UserRole


def _create_ticket_via_api(client, headers, subject="The site is down", body="Everything broken"):
    resp = client.post("/tickets", json={"subject": subject, "body": body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- policy lookup precedence ----


def test_sla_policy_tier_specific_beats_general(db_session):
    crud.create_sla_policy(
        db_session, priority=TicketPriority.P1, tier=None,
        first_response_minutes=240, resolution_minutes=1440,
    )
    crud.create_sla_policy(
        db_session, priority=TicketPriority.P1, tier=CustomerTier.PREMIUM,
        first_response_minutes=30, resolution_minutes=240,
    )

    premium_match = sla.find_sla_policy(db_session, TicketPriority.P1, CustomerTier.PREMIUM)
    assert premium_match.first_response_minutes == 30

    standard_match = sla.find_sla_policy(db_session, TicketPriority.P1, CustomerTier.STANDARD)
    assert standard_match.first_response_minutes == 240

    no_tier_match = sla.find_sla_policy(db_session, TicketPriority.P1, None)
    assert no_tier_match.first_response_minutes == 240


def test_no_matching_policy_returns_none(db_session):
    assert sla.find_sla_policy(db_session, TicketPriority.P0, None) is None


# ---- due-date computation via triage ----


def test_triage_sets_sla_due_dates_from_matching_policy(
    client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    crud.create_sla_policy(
        db_session, priority=TicketPriority.P1, tier=None,
        first_response_minutes=60, resolution_minutes=480,
    )

    ticket = _create_ticket_via_api(client, customer_headers)
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()

    created_at = datetime.fromisoformat(detail["created_at"])
    expected_first_response = created_at + timedelta(minutes=60)
    expected_resolution = created_at + timedelta(minutes=480)

    assert datetime.fromisoformat(detail["first_response_due_at"]) == expected_first_response
    assert datetime.fromisoformat(detail["resolution_due_at"]) == expected_resolution
    assert detail["first_response_sla_status"] == "on_track"
    assert detail["resolution_sla_status"] == "on_track"


def test_no_matching_policy_leaves_due_dates_null(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    # no SLA policy created at all

    ticket = _create_ticket_via_api(client, customer_headers)
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()

    assert detail["priority"] == "p1"
    assert detail["first_response_due_at"] is None
    assert detail["first_response_sla_status"] is None


def test_manual_priority_change_recomputes_sla_due_dates(
    client, db_session, customer_headers, agent_headers
):
    crud.create_sla_policy(
        db_session, priority=TicketPriority.P2, tier=None,
        first_response_minutes=1440, resolution_minutes=4320,
    )
    crud.create_sla_policy(
        db_session, priority=TicketPriority.P0, tier=None,
        first_response_minutes=15, resolution_minutes=120,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "Just a question", "no urgency")
    resp = client.patch(
        f"/tickets/{ticket['id']}", json={"priority": "p2"}, headers=agent_headers
    )
    assert resp.status_code == 200, resp.text
    first_due = resp.json()["first_response_due_at"]
    assert first_due is not None

    resp = client.patch(
        f"/tickets/{ticket['id']}", json={"priority": "p0"}, headers=agent_headers
    )
    assert resp.status_code == 200, resp.text
    new_due = resp.json()["first_response_due_at"]
    assert new_due != first_due  # recomputed for the tighter P0 policy


# ---- sla_status property (unit-level, no waiting on real clocks) ----


def test_sla_status_property_transitions(db_session):
    team = crud.create_team(db_session, "T")
    customer = crud.create_user(
        db_session, email="statuscust@example.com", password="pass12345", full_name="C",
        role=UserRole.CUSTOMER,
    )
    ticket = crud.create_ticket(
        db_session, customer_id=customer.id, subject="s", body="b",
        channel=TicketChannel.API, actor=customer,
    )

    assert ticket.first_response_sla_status is None  # no due date yet

    now = datetime.now(timezone.utc)
    ticket.first_response_due_at = now + timedelta(hours=1)
    db_session.commit()
    assert ticket.first_response_sla_status == "on_track"

    ticket.first_response_due_at = now + timedelta(minutes=5)  # inside default 30-min at-risk window
    db_session.commit()
    assert ticket.first_response_sla_status == "at_risk"

    ticket.first_response_due_at = now - timedelta(minutes=5)
    db_session.commit()
    assert ticket.first_response_sla_status == "breached"

    ticket.first_responded_at = now - timedelta(minutes=10)  # responded before the (past) due date
    db_session.commit()
    assert ticket.first_response_sla_status == "met"

    ticket.first_responded_at = now  # responded, but after the due date
    db_session.commit()
    assert ticket.first_response_sla_status == "breached_late"


# ---- first-response tracking ----


def test_first_response_recorded_on_first_public_staff_comment(
    client, customer_headers, agent_headers
):
    ticket = _create_ticket_via_api(client, customer_headers)
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()
    assert detail["first_responded_at"] is None

    client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "internal note", "is_internal": True},
        headers=agent_headers,
    )
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()
    assert detail["first_responded_at"] is None  # internal notes don't count

    client.post(
        f"/tickets/{ticket['id']}/comments", json={"body": "we're on it"}, headers=agent_headers
    )
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()
    assert detail["first_responded_at"] is not None


def test_customer_comment_does_not_count_as_first_response(client, customer_headers, agent_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    client.post(
        f"/tickets/{ticket['id']}/comments", json={"body": "any update?"}, headers=customer_headers
    )
    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()
    assert detail["first_responded_at"] is None


# ---- customer notifications on status transitions ----


def test_ticket_creation_sends_received_notification(client, customer_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    resp = client.get("/notifications", headers=customer_headers)
    assert resp.status_code == 200
    types = [n["type"] for n in resp.json()]
    assert "ticket_received" in types
    assert all(n["ticket_id"] == ticket["id"] for n in resp.json())


def test_status_change_sends_customer_notifications(client, customer_headers, agent_headers):
    ticket = _create_ticket_via_api(client, customer_headers)

    client.patch(f"/tickets/{ticket['id']}", json={"status": "open"}, headers=agent_headers)
    types = [n["type"] for n in client.get("/notifications", headers=customer_headers).json()]
    assert "ticket_in_progress" in types

    client.patch(f"/tickets/{ticket['id']}", json={"status": "resolved"}, headers=agent_headers)
    types = [n["type"] for n in client.get("/notifications", headers=customer_headers).json()]
    assert "ticket_resolved" in types


def test_notifications_are_scoped_to_owner(client, customer_headers, other_customer_headers):
    _create_ticket_via_api(client, customer_headers)
    resp = client.get("/notifications", headers=other_customer_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_mark_notification_read(client, customer_headers, other_customer_headers):
    _create_ticket_via_api(client, customer_headers)
    notification = client.get("/notifications", headers=customer_headers).json()[0]
    assert notification["read_at"] is None

    resp = client.patch(f"/notifications/{notification['id']}/read", headers=customer_headers)
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None

    # another user can't mark (or even discover) someone else's notification
    resp = client.patch(
        f"/notifications/{notification['id']}/read", headers=other_customer_headers
    )
    assert resp.status_code == 404


# ---- escalation ----


def test_escalation_bumps_priority_reassigns_notifies_and_is_idempotent(
    client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    agent1 = crud.create_user(
        db_session, email="esc1@example.com", password="pass12345", full_name="E1",
        role=UserRole.AGENT, team_id=technical.id,
    )
    agent2 = crud.create_user(
        db_session, email="esc2@example.com", password="pass12345", full_name="E2",
        role=UserRole.AGENT, team_id=technical.id,
    )
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P2, team_id=technical.id, evaluation_order=10,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    ticket_id = ticket["id"]

    # give agent1 a bunch of load so escalation's reassignment prefers agent2
    busy_customer = crud.create_user(
        db_session, email="escbusy@example.com", password="pass12345", full_name="Busy",
        role=UserRole.CUSTOMER,
    )
    other = crud.create_ticket(
        db_session, customer_id=busy_customer.id, subject="x", body="y",
        channel=TicketChannel.API, actor=busy_customer,
    )
    other.assigned_agent_id = agent1.id
    other.status = TicketStatus.OPEN
    db_session.commit()

    db_ticket = db_session.get(Ticket, ticket_id)
    assert db_ticket.priority == TicketPriority.P2
    db_ticket.first_response_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    resp = client.post("/sla/escalate", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["escalated_count"] == 1
    assert ticket_id in report["escalated_ticket_ids"]

    detail = client.get(f"/tickets/{ticket_id}", headers=agent_headers).json()
    assert detail["priority"] == "p1"  # bumped from p2
    assert detail["assigned_agent_id"] == agent2.id  # reassigned to the less-loaded agent
    assert detail["escalated_at"] is not None
    event_types = [e["type"] for e in detail["events"]]
    assert "sla_escalated" in event_types

    # the newly assigned agent got an in-app notification
    agent2_login = client.post(
        "/auth/login", json={"email": "esc2@example.com", "password": "pass12345"}
    ).json()
    agent2_headers = {"Authorization": f"Bearer {agent2_login['access_token']}"}
    notif_types = [n["type"] for n in client.get("/notifications", headers=agent2_headers).json()]
    assert "sla_escalated" in notif_types

    # idempotent: running again does not re-escalate the same ticket
    resp = client.post("/sla/escalate", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json()["escalated_count"] == 0
    detail_again = client.get(f"/tickets/{ticket_id}", headers=agent_headers).json()
    assert detail_again["priority"] == "p1"  # unchanged, not bumped a second time


def test_escalation_requires_staff(client, customer_headers):
    resp = client.post("/sla/escalate", headers=customer_headers)
    assert resp.status_code == 403


def test_sla_policy_crud_permissions(client, admin_headers, agent_headers, customer_headers):
    resp = client.post(
        "/sla-policies",
        json={"priority": "p3", "first_response_minutes": 4320, "resolution_minutes": 10080},
        headers=agent_headers,
    )
    assert resp.status_code == 403

    resp = client.post(
        "/sla-policies",
        json={"priority": "p3", "first_response_minutes": 4320, "resolution_minutes": 10080},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    policy_id = resp.json()["id"]

    resp = client.get("/sla-policies", headers=customer_headers)
    assert resp.status_code == 403

    resp = client.get("/sla-policies", headers=agent_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.patch(f"/sla-policies/{policy_id}", json={"active": False}, headers=agent_headers)
    assert resp.status_code == 403

    resp = client.patch(f"/sla-policies/{policy_id}", json={"active": False}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["active"] is False
