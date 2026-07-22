def _create_ticket(client, headers, subject="Cannot log in", body="Getting a 500 error"):
    resp = client.post(
        "/tickets", json={"subject": subject, "body": body}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_customer_can_create_and_view_own_ticket(client, customer_headers):
    ticket = _create_ticket(client, customer_headers)
    assert ticket["status"] == "new"
    assert ticket["channel"] == "api"

    resp = client.get(f"/tickets/{ticket['id']}", headers=customer_headers)
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["subject"] == "Cannot log in"
    # "created" plus the Phase 2 auto-triage pass (no rule matches here, so it's a no-op)
    assert [e["type"] for e in detail["events"]] == ["created", "auto_triaged"]


def test_customer_cannot_view_others_ticket(client, customer_headers, other_customer_headers):
    ticket = _create_ticket(client, customer_headers)
    resp = client.get(f"/tickets/{ticket['id']}", headers=other_customer_headers)
    assert resp.status_code == 403


def test_customer_ticket_list_is_scoped_to_self(
    client, customer_headers, other_customer_headers
):
    _create_ticket(client, customer_headers, subject="A")
    _create_ticket(client, other_customer_headers, subject="B")

    resp = client.get("/tickets", headers=customer_headers)
    assert resp.status_code == 200
    subjects = [t["subject"] for t in resp.json()]
    assert subjects == ["A"]


def test_staff_sees_all_tickets(client, customer_headers, other_customer_headers, agent_headers):
    _create_ticket(client, customer_headers, subject="A")
    _create_ticket(client, other_customer_headers, subject="B")

    resp = client.get("/tickets", headers=agent_headers)
    assert resp.status_code == 200
    subjects = {t["subject"] for t in resp.json()}
    assert subjects == {"A", "B"}


def test_staff_can_create_ticket_on_behalf_of_customer(client, agent_headers, customer_user):
    resp = client.post(
        "/tickets",
        json={"subject": "Phone-in issue", "body": "Called support", "customer_id": customer_user.id},
        headers=agent_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["customer_id"] == customer_user.id


def test_staff_creating_ticket_without_customer_id_fails(client, agent_headers):
    resp = client.post(
        "/tickets", json={"subject": "No customer", "body": "..."}, headers=agent_headers
    )
    assert resp.status_code == 400


def test_customer_cannot_update_ticket(client, customer_headers):
    ticket = _create_ticket(client, customer_headers)
    resp = client.patch(
        f"/tickets/{ticket['id']}", json={"status": "open"}, headers=customer_headers
    )
    assert resp.status_code == 403


def test_agent_can_triage_ticket_and_events_are_logged(client, customer_headers, agent_headers):
    ticket = _create_ticket(client, customer_headers)

    resp = client.patch(
        f"/tickets/{ticket['id']}",
        json={"status": "open", "category": "billing", "priority": "p1"},
        headers=agent_headers,
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["status"] == "open"
    assert updated["category"] == "billing"
    assert updated["priority"] == "p1"

    detail = client.get(f"/tickets/{ticket['id']}", headers=agent_headers).json()
    event_types = [e["type"] for e in detail["events"]]
    assert event_types == ["created", "auto_triaged", "updated"]
    assert detail["events"][2]["payload"]["changes"]["status"] == {"old": "new", "new": "open"}


def test_resolving_ticket_sets_resolved_at(client, customer_headers, agent_headers):
    ticket = _create_ticket(client, customer_headers)
    resp = client.patch(
        f"/tickets/{ticket['id']}", json={"status": "resolved"}, headers=agent_headers
    )
    assert resp.status_code == 200
    assert resp.json()["resolved_at"] is not None


def test_get_nonexistent_ticket_404(client, agent_headers):
    resp = client.get("/tickets/does-not-exist", headers=agent_headers)
    assert resp.status_code == 404
