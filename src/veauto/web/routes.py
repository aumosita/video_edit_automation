"""HTTP and WebSocket routes for the veauto web app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse

from .jobs import JobManager
from .schemas import JobCreateResponse, JobOptions, JobRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _manager() -> JobManager:
    """Return the attached :class:`JobManager`, raising 500 if missing."""
    from . import app as _app  # local import to avoid cycle
    if _app._job_manager is None:  # type: ignore[attr-defined]
        raise HTTPException(status_code=500, detail="JobManager not configured")
    return _app._job_manager  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.post("/jobs", response_model=JobCreateResponse, status_code=201)
async def create_job(
    file: UploadFile = File(...),
    options: str = Query(
        "{}",
        description=(
            "JSON-encoded JobOptions. Same fields as `veauto run` CLI."
        ),
    ),
) -> JobCreateResponse:
    """Upload a video and start a new job."""
    from . import app as _app  # local import
    mgr = _app._job_manager  # type: ignore[attr-defined]
    if mgr is None:
        raise HTTPException(status_code=500, detail="JobManager not configured")

    try:
        opts = JobOptions.model_validate_json(options)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail=f"Invalid options JSON: {exc}",
        ) from exc

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    job_id = f"tmp_{os.urandom(4).hex()}"
    job_dir = mgr.output_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload.bin").name
    save_path = job_dir / safe_name
    save_path.write_bytes(content)

    record = mgr.submit(
        input_path=save_path,
        input_name=safe_name,
        input_size=len(content),
        options=opts,
        loop=asyncio.get_running_loop(),
    )
    return JobCreateResponse(id=record.id, status=record.status)


@router.get("/jobs", response_model=list[JobRecord])
async def list_jobs() -> list[JobRecord]:
    mgr = _manager()
    return [r.with_download_urls() for r in mgr.list_jobs()]


@router.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    mgr = _manager()
    rec = mgr.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return rec.with_download_urls()


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(job_id: str) -> None:
    """Cancel a running job and remove it from the manager.

    A single DELETE call covers both operations: the worker thread is
    signalled to stop (``cancel_event.set``) and the job is popped from
    the in-memory map so subsequent ``GET /api/jobs`` calls won't
    resurrect it. The endpoint is idempotent — calling DELETE on an
    unknown id returns 204.
    """
    mgr = _manager()
    mgr.delete(job_id)  # idempotent: returns False if already gone
    return None


# ---------------------------------------------------------------------------
# Artefact downloads
# ---------------------------------------------------------------------------


def _safe_under(base: Path, candidate: Path) -> Path:
    """Ensure ``candidate`` is inside ``base`` (no path traversal)."""
    try:
        candidate = candidate.resolve()
        base = base.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not str(candidate).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    return candidate


@router.get("/jobs/{job_id}/download/{name}")
async def download_artefact(job_id: str, name: str) -> FileResponse:
    mgr = _manager()
    job_dir = mgr.job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    target = _safe_under(job_dir, job_dir / name)
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"Artefact not found: {name}"
        )
    media_type = "application/octet-stream"
    if name.endswith(".fcpxml"):
        media_type = "application/xml"
    elif name.endswith(".json"):
        media_type = "application/json"
    elif name.endswith(".md"):
        media_type = "text/markdown"
    return FileResponse(target, media_type=media_type, filename=name)


# ---------------------------------------------------------------------------
# Health / info
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "veauto-web", "version": _version()}


def _version() -> str:
    from . import app as _app  # local
    return _app._version  # type: ignore[attr-defined]


@router.get("/config/defaults")
async def get_default_options() -> dict:
    return JobOptions().model_dump(mode="json")


@router.get("/config/models")
async def list_models() -> dict:
    return {
        "models": [
            "tiny",
            "base",
            "small",
            "medium",
            "large-v3",
            "distil-large-v3",
        ]
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@router.websocket("/ws/jobs/{job_id}")
async def ws_jobs(websocket: WebSocket, job_id: str) -> None:
    mgr = _manager()
    rec = mgr.get(job_id)
    if rec is None:
        await websocket.close(code=4404, reason=f"Job not found: {job_id}")
        return

    await websocket.accept()
    sub = mgr.subscribe(job_id)
    if sub is None:
        await websocket.close(code=4404, reason="Job not found")
        return

    # Send current state immediately
    try:
        rec_now = mgr.get(job_id)
        if rec_now is not None:
            await websocket.send_json(
                {
                    "type": "state",
                    "job": rec_now.with_download_urls().model_dump(mode="json"),
                }
            )
    except Exception:  # noqa: BLE001
        logger.exception("Initial state send failed")
        mgr.unsubscribe(job_id, sub)
        return

    async def _reader() -> None:
        """Read client messages (cancel, ping)."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "cancel":
                    mgr.cancel(job_id)
                    await websocket.send_json(
                        {"type": "ack", "action": "cancel"}
                    )
                elif mtype == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            logger.exception("WS reader failed")
            return

    async def _writer() -> None:
        """Forward queue messages to the WebSocket."""
        try:
            while True:
                msg = await sub.queue.get()
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            logger.exception("WS writer failed")
            return
        finally:
            mgr.unsubscribe(job_id, sub)

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    finally:
        mgr.unsubscribe(job_id, sub)


