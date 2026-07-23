from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./ticketing.db"
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    fallback_triage_team_name: str = "Triage"

    # Phase 3: which engine POST /tickets and the background auto-triage task
    # use by default. "llm" requires ANTHROPIC_API_KEY to be configured;
    # per-ticket overrides are available via POST /tickets/{id}/triage?engine=.
    auto_triage_engine: str = "rule"
    llm_model: str = "claude-sonnet-5"
    llm_confidence_threshold: float = 0.6
    triage_categories_csv: str = "billing,incident,security,account,general"

    # Phase 4: SLA timers & escalation
    sla_at_risk_window_minutes: int = 30
    # notification channels — both are best-effort and log-only when unset,
    # so the app runs with zero external config (see app/notifications.py)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "support@example.com"
    slack_webhook_url: str | None = None

    # Phase 5: agent productivity
    # how recent a presence heartbeat must be to count as an active viewer
    presence_window_seconds: int = 60
    # how many recent resolved tickets in the same category to feed the LLM
    # reply drafter as "here's how we handled similar ones" context
    reply_draft_similar_limit: int = 3

    # Phase 7: abuse protection (per-process fixed-window; see app/rate_limit.py)
    rate_limit_enabled: bool = True
    rate_limit_ticket_create_per_minute: int = 60
    rate_limit_auth_per_minute: int = 20

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def triage_categories(self) -> list[str]:
        return [c.strip() for c in self.triage_categories_csv.split(",") if c.strip()]


settings = Settings()
