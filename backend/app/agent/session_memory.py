"""Notes that live only for the current agent thread.

A run keeps the reviewed notebook, the plan, and operator comments until the
copy finishes, the operator declines, or the chat thread ends. Nothing here
is written to disk.
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_RUNS: dict[str, dict[str, Any]] = {}
_BY_THREAD: dict[str, set[str]] = {}
_THREAD: ContextVar[str] = ContextVar("dataflow_thread", default="")


def bind_thread(thread_id: str) -> None:
    _THREAD.set(thread_id or "")


def current_thread() -> str:
    return _THREAD.get()


def open_run(**fields: Any) -> dict[str, Any]:
    """Start a short-term record for the notebook that is waiting on review."""
    review_id = uuid.uuid4().hex
    thread_id = current_thread()
    record = {
        "review_id": review_id,
        "thread_id": thread_id,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "comments": [],
        **fields,
    }
    with _LOCK:
        _RUNS[review_id] = record
        _BY_THREAD.setdefault(thread_id, set()).add(review_id)
    return dict(record)


def get_run(review_id: str) -> dict[str, Any] | None:
    with _LOCK:
        record = _RUNS.get(review_id)
        return dict(record) if record else None


def add_comment(review_id: str, comment: str) -> None:
    text = " ".join(str(comment or "").split())
    if not text:
        return
    with _LOCK:
        record = _RUNS.get(review_id)
        if record is None:
            return
        comments = list(record.get("comments") or [])
        comments.append(text[:500])
        record["comments"] = comments[-8:]


def close_run(review_id: str) -> None:
    if not review_id:
        return
    with _LOCK:
        record = _RUNS.pop(review_id, None)
        if record is None:
            return
        thread_id = str(record.get("thread_id") or "")
        bucket = _BY_THREAD.get(thread_id)
        if bucket is not None:
            bucket.discard(review_id)
            if not bucket:
                _BY_THREAD.pop(thread_id, None)


def close_thread(thread_id: str) -> None:
    """Drop every note for this chat thread. Called when the session finishes."""
    with _LOCK:
        for review_id in list(_BY_THREAD.pop(thread_id or "", set())):
            _RUNS.pop(review_id, None)
