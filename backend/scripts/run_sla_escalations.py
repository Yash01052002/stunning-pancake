"""Run the SLA breach escalation check once and exit — intended to be
invoked on a schedule (host crontab, k8s CronJob, etc.), since this project
has no in-process scheduler (see app/sla.py module docstring for why).

Run: python -m scripts.run_sla_escalations
"""
from app.database import SessionLocal
from app.sla import check_and_escalate_slas


def run() -> None:
    db = SessionLocal()
    try:
        result = check_and_escalate_slas(db)
        print(f"Checked at {result['checked_at']}: escalated {result['escalated_count']} ticket(s)")
        for ticket_id in result["escalated_ticket_ids"]:
            print(f"  - {ticket_id}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
