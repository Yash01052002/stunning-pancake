"""Phase 4: notification delivery.

In-app notifications are always created — they have no external dependency,
so they're the one channel guaranteed to work. Email and Slack are
best-effort: if unconfigured (no SMTP_HOST / SLACK_WEBHOOK_URL) or the send
fails for any reason, we log and move on rather than raising — a
notification failure must never block the ticket action that triggered it,
the same graceful-degradation pattern as the Phase 3 LLM classifier.
"""

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notification, NotificationType, Ticket, TicketStatus, User

logger = logging.getLogger(__name__)


def create_in_app_notification(
    db: Session, *, user: User, ticket: Ticket | None, type: NotificationType, title: str, message: str
) -> Notification:
    notification = Notification(
        user_id=user.id,
        ticket_id=ticket.id if ticket else None,
        type=type,
        title=title,
        message=message,
    )
    db.add(notification)
    db.flush()
    return notification


def send_email(*, to_address: str, subject: str, body: str) -> bool:
    """Returns True if actually sent, False if skipped (unconfigured) or
    failed. Callers should treat False as a non-fatal no-op."""
    if not settings.smtp_host:
        logger.info("SMTP not configured; skipping email to %s: %s", to_address, subject)
        return False
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from_address
        msg["To"] = to_address
        msg.set_content(body)

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
        return True
    except Exception:
        logger.warning("Email send failed", exc_info=True)
        return False


def send_slack(*, text: str) -> bool:
    if not settings.slack_webhook_url:
        logger.info("Slack webhook not configured; skipping notification: %s", text)
        return False
    try:
        import httpx

        response = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=10)
        response.raise_for_status()
        return True
    except Exception:
        logger.warning("Slack send failed", exc_info=True)
        return False


def notify_customer_ticket_received(db: Session, ticket: Ticket) -> None:
    customer = ticket.customer
    title = "We received your ticket"
    message = (
        f'Hi {customer.full_name}, we received your ticket "{ticket.subject}" '
        f"and will follow up soon."
    )
    create_in_app_notification(
        db, user=customer, ticket=ticket, type=NotificationType.TICKET_RECEIVED,
        title=title, message=message,
    )
    send_email(to_address=customer.email, subject=title, body=message)


_STATUS_NOTIFICATIONS = {
    TicketStatus.OPEN: (
        NotificationType.TICKET_IN_PROGRESS,
        "Your ticket is being worked on",
        'an agent has started working on "{subject}".',
    ),
    TicketStatus.RESOLVED: (
        NotificationType.TICKET_RESOLVED,
        "Your ticket has been resolved",
        'your ticket "{subject}" has been marked resolved.',
    ),
}


def notify_customer_status_change(db: Session, ticket: Ticket, new_status: TicketStatus) -> None:
    """Only fires for the customer-meaningful transitions the master plan
    calls out (received is handled separately at creation time): in
    progress and resolved. Other transitions (pending, closed) are silent —
    add them here if customer-facing updates are wanted for those too."""
    entry = _STATUS_NOTIFICATIONS.get(new_status)
    if entry is None:
        return
    type_, title, body_template = entry
    customer = ticket.customer
    message = f"Hi {customer.full_name}, " + body_template.format(subject=ticket.subject)
    create_in_app_notification(
        db, user=customer, ticket=ticket, type=type_, title=title, message=message
    )
    send_email(to_address=customer.email, subject=title, body=message)


def notify_sla_escalation(db: Session, ticket: Ticket, agent: User | None, reason: str) -> None:
    title = "SLA breach — ticket escalated"
    priority = ticket.priority.value if ticket.priority else "unset"
    message = (
        f'Ticket "{ticket.subject}" breached its {reason} SLA and was escalated '
        f"(priority now {priority})."
    )
    if agent is not None:
        create_in_app_notification(
            db, user=agent, ticket=ticket, type=NotificationType.SLA_ESCALATED,
            title=title, message=message,
        )
        send_email(to_address=agent.email, subject=title, body=message)
    send_slack(text=f":rotating_light: {message} (ticket {ticket.id})")
