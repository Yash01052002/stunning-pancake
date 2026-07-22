from fastapi import FastAPI

from app.routers import auth, comments, reports, teams, tickets, triage_rules, users

app = FastAPI(title="Support Ticket System API", version="0.2.0")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(teams.router)
app.include_router(tickets.router)
app.include_router(comments.router)
app.include_router(triage_rules.router)
app.include_router(reports.router)


@app.get("/health")
def health():
    return {"status": "ok"}
