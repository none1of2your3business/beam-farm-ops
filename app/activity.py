"""Activity audit helper."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ActivityLog


def log_activity(db: Session, username: str | None, action: str, detail: str | None = None) -> None:
    db.add(
        ActivityLog(
            username=(username or "unknown")[:64],
            action=action[:120],
            detail=(detail or "")[:2000] or None,
        )
    )
