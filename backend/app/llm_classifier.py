"""Phase 3: LLM-based ticket classification.

Uses Claude's structured-outputs (`client.messages.parse`) to get a
schema-validated classification instead of hand-parsing free text — see
docs/support-ticket-system-master-plan.md, Phase 3.

Classification failure (missing API key, network error, refusal, whatever)
is treated as a normal outcome, not an exception the caller must handle:
`classify()` returns None and app.triage routes the ticket to the human
fallback queue, the same way an unmatched rule does in Phase 2.
"""

import logging
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)


class LLMClassification(BaseModel):
    category: str = Field(description="Best-fit support category for this ticket")
    sentiment: Literal["positive", "neutral", "negative", "angry"]
    priority: Literal["p0", "p1", "p2", "p3"]
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this classification")
    rationale: str = Field(description="One sentence explaining the classification")


_SYSTEM_PROMPT = """You are a support-ticket triage classifier for a software company.

Given a ticket's subject and body, classify it into exactly one of these categories:
{categories}

Also assess:
- sentiment: the customer's emotional tone (positive, neutral, negative, or angry —
  use "angry" only for clear frustration/anger, not just a negative report)
- priority: p0 (critical/outage/security), p1 (high), p2 (medium), p3 (low)
- confidence: how confident you are in this classification, from 0.0 to 1.0
- rationale: one sentence explaining your reasoning

If the ticket doesn't clearly fit any category, use "general" and lower your
confidence accordingly rather than forcing a specific category."""


class LLMClassifier(Protocol):
    def classify(self, subject: str, body: str) -> LLMClassification | None: ...


class AnthropicLLMClassifier:
    """Calls the Anthropic API. Any failure (no API key, network, refusal,
    schema mismatch) is caught and reported as "no classification" rather
    than raised — the triage engine treats that identically to an unmatched
    rule and routes to the human fallback queue."""

    def classify(self, subject: str, body: str) -> LLMClassification | None:
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic package not installed; skipping LLM triage")
            return None

        try:
            client = anthropic.Anthropic()
            response = client.messages.parse(
                model=settings.llm_model,
                max_tokens=500,
                thinking={"type": "disabled"},  # fast, deterministic classification
                system=_SYSTEM_PROMPT.format(categories=", ".join(settings.triage_categories)),
                messages=[
                    {"role": "user", "content": f"Subject: {subject}\n\nBody: {body}"}
                ],
                output_format=LLMClassification,
            )
            return response.parsed_output
        except Exception:
            logger.warning("LLM classification failed; falling back", exc_info=True)
            return None


_classifier: LLMClassifier | None = None


def get_classifier() -> LLMClassifier:
    global _classifier
    if _classifier is None:
        _classifier = AnthropicLLMClassifier()
    return _classifier
