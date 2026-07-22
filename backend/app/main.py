from fastapi import FastAPI

from app.routers import auth, comments, teams, tickets, users

app = FastAPI(title="Support Ticket System API", version="0.1.0")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(teams.router)
app.include_router(tickets.router)
app.include_router(comments.router)


@app.get("/health")
def health():
    return {"status": "ok"}
