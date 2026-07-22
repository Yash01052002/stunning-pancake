from datetime import datetime

from pydantic import BaseModel, EmailStr, ConfigDict

from app.models import (
    CustomerTier,
    SentimentLabel,
    TicketChannel,
    TicketPriority,
    TicketStatus,
    TriageMethod,
    TriageOutcome,
    UserRole,
)


# ---- Auth ----


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---- Users ----


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: UserRole = UserRole.CUSTOMER
    tier: CustomerTier | None = None
    team_id: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    role: UserRole
    tier: CustomerTier | None
    team_id: str | None
    active: bool
    created_at: datetime


# ---- Teams ----


class TeamCreate(BaseModel):
    name: str


class TeamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime


# ---- Comments ----


class CommentCreate(BaseModel):
    body: str
    is_internal: bool = False


class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    author_id: str
    body: str
    is_internal: bool
    created_at: datetime


# ---- Ticket events ----


class TicketEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    type: str
    actor_id: str | None
    payload: dict
    created_at: datetime


# ---- Tickets ----


class TicketCreate(BaseModel):
    subject: str
    body: str
    channel: TicketChannel = TicketChannel.API
    customer_id: str | None = None  # agents/admins may create on behalf of a customer


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    category: str | None = None
    priority: TicketPriority | None = None
    assigned_agent_id: str | None = None
    assigned_team_id: str | None = None


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    subject: str
    body: str
    channel: TicketChannel
    category: str | None
    priority: TicketPriority | None
    status: TicketStatus
    assigned_agent_id: str | None
    assigned_team_id: str | None
    confidence_score: float | None
    triage_outcome: TriageOutcome | None
    triage_method: TriageMethod | None
    sentiment: SentimentLabel | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class TicketDetailRead(TicketRead):
    comments: list[CommentRead] = []
    events: list[TicketEventRead] = []


# ---- Triage rules ----


class TriageRuleCreate(BaseModel):
    name: str
    keyword: str
    category: str
    priority: TicketPriority
    team_id: str | None = None
    active: bool = True
    evaluation_order: int = 100


class TriageRuleUpdate(BaseModel):
    name: str | None = None
    keyword: str | None = None
    category: str | None = None
    priority: TicketPriority | None = None
    team_id: str | None = None
    active: bool | None = None
    evaluation_order: int | None = None


class TriageRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    keyword: str
    category: str
    priority: TicketPriority
    team_id: str | None
    active: bool
    evaluation_order: int
    created_at: datetime


# ---- Reports ----


class TriageAccuracyReport(BaseModel):
    total_tickets: int
    matched: int
    unmatched: int
    overridden: int
    coverage_rate: float  # matched / total_tickets
    accuracy_rate: float  # (matched - overridden) / matched
    auto_triage_success_rate: float  # (matched - overridden) / total_tickets
