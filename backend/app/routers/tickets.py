from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud, database, triage
from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_staff
from app.models import Ticket, TicketPriority, TicketStatus, User, UserRole
from app.reply_drafter import SimilarTicket, get_drafter
from app.schemas import (
    BulkActionResult,
    BulkCloseRequest,
    BulkReassignRequest,
    CannedResponseRead,
    CSATSubmit,
    MergeRequest,
    PresenceRead,
    SuggestedReplies,
    TicketCreate,
    TicketDetailRead,
    TicketRead,
    TicketUpdate,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _run_triage_in_background(ticket_id: str, actor_id: str | None) -> None:
    """Runs in a background task with its own DB session — the request's
    session is already torn down by the time background tasks execute."""
    db = database.SessionLocal()
    try:
        ticket = crud.get_ticket(db, ticket_id)
        if ticket is None:
            return
        actor = db.get(User, actor_id) if actor_id else None
        triage.run_auto_triage(db, ticket, actor=actor)
    finally:
        db.close()


def _get_ticket_or_404(db: Session, ticket_id: str) -> Ticket:
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _ensure_can_view(ticket: Ticket, user: User) -> None:
    if user.role == UserRole.CUSTOMER and ticket.customer_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.CUSTOMER:
        customer_id = current_user.id
    else:
        # agents/admins may open a ticket on behalf of a customer
        if not payload.customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is required when staff creates a ticket on behalf of a customer",
            )
        customer_id = payload.customer_id

    ticket = crud.create_ticket(
        db,
        customer_id=customer_id,
        subject=payload.subject,
        body=payload.body,
        channel=payload.channel,
        actor=current_user,
    )
    # auto-triage runs after the response is sent, in its own DB session —
    # the ticket returned here reflects pre-triage state (category/priority/
    # assignment are still null); poll GET /tickets/{id} to see the result.
    background_tasks.add_task(_run_triage_in_background, ticket.id, current_user.id)
    return ticket


@router.get("", response_model=list[TicketRead])
def list_tickets(
    status_filter: TicketStatus | None = None,
    category: str | None = None,
    priority: TicketPriority | None = None,
    assigned_team_id: str | None = None,
    assigned_agent_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer_id = current_user.id if current_user.role == UserRole.CUSTOMER else None
    return crud.list_tickets(
        db,
        customer_id=customer_id,
        status=status_filter,
        category=category,
        priority=priority,
        assigned_team_id=assigned_team_id,
        assigned_agent_id=assigned_agent_id,
    )


@router.get("/{ticket_id}", response_model=TicketDetailRead)
def get_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_can_view(ticket, current_user)
    # customers must not see internal-only comments
    if current_user.role == UserRole.CUSTOMER:
        ticket.comments = [c for c in ticket.comments if not c.is_internal]
    return ticket


@router.patch("/{ticket_id}", response_model=TicketRead)
def update_ticket(
    ticket_id: str,
    payload: TicketUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    updates = payload.model_dump(exclude_unset=True)
    return crud.update_ticket(db, ticket, updates, actor=current_user)


@router.post("/{ticket_id}/csat", response_model=TicketRead)
def submit_csat(
    ticket_id: str,
    payload: CSATSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Customer rates their own resolved/closed ticket (1-5, optional comment).
    Only the ticket's own customer may rate it, and only once it's resolved
    or closed."""
    ticket = _get_ticket_or_404(db, ticket_id)
    if ticket.customer_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only rate your own tickets"
        )
    if ticket.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ticket must be resolved or closed before it can be rated",
        )
    return crud.submit_csat(
        db, ticket, rating=payload.rating, comment=payload.comment, actor=current_user
    )


@router.post("/{ticket_id}/triage", response_model=TicketRead)
def retrigger_triage(
    ticket_id: str,
    engine: Literal["rule", "llm"] | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Manually re-run auto-triage, e.g. after adding/editing triage rules.

    `engine` overrides the configured default for this call only — pass it
    explicitly to A/B-compare the rule engine against the LLM engine on a
    given ticket.
    """
    ticket = _get_ticket_or_404(db, ticket_id)
    return triage.run_auto_triage(db, ticket, actor=current_user, engine=engine)


# ---- Phase 5: agent productivity ----


@router.post("/bulk/close", response_model=BulkActionResult)
def bulk_close(
    payload: BulkCloseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    updated = crud.bulk_update_tickets(
        db, payload.ticket_ids, {"status": TicketStatus.CLOSED}, actor=current_user
    )
    return BulkActionResult(updated_ticket_ids=updated, updated_count=len(updated))


@router.post("/bulk/reassign", response_model=BulkActionResult)
def bulk_reassign(
    payload: BulkReassignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    updates: dict = {}
    if payload.assigned_agent_id is not None:
        updates["assigned_agent_id"] = payload.assigned_agent_id
    if payload.assigned_team_id is not None:
        updates["assigned_team_id"] = payload.assigned_team_id
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide assigned_agent_id and/or assigned_team_id",
        )
    updated = crud.bulk_update_tickets(db, payload.ticket_ids, updates, actor=current_user)
    return BulkActionResult(updated_ticket_ids=updated, updated_count=len(updated))


@router.get("/{ticket_id}/suggested-replies", response_model=SuggestedReplies)
def suggested_replies(
    ticket_id: str,
    draft: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Canned responses relevant to this ticket, plus an optional LLM-drafted
    reply grounded in similar resolved tickets. `draft=false` skips the LLM
    call; when the drafter is unavailable, drafted_reply is simply null."""
    ticket = _get_ticket_or_404(db, ticket_id)
    canned = crud.list_canned_responses(db, category=ticket.category)
    similar = crud.find_similar_resolved_tickets(db, ticket, settings.reply_draft_similar_limit)

    drafted_reply = None
    if draft:
        drafted_reply = get_drafter().draft_reply(
            ticket.subject,
            ticket.body,
            [SimilarTicket(subject=t.subject, body=t.body, resolution=res) for t, res in similar],
        )

    return SuggestedReplies(
        canned=[CannedResponseRead.model_validate(c) for c in canned],
        drafted_reply=drafted_reply,
        similar_ticket_ids=[t.id for t, _ in similar],
    )


@router.post("/{ticket_id}/merge", response_model=TicketRead)
def merge_tickets(
    ticket_id: str,
    payload: MergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Merge the given source tickets INTO this one (marks each source a
    duplicate: sets merged_into_id and closes it)."""
    target = _get_ticket_or_404(db, ticket_id)
    crud.merge_tickets(db, target, payload.source_ticket_ids, actor=current_user)
    return target


@router.post("/{ticket_id}/presence", response_model=list[PresenceRead])
def heartbeat_presence(
    ticket_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Register/refresh this agent's presence on the ticket (collision
    detection heartbeat) and return the OTHER agents currently viewing it."""
    _get_ticket_or_404(db, ticket_id)
    crud.touch_presence(db, ticket_id, current_user.id)
    others = crud.list_active_presence(
        db, ticket_id, settings.presence_window_seconds, exclude_user_id=current_user.id
    )
    return [PresenceRead.model_validate(p) for p in others]


@router.get("/{ticket_id}/presence", response_model=list[PresenceRead])
def list_presence(
    ticket_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Agents currently viewing this ticket (excluding the caller), without
    registering the caller's own presence."""
    _get_ticket_or_404(db, ticket_id)
    others = crud.list_active_presence(
        db, ticket_id, settings.presence_window_seconds, exclude_user_id=current_user.id
    )
    return [PresenceRead.model_validate(p) for p in others]
