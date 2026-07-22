import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    AGENT = "agent"
    CUSTOMER = "customer"


class CustomerTier(str, enum.Enum):
    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"


class TicketStatus(str, enum.Enum):
    NEW = "new"
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, enum.Enum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class TicketChannel(str, enum.Enum):
    WEB = "web"
    EMAIL = "email"
    API = "api"
    WIDGET = "widget"


class TriageOutcome(str, enum.Enum):
    """Immutable record of what the triage engine produced at creation time."""

    MATCHED = "matched"
    LOW_CONFIDENCE = "low_confidence"  # LLM classified it, but below the confidence threshold
    UNMATCHED = "unmatched"


class TriageMethod(str, enum.Enum):
    """Who currently owns this ticket's category/priority/assignment.

    Starts as RULE or LLM when the corresponding engine successfully
    classifies the ticket, flips to MANUAL the first time a staff member
    edits those fields — this is the override signal the accuracy metric
    is built on.
    """

    RULE = "rule"
    LLM = "llm"
    MANUAL = "manual"


class SentimentLabel(str, enum.Enum):
    """LLM-detected customer sentiment (Phase 3). Only ANGRY affects priority scoring."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    ANGRY = "angry"


_PRIORITY_ORDER = [TicketPriority.P0, TicketPriority.P1, TicketPriority.P2, TicketPriority.P3]


def bump_priority(priority: TicketPriority) -> TicketPriority:
    """One level more urgent, capped at P0. Shared by triage (tier/sentiment
    boosts) and SLA escalation (breach boost) so both use one ordering."""
    idx = _PRIORITY_ORDER.index(priority)
    return _PRIORITY_ORDER[max(idx - 1, 0)]


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    members: Mapped[list["User"]] = relationship(back_populates="team")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), nullable=False)
    tier: Mapped[CustomerTier | None] = mapped_column(
        Enum(CustomerTier, native_enum=False), nullable=True
    )
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    team: Mapped[Team | None] = relationship(back_populates="members")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[TicketChannel] = mapped_column(
        Enum(TicketChannel, native_enum=False), default=TicketChannel.API
    )
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[TicketPriority | None] = mapped_column(
        Enum(TicketPriority, native_enum=False), nullable=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, native_enum=False), default=TicketStatus.NEW
    )
    assigned_agent_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    triage_outcome: Mapped[TriageOutcome | None] = mapped_column(
        Enum(TriageOutcome, native_enum=False), nullable=True
    )
    triage_method: Mapped[TriageMethod | None] = mapped_column(
        Enum(TriageMethod, native_enum=False), nullable=True
    )
    sentiment: Mapped[SentimentLabel | None] = mapped_column(
        Enum(SentimentLabel, native_enum=False), nullable=True
    )
    first_response_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id])
    assigned_agent: Mapped[User | None] = relationship(foreign_keys=[assigned_agent_id])
    assigned_team: Mapped[Team | None] = relationship(foreign_keys=[assigned_team_id])
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    events: Mapped[list["TicketEvent"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketEvent.created_at"
    )

    @property
    def first_response_sla_status(self) -> str | None:
        return _sla_status(self.first_response_due_at, self.first_responded_at)

    @property
    def resolution_sla_status(self) -> str | None:
        return _sla_status(self.resolution_due_at, self.resolved_at)


def as_aware_utc(dt: datetime) -> datetime:
    """SQLite (unlike Postgres) drops tzinfo on round-trip even for
    DateTime(timezone=True) columns. Every datetime this app stores is
    produced by _now() (UTC), so a naive value read back is always UTC —
    reattach it before comparing, or naive/aware comparison raises."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _sla_status(due_at: datetime | None, satisfied_at: datetime | None) -> str | None:
    """Computed fresh on every read (never cached), so it's always accurate
    without needing a background job to keep it up to date:
    - None: no SLA target set (priority not yet triaged)
    - met / breached_late: the clock already stopped (responded/resolved)
    - on_track / at_risk / breached: the clock is still running
    """
    if due_at is None:
        return None
    due_at = as_aware_utc(due_at)
    if satisfied_at is not None:
        return "met" if as_aware_utc(satisfied_at) <= due_at else "breached_late"
    now = datetime.now(timezone.utc)
    if now > due_at:
        return "breached"
    if now > due_at - timedelta(minutes=settings.sla_at_risk_window_minutes):
        return "at_risk"
    return "on_track"


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    ticket: Mapped[Ticket] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    ticket: Mapped[Ticket] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship()


class TriageRule(Base):
    """v1 rule: case-insensitive keyword match against subject+body.

    Plain substring matching (not regex) is a deliberate choice — admin-supplied
    regex patterns risk ReDoS, and keyword matching is sufficient for v1's
    "refund" / "down" / "security" style rules described in the master plan.
    """

    __tablename__ = "triage_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    keyword: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, native_enum=False), nullable=False
    )
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    evaluation_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    team: Mapped[Team | None] = relationship()


class SLAPolicy(Base):
    """Time-to-first-response / time-to-resolution targets, in minutes.

    Looked up by (priority, tier) with tier as the more specific match:
    a policy with tier set wins over a tier=null policy for the same
    priority, so you can have a general P1 policy plus a tighter P1 policy
    just for premium customers.
    """

    __tablename__ = "sla_policies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, native_enum=False), nullable=False
    )
    tier: Mapped[CustomerTier | None] = mapped_column(
        Enum(CustomerTier, native_enum=False), nullable=True
    )
    first_response_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NotificationType(str, enum.Enum):
    TICKET_RECEIVED = "ticket_received"
    TICKET_IN_PROGRESS = "ticket_in_progress"
    TICKET_RESOLVED = "ticket_resolved"
    SLA_AT_RISK = "sla_at_risk"
    SLA_ESCALATED = "sla_escalated"


class Notification(Base):
    """In-app notification. Always created regardless of whether email/Slack
    are configured — this is the one channel that has no external
    dependency, so it's the reliable fallback for "did anyone get told?"."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, native_enum=False), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    ticket: Mapped[Ticket | None] = relationship(foreign_keys=[ticket_id])
