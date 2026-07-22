from fastapi import FastAPI

from app.routers import (
    auth,
    comments,
    notifications,
    reports,
    sla_policies,
    teams,
    tickets,
    triage_rules,
    users,
)

app = FastAPI(title="Support Ticket System API", version="0.4.0")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(teams.router)
app.include_router(tickets.router)
app.include_router(comments.router)
app.include_router(triage_rules.router)
app.include_router(reports.router)
app.include_router(sla_policies.router)
app.include_router(notifications.router)


@app.get("/health")
def health():
    return {"status": "ok"}
