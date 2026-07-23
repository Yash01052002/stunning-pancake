"""Phase 7: PII redaction before anything leaves for an external LLM.

The master plan calls out "redact/mask PII before sending to any external
model provider" and "injection risks in triage prompts". Both the Phase 3
classifier and Phase 5 reply drafter send raw customer ticket text to
Anthropic — this module masks the common high-risk identifiers first.

This is defense-in-depth, not a guarantee: regex PII detection has false
negatives (unusual formats) and false positives. It removes the obvious,
high-volume leaks (emails, phone numbers, card/SSN-shaped numbers) so they
don't land in prompts, logs, or the provider's request history. Category
classification and reply drafting don't need the literal identifiers to work.
"""

import re

# Order matters: email before phone (an email can contain digit runs), and the
# long-digit / card patterns before the shorter phone pattern.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 13-16 digit runs allowing spaces/dashes (credit-card shaped)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")
# US-style SSN
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# phone: 10+ digits with common separators / country code
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")

_REPLACEMENTS = [
    (_EMAIL_RE, "[EMAIL]"),
    (_SSN_RE, "[SSN]"),
    (_CARD_RE, "[CARD]"),
    (_PHONE_RE, "[PHONE]"),
]


def redact(text: str) -> str:
    """Return `text` with common PII masked by placeholder tokens."""
    if not text:
        return text
    for pattern, replacement in _REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text
