from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import crud
from app.models import Ticket, TicketChannel, TicketPresence, TicketStatus, UserRole
from app.routers import tickets as tickets_router


def _create_ticket_via_api(client, headers, subject="Help", body="details"):
    resp = client.post("/tickets", json={"subject": subject, "body": body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- canned responses ----


def test_canned_response_crud_permissions(client, admin_headers, agent_headers, customer_headers):
    resp = client.post(
        "/canned-responses", json={"title": "Greeting", "body": "Hi there"}, headers=agent_headers
    )
    assert resp.status_code == 403

    resp = client.post(
        "/canned-responses", json={"title": "Greeting", "body": "Hi there"}, headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    canned_id = resp.json()["id"]

    assert client.get("/canned-responses", headers=customer_headers).status_code == 403
    assert client.get("/canned-responses", headers=agent_headers).status_code == 200

    resp = client.patch(
        f"/canned-responses/{canned_id}", json={"active": False}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_canned_response_category_filter(client, db_session, agent_headers):
    crud.create_canned_response(db_session, title="Billing macro", body="...", category="billing")
    crud.create_canned_response(db_session, title="Generic", body="...", category=None)

    billing = client.get("/canned-responses?category=billing", headers=agent_headers).json()
    titles = {c["title"] for c in billing}
    assert titles == {"Billing macro", "Generic"}  # null-category applies to all

    incident = client.get("/canned-responses?category=incident", headers=agent_headers).json()
    assert {c["title"] for c in incident} == {"Generic"}


# ---- suggested replies ----


class FakeDrafter:
    def __init__(self):
        self.received_similar = None

    def draft_reply(self, subject, body, similar):
        self.received_similar = similar
        return f"Drafted reply for: {subject} (using {len(similar)} similar)"


def _make_resolved_ticket(db_session, category, subject, agent, reply_body):
    customer = crud.create_user(
        db_session, email=f"c-{subject}@example.com", password="pass12345", full_name="C",
        role=UserRole.CUSTOMER,
    )
    t = crud.create_ticket(
        db_session, customer_id=customer.id, subject=subject, body="old issue",
        channel=TicketChannel.API, actor=customer,
    )
    t.category = category
    db_session.commit()
    crud.add_comment(db_session, t, author=agent, body=reply_body, is_internal=False)
    crud.update_ticket(db_session, t, {"status": TicketStatus.RESOLVED}, actor=agent)
    return t


def test_suggested_replies_returns_canned_and_drafted(
    monkeypatch, client, db_session, agent_user, customer_headers, agent_headers
):
    crud.create_canned_response(db_session, title="Billing macro", body="...", category="billing")
    crud.create_canned_response(db_session, title="Generic", body="...", category=None)

    # a resolved billing ticket with a public agent reply, to be found as "similar"
    _make_resolved_ticket(db_session, "billing", "Old refund", agent_user, "We refunded you.")

    ticket = _create_ticket_via_api(client, customer_headers, "Refund please")
    db_ticket = db_session.get(Ticket, ticket["id"])
    db_ticket.category = "billing"
    db_session.commit()

    fake = FakeDrafter()
    monkeypatch.setattr(tickets_router, "get_drafter", lambda: fake)

    resp = client.get(f"/tickets/{ticket['id']}/suggested-replies", headers=agent_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert {c["title"] for c in data["canned"]} == {"Billing macro", "Generic"}
    assert data["drafted_reply"].startswith("Drafted reply for: Refund please")
    assert len(data["similar_ticket_ids"]) == 1
    # the drafter actually received the similar resolved ticket's agent reply
    assert fake.received_similar[0].resolution == "We refunded you."


def test_suggested_replies_draft_false_skips_llm(
    monkeypatch, client, db_session, customer_headers, agent_headers
):
    called = {"n": 0}

    class Counting:
        def draft_reply(self, *a, **k):
            called["n"] += 1
            return "x"

    monkeypatch.setattr(tickets_router, "get_drafter", lambda: Counting())
    ticket = _create_ticket_via_api(client, customer_headers)

    resp = client.get(
        f"/tickets/{ticket['id']}/suggested-replies?draft=false", headers=agent_headers
    )
    assert resp.status_code == 200
    assert resp.json()["drafted_reply"] is None
    assert called["n"] == 0


def test_suggested_replies_staff_only(client, customer_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    resp = client.get(f"/tickets/{ticket['id']}/suggested-replies", headers=customer_headers)
    assert resp.status_code == 403


# ---- KB articles & deflection ----


def test_kb_crud_permissions(client, admin_headers, agent_headers, customer_headers):
    resp = client.post(
        "/kb-articles",
        json={"title": "Reset password", "body": "Go to settings", "keywords": "password,reset"},
        headers=agent_headers,
    )
    assert resp.status_code == 403

    resp = client.post(
        "/kb-articles",
        json={"title": "Reset password", "body": "Go to settings", "keywords": "password,reset"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text

    assert client.get("/kb-articles", headers=customer_headers).status_code == 403
    assert client.get("/kb-articles", headers=agent_headers).status_code == 200


def test_kb_suggest_ranks_by_keyword_hits(client, db_session, customer_headers):
    crud.create_kb_article(
        db_session, title="Password reset", body="...", keywords="password,login,reset"
    )
    crud.create_kb_article(db_session, title="Billing FAQ", body="...", keywords="invoice,refund")
    crud.create_kb_article(db_session, title="Inactive", body="...", keywords="password", active=False)

    # customer self-service deflection — available to the customer role
    resp = client.post(
        "/kb/suggest",
        json={"subject": "Can't login", "body": "I forgot my password and need to reset it"},
        headers=customer_headers,
    )
    assert resp.status_code == 200, resp.text
    titles = [a["title"] for a in resp.json()]
    assert titles == ["Password reset"]  # billing has 0 hits, inactive excluded


def test_kb_suggest_no_matches_returns_empty(client, db_session, customer_headers):
    crud.create_kb_article(db_session, title="Billing FAQ", body="...", keywords="invoice,refund")
    resp = client.post(
        "/kb/suggest", json={"subject": "hello", "body": "unrelated question"}, headers=customer_headers
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---- bulk actions ----


def test_bulk_close(client, customer_headers, agent_headers):
    t1 = _create_ticket_via_api(client, customer_headers, "one")
    t2 = _create_ticket_via_api(client, customer_headers, "two")

    resp = client.post(
        "/tickets/bulk/close", json={"ticket_ids": [t1["id"], t2["id"], "nonexistent"]},
        headers=agent_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 2  # nonexistent skipped
    for tid in (t1["id"], t2["id"]):
        detail = client.get(f"/tickets/{tid}", headers=agent_headers).json()
        assert detail["status"] == "closed"


def test_bulk_reassign_requires_a_target(client, customer_headers, agent_headers):
    t1 = _create_ticket_via_api(client, customer_headers)
    resp = client.post(
        "/tickets/bulk/reassign", json={"ticket_ids": [t1["id"]]}, headers=agent_headers
    )
    assert resp.status_code == 400


def test_bulk_reassign_to_team(client, db_session, customer_headers, agent_headers):
    team = crud.create_team(db_session, "Escalations")
    t1 = _create_ticket_via_api(client, customer_headers, "a")
    t2 = _create_ticket_via_api(client, customer_headers, "b")

    resp = client.post(
        "/tickets/bulk/reassign",
        json={"ticket_ids": [t1["id"], t2["id"]], "assigned_team_id": team.id},
        headers=agent_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["updated_count"] == 2
    detail = client.get(f"/tickets/{t1['id']}", headers=agent_headers).json()
    assert detail["assigned_team_id"] == team.id
    assert detail["triage_method"] == "manual"  # reassignment is a manual override


def test_bulk_actions_staff_only(client, customer_headers):
    resp = client.post("/tickets/bulk/close", json={"ticket_ids": []}, headers=customer_headers)
    assert resp.status_code == 403


# ---- merge ----


def test_merge_marks_sources_as_duplicates_and_closes_them(
    client, customer_headers, agent_headers
):
    target = _create_ticket_via_api(client, customer_headers, "primary")
    dup1 = _create_ticket_via_api(client, customer_headers, "dup one")
    dup2 = _create_ticket_via_api(client, customer_headers, "dup two")

    resp = client.post(
        f"/tickets/{target['id']}/merge",
        json={"source_ticket_ids": [dup1["id"], dup2["id"]]},
        headers=agent_headers,
    )
    assert resp.status_code == 200, resp.text

    for dup in (dup1, dup2):
        detail = client.get(f"/tickets/{dup['id']}", headers=agent_headers).json()
        assert detail["merged_into_id"] == target["id"]
        assert detail["status"] == "closed"
        assert "merged_into" in [e["type"] for e in detail["events"]]

    target_detail = client.get(f"/tickets/{target['id']}", headers=agent_headers).json()
    assert target_detail["merged_into_id"] is None
    assert [e["type"] for e in target_detail["events"]].count("merge_received") == 2


def test_merge_skips_self_and_already_merged(client, customer_headers, agent_headers):
    target = _create_ticket_via_api(client, customer_headers, "primary")
    dup = _create_ticket_via_api(client, customer_headers, "dup")

    # first merge
    client.post(
        f"/tickets/{target['id']}/merge",
        json={"source_ticket_ids": [dup["id"]]},
        headers=agent_headers,
    )
    # second merge attempt including the target itself and the already-merged dup
    resp = client.post(
        f"/tickets/{target['id']}/merge",
        json={"source_ticket_ids": [target["id"], dup["id"]]},
        headers=agent_headers,
    )
    assert resp.status_code == 200
    detail = client.get(f"/tickets/{dup['id']}", headers=agent_headers).json()
    # still only merged once — no second merged_into event
    assert [e["type"] for e in detail["events"]].count("merged_into") == 1


# ---- @mentions ----


def test_internal_note_mention_notifies_staff(client, db_session, customer_headers, agent_headers):
    # a second agent to be mentioned
    crud.create_user(
        db_session, email="mentioned@example.com", password="pass12345", full_name="Mira Mention",
        role=UserRole.AGENT,
    )
    ticket = _create_ticket_via_api(client, customer_headers)

    resp = client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "hey @mentioned@example.com can you take this?", "is_internal": True},
        headers=agent_headers,
    )
    assert resp.status_code == 201, resp.text

    login = client.post(
        "/auth/login", json={"email": "mentioned@example.com", "password": "pass12345"}
    ).json()
    mentioned_headers = {"Authorization": f"Bearer {login['access_token']}"}
    notifs = client.get("/notifications", headers=mentioned_headers).json()
    assert "mentioned" in [n["type"] for n in notifs]


def test_public_reply_does_not_trigger_mentions(client, db_session, customer_headers, agent_headers):
    crud.create_user(
        db_session, email="mentioned2@example.com", password="pass12345", full_name="M2",
        role=UserRole.AGENT,
    )
    ticket = _create_ticket_via_api(client, customer_headers)
    client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "public note @mentioned2@example.com", "is_internal": False},
        headers=agent_headers,
    )
    login = client.post(
        "/auth/login", json={"email": "mentioned2@example.com", "password": "pass12345"}
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    notifs = client.get("/notifications", headers=headers).json()
    assert "mentioned" not in [n["type"] for n in notifs]


def test_mentioning_a_customer_email_does_not_notify(
    client, db_session, customer_headers, agent_headers, customer_user
):
    # customer_user email is customer@example.com — mentioning it must be ignored
    ticket = _create_ticket_via_api(client, customer_headers)
    client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "internal @customer@example.com", "is_internal": True},
        headers=agent_headers,
    )
    notifs = client.get("/notifications", headers=customer_headers).json()
    assert "mentioned" not in [n["type"] for n in notifs]


# ---- presence / collision detection ----


def test_presence_heartbeat_shows_other_active_agents(
    client, db_session, customer_headers, agent_headers, admin_headers
):
    ticket = _create_ticket_via_api(client, customer_headers)

    # agent heartbeats first — sees nobody else
    resp = client.post(f"/tickets/{ticket['id']}/presence", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # admin heartbeats — now sees the agent
    resp = client.post(f"/tickets/{ticket['id']}/presence", headers=admin_headers)
    assert resp.status_code == 200
    others = resp.json()
    assert len(others) == 1
    assert others[0]["user_id"]  # the agent's id


def test_presence_excludes_stale_heartbeats(
    client, db_session, customer_headers, agent_headers, admin_headers, agent_user
):
    ticket = _create_ticket_via_api(client, customer_headers)
    client.post(f"/tickets/{ticket['id']}/presence", headers=agent_headers)

    # backdate the agent's heartbeat well outside the active window
    row = db_session.scalar(
        select(TicketPresence).where(
            TicketPresence.ticket_id == ticket["id"], TicketPresence.user_id == agent_user.id
        )
    )
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    resp = client.get(f"/tickets/{ticket['id']}/presence", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []  # agent's heartbeat is stale


def test_get_presence_does_not_register_caller(
    client, customer_headers, agent_headers, admin_headers
):
    ticket = _create_ticket_via_api(client, customer_headers)
    # admin only GETs (never heartbeats)
    client.get(f"/tickets/{ticket['id']}/presence", headers=admin_headers)
    # agent heartbeats and should NOT see admin (admin never registered presence)
    resp = client.post(f"/tickets/{ticket['id']}/presence", headers=agent_headers)
    assert resp.json() == []


def test_presence_staff_only(client, customer_headers):
    ticket = _create_ticket_via_api(client, customer_headers)
    assert client.post(f"/tickets/{ticket['id']}/presence", headers=customer_headers).status_code == 403
    assert client.get(f"/tickets/{ticket['id']}/presence", headers=customer_headers).status_code == 403
