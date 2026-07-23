"""Phase 5: LLM-drafted suggested replies.

Given the current ticket plus a few similar resolved tickets (how the team
handled comparable issues before), asks Claude to draft a reply an agent can
edit and send. Same graceful-degradation contract as the Phase 3 classifier:
any failure (no API key, network, refusal) returns None, and the
suggested-replies endpoint simply omits the drafted reply rather than erroring
— canned responses are still returned.

The reply is a *draft for an agent to review*, never sent to the customer
automatically.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SimilarTicket:
    subject: str
    body: str
    resolution: str  # the last public agent reply on that resolved ticket


_SYSTEM_PROMPT = """You are a support agent drafting a reply to a customer ticket.

You'll be given the current ticket and, when available, a few similar tickets \
that were already resolved (with how the team replied). Draft a concise, \
friendly, professional reply the agent can review, edit, and send.

Rules:
- Write only the reply body — no subject line, no "Draft:" preamble, no \
  placeholders like [name] unless you genuinely lack the information.
- Ground your reply in the similar resolved tickets when they're relevant; \
  don't invent policies or facts not supported by them or the ticket.
- If you're unsure, ask a clarifying question rather than guessing."""


class ReplyDrafter(Protocol):
    def draft_reply(
        self, subject: str, body: str, similar: list[SimilarTicket]
    ) -> str | None: ...


class AnthropicReplyDrafter:
    def draft_reply(
        self, subject: str, body: str, similar: list[SimilarTicket]
    ) -> str | None:
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic package not installed; skipping reply draft")
            return None

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=settings.llm_model,
                max_tokens=1000,
                thinking={"type": "disabled"},
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_prompt(subject, body, similar)}],
            )
            parts = [block.text for block in response.content if block.type == "text"]
            drafted = "".join(parts).strip()
            return drafted or None
        except Exception:
            logger.warning("Reply drafting failed; omitting draft", exc_info=True)
            return None


def _build_prompt(subject: str, body: str, similar: list[SimilarTicket]) -> str:
    lines = [f"Current ticket:\nSubject: {subject}\nBody: {body}\n"]
    if similar:
        lines.append("\nSimilar resolved tickets:")
        for i, s in enumerate(similar, 1):
            lines.append(
                f"\n[{i}] Subject: {s.subject}\n    Issue: {s.body}\n    How we replied: {s.resolution}"
            )
    else:
        lines.append("\n(No similar resolved tickets found — draft from the ticket alone.)")
    lines.append("\n\nDraft the reply now.")
    return "".join(lines)


_drafter: ReplyDrafter | None = None


def get_drafter() -> ReplyDrafter:
    global _drafter
    if _drafter is None:
        _drafter = AnthropicReplyDrafter()
    return _drafter
