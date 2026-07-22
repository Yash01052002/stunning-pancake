from app import crud
from app.models import CustomerTier, TicketChannel, TicketPriority, TicketStatus, UserRole


def _create_ticket_via_api(client, headers, subject, body="details here"):
    resp = client.post("/tickets", json={"subject": subject, "body": body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _get_ticket(client, headers, ticket_id):
    resp = client.get(f"/tickets/{ticket_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_rule_match_sets_category_priority_team_and_agent(
    client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    tech_agent = crud.create_user(
        db_session,
        email="tech1@example.com",
        password="pass12345",
        full_name="Tech One",
        role=UserRole.AGENT,
        team_id=technical.id,
    )
    crud.create_triage_rule(
        db_session,
        name="Outage",
        keyword="down",
        category="incident",
        priority=TicketPriority.P1,
        team_id=technical.id,
        active=True,
        evaluation_order=10,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["category"] == "incident"
    assert ticket["priority"] == "p1"
    assert ticket["assigned_team_id"] == technical.id
    assert ticket["assigned_agent_id"] == tech_agent.id
    assert ticket["triage_outcome"] == "matched"
    assert ticket["triage_method"] == "rule"
    assert ticket["confidence_score"] == 1.0


def test_premium_customer_gets_priority_boost(client, db_session, agent_headers):
    billing = crud.create_team(db_session, "Billing")
    crud.create_triage_rule(
        db_session,
        name="Refund",
        keyword="refund",
        category="billing",
        priority=TicketPriority.P2,
        team_id=billing.id,
        evaluation_order=10,
    )
    premium_customer = crud.create_user(
        db_session,
        email="vip@example.com",
        password="pass12345",
        full_name="VIP Customer",
        role=UserRole.CUSTOMER,
        tier=CustomerTier.PREMIUM,
    )

    resp = client.post(
        "/auth/login", json={"email": "vip@example.com", "password": "pass12345"}
    )
    vip_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    ticket = _create_ticket_via_api(client, vip_headers, "I want a refund please")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["priority"] == "p1"  # boosted from p2 -> p1


def test_standard_tier_gets_no_boost(client, db_session, customer_headers, agent_headers):
    billing = crud.create_team(db_session, "Billing")
    crud.create_triage_rule(
        db_session,
        name="Refund",
        keyword="refund",
        category="billing",
        priority=TicketPriority.P2,
        team_id=billing.id,
        evaluation_order=10,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "I want a refund please")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["priority"] == "p2"


def test_unmatched_ticket_falls_back_to_triage_team(client, db_session, customer_headers, agent_headers):
    fallback_team = crud.create_team(db_session, "Triage")

    ticket = _create_ticket_via_api(client, customer_headers, "What features do I have?")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["category"] is None
    assert ticket["priority"] is None
    assert ticket["triage_outcome"] == "unmatched"
    assert ticket["assigned_team_id"] == fallback_team.id


def test_unmatched_ticket_without_fallback_team_stays_unassigned(
    client, customer_headers, agent_headers
):
    ticket = _create_ticket_via_api(client, customer_headers, "What features do I have?")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["triage_outcome"] == "unmatched"
    assert ticket["assigned_team_id"] is None


def test_load_based_routing_prefers_least_loaded_agent(
    client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    agent1 = crud.create_user(
        db_session, email="t1@example.com", password="pass12345", full_name="T1",
        role=UserRole.AGENT, team_id=technical.id,
    )
    agent2 = crud.create_user(
        db_session, email="t2@example.com", password="pass12345", full_name="T2",
        role=UserRole.AGENT, team_id=technical.id,
    )
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )

    # give agent1 an existing open ticket so agent2 is the less-loaded one
    busy_customer = crud.create_user(
        db_session, email="busy@example.com", password="pass12345", full_name="Busy",
        role=UserRole.CUSTOMER,
    )
    existing_ticket = crud.create_ticket(
        db_session, customer_id=busy_customer.id, subject="unrelated", body="unrelated",
        channel=TicketChannel.API, actor=busy_customer,
    )
    existing_ticket.assigned_agent_id = agent1.id
    existing_ticket.status = TicketStatus.OPEN
    db_session.commit()

    ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["assigned_agent_id"] == agent2.id


def test_manual_override_flips_triage_method_and_counts_as_overridden(
    client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    ticket_id = ticket["id"]
    detail = _get_ticket(client, agent_headers, ticket_id)
    assert detail["triage_method"] == "rule"

    resp = client.patch(
        f"/tickets/{ticket_id}", json={"priority": "p0"}, headers=agent_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["triage_method"] == "manual"

    report = client.get("/reports/triage-accuracy", headers=agent_headers).json()
    assert report["matched"] == 1
    assert report["overridden"] == 1
    assert report["accuracy_rate"] == 0.0
    assert report["auto_triage_success_rate"] == 0.0


def test_triage_accuracy_report_shape(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_team(db_session, "Triage")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )

    _create_ticket_via_api(client, customer_headers, "The site is down")  # matched
    _create_ticket_via_api(client, customer_headers, "Just a question")  # unmatched

    report = client.get("/reports/triage-accuracy", headers=agent_headers).json()
    assert report["total_tickets"] == 2
    assert report["matched"] == 1
    assert report["unmatched"] == 1
    assert report["overridden"] == 0
    assert report["coverage_rate"] == 0.5
    assert report["accuracy_rate"] == 1.0
    assert report["auto_triage_success_rate"] == 0.5


def test_customer_cannot_view_triage_report(client, customer_headers):
    resp = client.get("/reports/triage-accuracy", headers=customer_headers)
    assert resp.status_code == 403


def test_manual_retrigger_endpoint_after_adding_a_new_rule(
    client, db_session, admin_headers, customer_headers, agent_headers
):
    ticket = _create_ticket_via_api(client, customer_headers, "Need help with my password")
    detail = _get_ticket(client, agent_headers, ticket["id"])
    assert detail["triage_outcome"] == "unmatched"

    technical = crud.create_team(db_session, "Technical")
    resp = client.post(
        "/triage-rules",
        json={
            "name": "Password reset",
            "keyword": "password",
            "category": "account",
            "priority": "p3",
            "team_id": technical.id,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(f"/tickets/{ticket['id']}/triage", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category"] == "account"
    assert body["triage_outcome"] == "matched"


def test_customer_cannot_retrigger_triage(client, customer_headers):
    ticket = _create_ticket_via_api(client, customer_headers, "hello")
    resp = client.post(f"/tickets/{ticket['id']}/triage", headers=customer_headers)
    assert resp.status_code == 403


def test_triage_rule_crud_permissions(client, admin_headers, agent_headers, customer_headers):
    resp = client.post(
        "/triage-rules",
        json={"name": "X", "keyword": "x", "category": "x", "priority": "p3"},
        headers=agent_headers,
    )
    assert resp.status_code == 403

    resp = client.post(
        "/triage-rules",
        json={"name": "X", "keyword": "x", "category": "x", "priority": "p3"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    rule_id = resp.json()["id"]

    resp = client.get("/triage-rules", headers=customer_headers)
    assert resp.status_code == 403

    resp = client.get("/triage-rules", headers=agent_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.patch(f"/triage-rules/{rule_id}", json={"active": False}, headers=agent_headers)
    assert resp.status_code == 403

    resp = client.patch(f"/triage-rules/{rule_id}", json={"active": False}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_inactive_rule_is_not_matched(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, active=False, evaluation_order=10,
    )

    ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    ticket = _get_ticket(client, agent_headers, ticket["id"])

    assert ticket["triage_outcome"] == "unmatched"
