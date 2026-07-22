def _create_ticket(client, headers):
    resp = client.post(
        "/tickets", json={"subject": "Help", "body": "Something is broken"}, headers=headers
    )
    assert resp.status_code == 201
    return resp.json()


def test_customer_can_comment_on_own_ticket(client, customer_headers):
    ticket = _create_ticket(client, customer_headers)
    resp = client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "Any update?"},
        headers=customer_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_internal"] is False


def test_customer_cannot_post_internal_note(client, customer_headers):
    ticket = _create_ticket(client, customer_headers)
    resp = client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "internal thought", "is_internal": True},
        headers=customer_headers,
    )
    assert resp.status_code == 403


def test_customer_cannot_comment_on_others_ticket(
    client, customer_headers, other_customer_headers
):
    ticket = _create_ticket(client, customer_headers)
    resp = client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "trying to snoop"},
        headers=other_customer_headers,
    )
    assert resp.status_code == 403


def test_internal_notes_hidden_from_customer(client, customer_headers, agent_headers):
    ticket = _create_ticket(client, customer_headers)
    client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "public reply"},
        headers=agent_headers,
    )
    client.post(
        f"/tickets/{ticket['id']}/comments",
        json={"body": "internal note for team", "is_internal": True},
        headers=agent_headers,
    )

    resp = client.get(f"/tickets/{ticket['id']}/comments", headers=customer_headers)
    assert resp.status_code == 200
    bodies = [c["body"] for c in resp.json()]
    assert bodies == ["public reply"]

    resp = client.get(f"/tickets/{ticket['id']}/comments", headers=agent_headers)
    assert len(resp.json()) == 2


def test_comment_creates_ticket_event(client, customer_headers):
    ticket = _create_ticket(client, customer_headers)
    client.post(
        f"/tickets/{ticket['id']}/comments", json={"body": "hello"}, headers=customer_headers
    )
    detail = client.get(f"/tickets/{ticket['id']}", headers=customer_headers).json()
    assert [e["type"] for e in detail["events"]] == ["created", "commented"]
