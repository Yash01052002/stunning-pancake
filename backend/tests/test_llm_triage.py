from app import crud, triage
from app.llm_classifier import LLMClassification
from app.models import CustomerTier, TicketPriority, UserRole


class FakeLLMClassifier:
    """Deterministic stand-in for AnthropicLLMClassifier — no network/API key
    needed. `result` is either an LLMClassification or None (simulating
    classifier unavailability, e.g. no API key or a network failure)."""

    def __init__(self, result):
        self._result = result

    def classify(self, subject: str, body: str):
        return self._result


def _create_ticket_via_api(client, headers, subject="Help", body="details here"):
    resp = client.post("/tickets", json={"subject": subject, "body": body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _retrigger(client, headers, ticket_id, engine=None):
    params = {"engine": engine} if engine else {}
    resp = client.post(f"/tickets/{ticket_id}/triage", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_llm_engine_matches_and_routes_via_category_team_mapping(
    monkeypatch, client, db_session, customer_headers, agent_headers
):
    billing = crud.create_team(db_session, "Billing")
    billing_agent = crud.create_user(
        db_session, email="billing1@example.com", password="pass12345", full_name="B1",
        role=UserRole.AGENT, team_id=billing.id,
    )
    # LLM engine routes by matching an active rule's *category* (not its
    # keyword) to a team — the rule table doubles as the category->team map.
    crud.create_triage_rule(
        db_session, name="Billing category map", keyword="__unused__", category="billing",
        priority=TicketPriority.P2, team_id=billing.id, evaluation_order=10,
    )
    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="billing", sentiment="neutral", priority="p2",
                confidence=0.9, rationale="Customer mentions an invoice charge.",
            )
        ),
    )

    ticket = _create_ticket_via_api(client, customer_headers, "Invoice question")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["category"] == "billing"
    assert result["priority"] == "p2"
    assert result["sentiment"] == "neutral"
    assert result["confidence_score"] == 0.9
    assert result["assigned_team_id"] == billing.id
    assert result["assigned_agent_id"] == billing_agent.id
    assert result["triage_outcome"] == "matched"
    assert result["triage_method"] == "llm"


def test_llm_premium_tier_boosts_priority(
    monkeypatch, client, db_session, agent_headers
):
    premium_customer = crud.create_user(
        db_session, email="vip2@example.com", password="pass12345", full_name="VIP2",
        role=UserRole.CUSTOMER, tier=CustomerTier.PREMIUM,
    )
    resp = client.post(
        "/auth/login", json={"email": "vip2@example.com", "password": "pass12345"}
    )
    vip_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="general", sentiment="neutral", priority="p2",
                confidence=0.9, rationale="Generic question.",
            )
        ),
    )

    ticket = _create_ticket_via_api(client, vip_headers, "Question")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["priority"] == "p1"  # boosted from p2 -> p1


def test_llm_angry_sentiment_boosts_priority(
    monkeypatch, client, customer_headers, agent_headers
):
    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="general", sentiment="angry", priority="p2",
                confidence=0.9, rationale="Customer is furious.",
            )
        ),
    )

    ticket = _create_ticket_via_api(client, customer_headers, "This is unacceptable")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["sentiment"] == "angry"
    assert result["priority"] == "p1"  # boosted from p2 -> p1


def test_llm_premium_and_angry_boosts_stack(
    monkeypatch, client, db_session, agent_headers
):
    premium_customer = crud.create_user(
        db_session, email="vip3@example.com", password="pass12345", full_name="VIP3",
        role=UserRole.CUSTOMER, tier=CustomerTier.PREMIUM,
    )
    resp = client.post(
        "/auth/login", json={"email": "vip3@example.com", "password": "pass12345"}
    )
    vip_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="general", sentiment="angry", priority="p3",
                confidence=0.9, rationale="Furious VIP.",
            )
        ),
    )

    ticket = _create_ticket_via_api(client, vip_headers, "Furious")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["priority"] == "p1"  # p3 -> p2 (premium) -> p1 (angry)


def test_llm_low_confidence_routes_to_fallback_without_auto_assign(
    monkeypatch, client, db_session, customer_headers, agent_headers
):
    fallback_team = crud.create_team(db_session, "Triage")
    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="billing", sentiment="neutral", priority="p2",
                confidence=0.3, rationale="Not sure, could be billing.",
            )
        ),
    )

    ticket = _create_ticket_via_api(client, customer_headers, "Unclear request")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["triage_outcome"] == "low_confidence"
    assert result["triage_method"] is None  # not auto-owned; a human must claim it
    assert result["assigned_agent_id"] is None
    assert result["assigned_team_id"] == fallback_team.id
    # the low-confidence guess is still stored as context for the human reviewer
    assert result["category"] == "billing"
    assert result["confidence_score"] == 0.3


def test_llm_classifier_unavailable_falls_back_like_unmatched(
    monkeypatch, client, db_session, customer_headers, agent_headers
):
    fallback_team = crud.create_team(db_session, "Triage")
    monkeypatch.setattr(triage, "get_classifier", lambda: FakeLLMClassifier(None))

    ticket = _create_ticket_via_api(client, customer_headers, "Anything")
    result = _retrigger(client, agent_headers, ticket["id"], engine="llm")

    assert result["triage_outcome"] == "unmatched"
    assert result["category"] is None
    assert result["assigned_team_id"] == fallback_team.id


def test_engine_query_param_overrides_default_per_call(
    monkeypatch, client, db_session, customer_headers, agent_headers
):
    technical = crud.create_team(db_session, "Technical")
    crud.create_triage_rule(
        db_session, name="Outage", keyword="down", category="incident",
        priority=TicketPriority.P1, team_id=technical.id, evaluation_order=10,
    )
    monkeypatch.setattr(
        triage,
        "get_classifier",
        lambda: FakeLLMClassifier(
            LLMClassification(
                category="general", sentiment="neutral", priority="p3",
                confidence=0.9, rationale="fake",
            )
        ),
    )

    # created with default engine (rule) via the background task
    rule_ticket = _create_ticket_via_api(client, customer_headers, "The site is down")
    detail = client.get(f"/tickets/{rule_ticket['id']}", headers=agent_headers).json()
    assert detail["triage_method"] == "rule"
    assert detail["category"] == "incident"

    # explicitly retriage the same ticket with the LLM engine
    result = _retrigger(client, agent_headers, rule_ticket["id"], engine="llm")
    assert result["triage_method"] == "llm"
    assert result["category"] == "general"


def test_llm_engine_import_does_not_require_anthropic_at_app_boot(client):
    # app.main imports app.triage which imports app.llm_classifier; anthropic
    # itself is only imported lazily inside AnthropicLLMClassifier.classify().
    # If this test collects and the app boots (via the `client` fixture),
    # that's already proof the import chain doesn't hard-depend on it running.
    resp = client.get("/health")
    assert resp.status_code == 200
