import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import DebugTrace
from app.utils.ids import trace_id as make_trace_id
from app.utils.redaction import redact_payload


class TraceRecorder:
    def __init__(self, db: Session, event_type: str, prefix: str = "REQ") -> None:
        self.db = db
        self.trace_id = make_trace_id(prefix)
        self.event_type = event_type
        self.started = time.perf_counter()
        self.timeline: list[dict[str, Any]] = []

    def add(self, message: str, data: dict[str, Any] | None = None) -> None:
        self.timeline.append(
            {
                "at": datetime.utcnow().isoformat(),
                "message": message,
                "data": redact_payload(data or {}),
            }
        )

    def finish(
        self,
        status: str = "success",
        error_summary: str | None = None,
        config_version: int | None = None,
    ) -> DebugTrace:
        duration_ms = int((time.perf_counter() - self.started) * 1000)
        row = DebugTrace(
            trace_id=self.trace_id,
            event_type=self.event_type,
            status=status,
            timeline=self.timeline,
            duration_ms=duration_ms,
            config_version=config_version,
            error_summary=error_summary,
        )
        self.db.add(row)
        self.db.commit()
        return row


@contextmanager
def traced(db: Session, event_type: str, prefix: str = "REQ"):
    recorder = TraceRecorder(db, event_type, prefix)
    try:
        yield recorder
        recorder.finish()
    except Exception as exc:
        recorder.add("operation failed", {"error": str(exc)})
        recorder.finish(status="failed", error_summary=str(exc))
        raise

