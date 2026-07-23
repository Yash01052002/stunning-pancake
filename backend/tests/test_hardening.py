from app import rate_limit
from app.config import settings
from app.pii import redact
from app.reply_drafter import SimilarTicket, _build_prompt


# ---- PII redaction ----


def test_redact_masks_email_phone_card_ssn():
    text = "email jane.doe@x.com phone 555-123-4567 card 4111 1111 1111 1111 ssn 123-45-6789"
    out = redact(text)
    assert "jane.doe@x.com" not in out
    assert "[EMAIL]" in out
    assert "[PHONE]" in out
    assert "[CARD]" in out
    assert "[SSN]" in out


def test_redact_leaves_ordinary_text_untouched():
    assert redact("the site is down and nothing loads") == "the site is down and nothing loads"


def test_redact_handles_empty():
    assert redact("") == ""


def test_reply_drafter_prompt_redacts_and_fences_untrusted_content():
    prompt = _build_prompt(
        "Refund for jane@x.com",
        "call me at 555-123-4567",
        [SimilarTicket("old ticket", "issue from bob@y.com", "we fixed it")],
    )
    # PII masked everywhere, including in the similar-ticket context
    assert "jane@x.com" not in prompt
    assert "bob@y.com" not in prompt
    assert "555-123-4567" not in prompt
    # untrusted content is fenced and labeled
    assert "untrusted" in prompt.lower()


# ---- rate limiting ----


def test_login_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    rate_limit.reset()

    # 3 allowed (even though creds are bad → 401), 4th is rate-limited
    for _ in range(3):
        resp = client.post("/auth/login", json={"email": "x@y.com", "password": "nope"})
        assert resp.status_code == 401
    resp = client.post("/auth/login", json={"email": "x@y.com", "password": "nope"})
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers}


def test_ticket_create_rate_limited(client, customer_headers, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_ticket_create_per_minute", 2)
    rate_limit.reset()

    for i in range(2):
        resp = client.post("/tickets", json={"subject": f"t{i}", "body": "b"}, headers=customer_headers)
        assert resp.status_code == 201
    resp = client.post("/tickets", json={"subject": "over", "body": "b"}, headers=customer_headers)
    assert resp.status_code == 429


def test_rate_limit_disabled_by_default(client, customer_headers):
    # the autouse conftest fixture disables limiting; many creates are fine
    for i in range(10):
        resp = client.post("/tickets", json={"subject": f"t{i}", "body": "b"}, headers=customer_headers)
        assert resp.status_code == 201


# ---- audit logging ----


def test_mutating_request_is_audited(client, customer_headers, admin_headers, customer_user):
    ticket = client.post(
        "/tickets", json={"subject": "audited", "body": "b"}, headers=customer_headers
    ).json()

    logs = client.get("/admin/audit-logs", headers=admin_headers).json()
    ticket_creates = [
        entry for entry in logs if entry["method"] == "POST" and entry["path"] == "/tickets"
    ]
    assert ticket_creates
    entry = ticket_creates[0]
    assert entry["status_code"] == 201
    assert entry["actor_id"] == customer_user.id  # decoded from the bearer token


def test_failed_login_is_audited(client, admin_headers):
    client.post("/auth/login", json={"email": "ghost@x.com", "password": "bad"})
    logs = client.get("/admin/audit-logs", headers=admin_headers).json()
    login_attempts = [e for e in logs if e["path"] == "/auth/login"]
    assert login_attempts
    assert login_attempts[0]["status_code"] == 401
    assert login_attempts[0]["actor_id"] is None  # no valid bearer on a failed login


def test_get_requests_are_not_audited(client, admin_headers):
    # a GET should not create audit rows; only mutations are recorded
    client.get("/reports/volume", headers=admin_headers)
    logs = client.get("/admin/audit-logs", headers=admin_headers).json()
    assert all(e["method"] != "GET" for e in logs)


def test_audit_logs_admin_only(client, agent_headers, customer_headers):
    assert client.get("/admin/audit-logs", headers=agent_headers).status_code == 403
    assert client.get("/admin/audit-logs", headers=customer_headers).status_code == 403
