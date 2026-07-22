def test_admin_can_create_agent(client, admin_headers):
    resp = client.post(
        "/users",
        json={
            "email": "newagent@example.com",
            "password": "agentpass123",
            "full_name": "New Agent",
            "role": "agent",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "agent"


def test_agent_cannot_create_users(client, agent_headers):
    resp = client.post(
        "/users",
        json={
            "email": "x@example.com",
            "password": "pass12345",
            "full_name": "X",
            "role": "agent",
        },
        headers=agent_headers,
    )
    assert resp.status_code == 403


def test_customer_cannot_list_users(client, customer_headers):
    resp = client.get("/users", headers=customer_headers)
    assert resp.status_code == 403


def test_agent_can_list_users(client, agent_headers, agent_user):
    resp = client.get("/users", headers=agent_headers)
    assert resp.status_code == 200


def test_admin_can_create_team_and_staff_can_list(client, admin_headers, agent_headers):
    resp = client.post("/teams", json={"name": "Billing"}, headers=admin_headers)
    assert resp.status_code == 201, resp.text

    resp = client.get("/teams", headers=agent_headers)
    assert resp.status_code == 200
    assert any(t["name"] == "Billing" for t in resp.json())


def test_agent_cannot_create_team(client, agent_headers):
    resp = client.post("/teams", json={"name": "Nope"}, headers=agent_headers)
    assert resp.status_code == 403