# ---------------------------------------------------------------------------
# Global event stream
# ---------------------------------------------------------------------------


@router.websocket("/ws")
async def ws_global(websocket: WebSocket) -> None:
    """Push every job's lifecycle events (create / update / delete).

    Used by the SPA to keep the job table in sync without polling
    ``GET /api/jobs``. Pairs with :meth:`JobManager.subscribe_all`.

    Local-only security: we accept any ``Origin`` that points at
    ``localhost`` or ``127.0.0.1`` (any port). Reject anything else
    with ``1008`` (policy violation) so a browser opened against
    a remote host cannot subscribe to the local job stream.
    """
    if not _is_local_origin(websocket):
        await websocket.close(code=1008, reason="origin not allowed")
        return

    mgr = _manager()
    await websocket.accept()

    sub = mgr.subscribe_all()
    # The broadcast path uses call_soon_threadsafe on this loop; the
    # worker thread writes to a queue that lives on this loop's queue.
    sub.loop = asyncio.get_running_loop()  # type: ignore[attr-defined]

    # Send a snapshot of all current jobs so the UI can render on connect.
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "jobs": [
                    r.with_download_urls().model_dump(mode="json")
                    for r in mgr.list_jobs()
                ],
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("Initial snapshot send failed")
        mgr.unsubscribe_all(sub)
        await websocket.close()
        return

    async def _reader() -> None:
        """Read client messages (ping only)."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            logger.exception("WS global reader failed")
            return

    async def _writer() -> None:
        """Forward queue messages to the WebSocket."""
        try:
            while True:
                msg = await sub.queue.get()
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            logger.exception("WS global writer failed")
            return
        finally:
            mgr.unsubscribe_all(sub)

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    finally:
        mgr.unsubscribe_all(sub)


def _is_local_origin(websocket: WebSocket) -> bool:
    """Allow only ``localhost`` / ``127.0.0.1`` (any port) for WS."""
    origin = websocket.headers.get("origin") or websocket.headers.get("Origin")
    if not origin:
        # No Origin header: the server is bound to 127.0.0.1 so only
        # local clients can connect; we let them in.
        host = websocket.headers.get("host") or ""
        return host.startswith("localhost") or host.startswith("127.0.0.1")
    # Accept http://localhost[:port] and http://127.0.0.1[:port]
    lowered = origin.lower().strip()
    for prefix in ("http://localhost", "ws://localhost", "https://localhost",
                   "http://127.0.0.1", "ws://127.0.0.1", "https://127.0.0.1"):
        if lowered == prefix or lowered.startswith(prefix + ":"):
            return True
    return False
