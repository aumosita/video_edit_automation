"""Web client for veauto.

A small FastAPI app that exposes the veauto pipeline over HTTP and
WebSocket. Designed for local use (``localhost``); it does not include
authentication by default.

Public entry points
-------------------

* :func:`create_app` — builds a :class:`fastapi.FastAPI` ready to be
  served with ``uvicorn``.
* :class:`veauto.web.jobs.JobManager` — the asyncio-based job runner
  that the routes talk to.
"""

from __future__ import annotations

from .app import create_app
from .jobs import JobManager, JobRecord, JobStatus

__all__ = [
    "create_app",
    "JobManager",
    "JobRecord",
    "JobStatus",
]
