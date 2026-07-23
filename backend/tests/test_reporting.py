import csv
import io

from app import crud
from app.models import CustomerTier, TicketChannel, TicketPriority, TicketStatus, UserRole


def _create_ticket_via_api(client, headers, subject="Help", body="details"):
    resp = client.post("/tickets", json={"subject": subject, "body": body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _resolve(client, agent_headers, ticket_id):
    resp = client.patch(
        f"/tickets/{ticket_id}", json={"status": "resolved"}, headers=agent_headers
    )
    assert resp.status_code == 200, resp.text


# ---- CSAT ----


def test_customer_rates_resolved_ticket(client, customer_headers, agent_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    _resolve(client, agent_headers, ticket["id"])

    resp = client.post(
        f"/tickets/{ticket['id']}/csat",
        json={"rating": 5, "comment": "Great help!"},
        headers=customer_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["csat_rating"] == 5
    assert body["csat_comment"] == "Great help!"
    assert body["csat_submitted_at"] is not None


def test_cannot_rate_unresolved_ticket(client, customer_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    resp = client.post(
        f"/tickets/{ticket['id']}/csat", json={"rating": 4}, headers=customer_headers
    )
    assert resp.status_code == 400


def test_cannot_rate_someone_elses_ticket(
    client, customer_headers, other_customer_headers, agent_headers
):
    ticket = _create_ticket_via_api(client, customer_headers)
    _resolve(client, agent_headers, ticket["id"])
    resp = client.post(
        f"/tickets/{ticket['id']}/csat", json={"rating": 5}, headers=other_customer_headers
    )
    assert resp.status_code == 403


def test_csat_rating_out_of_range_rejected(client, customer_headers, agent_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    _resolve(client, agent_headers, ticket["id"])
    resp = client.post(
        f"/tickets/{ticket['id']}/csat", json={"rating": 6}, headers=customer_headers
    )
    assert resp.status_code == 422  # pydantic Field(ge=1, le=5)


# ---- volume report ----


def test_volume_report_groups_by_category(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    crud.create_triage_rule(
        db_session, name="Refund", keyword="refund", category="billing",
        priority=TicketPriority.P2, team_id=technical.id, evaluation_order=20,
    )
    _create_ticket_via_api(client, customer_headers, "site is down")  # incident
    _create_ticket_via_api(client, customer_headers, "site is down again")  # incident
    _create_ticket_via_api(client, customer_headers, "refund please")  # billing

    resp = client.get("/reports/volume", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 3
    counts = {c["category"]: c["count"] for c in data["by_category"]}
    assert counts["incident"] == 2
    assert counts["billing"] == 1
    # by_category is ordered highest-first
    assert data["by_category"][0]["category"] == "incident"
    assert data["by_priority"]["p1"] == 2


def test_volume_report_staff_only(client, customer_headers):
    assert client.get("/reports/volume", headers=customer_headers).status_code == 403


# ---- SLA compliance report ----


def test_sla_compliance_via_api(client, db_session, agent_headers):
    from datetime import datetime, timedelta, timezone

    customer = crud.create_user(
        db_session, email="slac2@example.com", password="pass12345", full_name="C",
        role=UserRole.CUSTOMER,
    )
    now = datetime.now(timezone.utc)

    met = crud.create_ticket(
        db_session, customer_id=customer.id, subject="met", body="b",
        channel=TicketChannel.API, actor=customer,
    )
    met.first_response_due_at = now + timedelta(hours=1)
    met.first_responded_at = now
    met.resolution_due_at = now + timedelta(hours=5)

    breached = crud.create_ticket(
        db_session, customer_id=customer.id, subject="breached", body="b",
        channel=TicketChannel.API, actor=customer,
    )
    breached.first_response_due_at = now - timedelta(hours=1)  # past due, unanswered
    db_session.commit()

    resp = client.get("/reports/sla-compliance", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["first_response_met"] == 1
    assert data["first_response_breached"] == 1
    assert data["first_response_compliance_rate"] == 0.5
    assert data["resolution_pending"] == 1  # met ticket's resolution clock still running


# ---- CSAT report ----


def test_csat_report_aggregates(client, db_session, customer_headers, agent_headers):
    ratings = [5, 4, 5]
    for i, rating in enumerate(ratings):
        t = _create_ticket_via_api(client, customer_headers, f"t{i}")
        _resolve(client, agent_headers, t["id"])
        client.post(f"/tickets/{t['id']}/csat", json={"rating": rating}, headers=customer_headers)

    resp = client.get("/reports/csat", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["responses"] == 3
    assert abs(data["average_rating"] - (14 / 3)) < 1e-6
    assert data["distribution"]["5"] == 2
    assert data["distribution"]["4"] == 1


# ---- agent workload report ----


def test_agent_workload_report(client, db_session, agent_user, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    # put the fixture agent on the team so triage auto-assigns to them
    agent_user.team_id = technical.id
    db_session.commit()

    t1 = _create_ticket_via_api(client, customer_headers, "site is down")
    t2 = _create_ticket_via_api(client, customer_headers, "down again")
    _resolve(client, agent_headers, t2["id"])

    resp = client.get("/reports/agent-workload", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    rows = {r["agent_id"]: r for r in resp.json()["agents"]}
    row = rows[agent_user.id]
    assert row["total_assigned"] == 2
    assert row["resolved_tickets"] == 1
    assert row["open_tickets"] == 1


# ---- triage trend ----


def test_triage_trend_buckets_by_day(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    _create_ticket_via_api(client, customer_headers, "site is down")  # matched
    _create_ticket_via_api(client, customer_headers, "unrelated question")  # unmatched

    resp = client.get("/reports/triage-trend", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    buckets = resp.json()["buckets"]
    assert len(buckets) == 1  # all created "today"
    b = buckets[0]
    assert b["total"] == 2
    assert b["matched"] == 1
    assert b["auto_triage_success_rate"] == 0.5


# ---- CSV export ----


def test_export_tickets_csv(client, customer_headers, agent_headers):
    t = _create_ticket_via_api(client, customer_headers, "exportable")

    resp = client.get("/reports/export/tickets.csv", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["id"] == t["id"]
    assert rows[0]["subject"] == "exportable"
    assert "first_response_sla_status" in rows[0]


def test_export_volume_csv(client, db_session, customer_headers, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    _create_ticket_via_api(client, customer_headers, "site is down")

    resp = client.get("/reports/export/volume.csv", headers=agent_headers)
    assert resp.status_code == 200
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert rows[0] == ["category", "count"]
    assert ["incident", "1"] in rows


def test_export_staff_only(client, customer_headers):
    assert client.get("/reports/export/tickets.csv", headers=customer_headers).status_code == 403


# ---- admin management ----


def test_team_rename(client, db_session, admin_headers, agent_headers):
    team = crud.create_team(db_session, "Old Name")
    resp = client.patch(f"/teams/{team.id}", json={"name": "New Name"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New Name"

    # agents can't rename
    resp = client.patch(f"/teams/{team.id}", json={"name": "Nope"}, headers=agent_headers)
    assert resp.status_code == 403


def test_team_rename_404(client, admin_headers):
    resp = client.patch("/teams/does-not-exist", json={"name": "x"}, headers=admin_headers)
    assert resp.status_code == 404


def test_categories_listing(client, db_session, agent_headers):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    resp = client.get("/admin/categories", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "incident" in data["in_use"]
    assert isinstance(data["configured"], list)
    assert "billing" in data["configured"]  # from default TRIAGE_CATEGORIES_CSV


def test_categories_staff_only(client, customer_headers):
    assert client.get("/admin/categories", headers=customer_headers).status_code == 403
