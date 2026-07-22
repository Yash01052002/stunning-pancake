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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def triage_categories(self) -> list[str]:
        return [c.strip() for c in self.triage_categories_csv.split(",") if c.strip()]


settings = Settings()
