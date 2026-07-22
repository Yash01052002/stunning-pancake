def test_register_creates_customer_account(client):
    resp = client.post(
        "/auth/register",
        json={"email": "new@example.com", "password": "supersecret1", "full_name": "New User"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "customer"
    assert body["email"] == "new@example.com"


def test_register_ignores_client_supplied_role(client):
    resp = client.post(
        "/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": "supersecret1",
            "full_name": "Sneaky",
            "role": "admin",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "customer"


def test_duplicate_email_registration_rejected(client, customer_user):
    resp = client.post(
        "/auth/register",
        json={
            "email": "customer@example.com",
            "password": "supersecret1",
            "full_name": "Duplicate",
        },
    )
    assert resp.status_code == 400


def test_login_success_and_me(client, customer_user, customer_headers):
    resp = client.get("/auth/me", headers=customer_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "customer@example.com"


def test_login_wrong_password_rejected(client, customer_user):
    resp = client.post(
        "/auth/login", json={"email": "customer@example.com", "password": "wrongpass"}
    )
    assert resp.status_code == 401


def test_me_requires_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401
