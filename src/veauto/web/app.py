"""FastAPI app factory for veauto.

The factory attaches a :class:`JobManager` to the module-level
``_job_manager`` slot, registers the API routes, and (optionally) mounts
the static SPA bundle as the catch-all root.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .jobs import JobManager

logger = logging.getLogger(__name__)

# Module-level slot. The route handlers read from this; tests can
# override it before invoking the app.
_job_manager: JobManager | None = None
_version: str = __version__


def create_app(
    *,
    output_root: Path,
    max_workers: int = 2,
    static_dir: Path | None = None,
    on_job_done: Any = None,
    allow_origins: list[str] | str = "auto",
) -> FastAPI:
    """Create a configured FastAPI app.

    Parameters
    ----------
    output_root:
        Directory where uploaded videos and produced FCPXML / report
        files are written. Created if it does not exist.
    max_workers:
        Number of jobs that may run in parallel.
    static_dir:
        Optional path to the built Svelte SPA. If provided, the app
        serves it at ``/`` and returns ``index.html`` for unknown paths.
    allow_origins:
        List of origins to permit for CORS / WebSocket. The special
        value ``"auto"`` (default) allows any ``http://localhost:*``
        and ``http://127.0.0.1:*``. Use ``"*"`` to allow any origin
        (local-only; do not expose to the network).
    """
    global _job_manager
    _job_manager = JobManager(
        output_root=Path(output_root),
        max_workers=max_workers,
        on_job_done=on_job_done,
    )

    app = FastAPI(
        title="veauto web",
        version=__version__,
        description=(
            "Local web client for veauto — silence removal + auto subtitles."
        ),
    )

    # CORS — required because the SPA may be served from one origin
    # (e.g. http://localhost:8765) while the API is at another
    # (http://127.0.0.1:8765). Uvicorn's built-in WS origin check also
    # rejects handshakes that don't match the bound host.
    #
    # We allow common local origins (any port) plus the explicit
    # ``http://localhost:<port>`` / ``http://127.0.0.1:<port>`` variants
    # so that the WebSocket handshake succeeds in both directions.
    # ``ws://`` schemes are added for completeness (CORS itself does
    # not apply to WebSockets, but uvicorn's ``ws_origins`` does
    # accept them and is permissive about port matching).
    if allow_origins == "auto":
        origins: list[str] = [
            "http://localhost",
            "http://127.0.0.1",
            "ws://localhost",
            "ws://127.0.0.1",
        ]
    elif allow_origins == "*":
        origins = ["*"]
    else:
        origins = list(allow_origins)

    # If the user passed bare-host entries (no port), also add a
    # regex-friendly pattern. This is a no-op for CORS (it ignores
    # patterns) but uvicorn's ws_origins honours them. We just expand
    # the same list to include both bare and common ports.
    # Note: the actual WebSocket port expansion is performed in
    # ``veauto serve`` (cli.py) which knows the bind port.

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Local import to avoid cycle
    from . import routes  # noqa: WPS433
    app.include_router(routes.router)

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc):  # noqa: ANN001
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    if static_dir is not None and Path(static_dir).is_dir():
        static_path = Path(static_dir)

        # Mount assets subfolder if present
        assets = static_path / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        async def _index() -> FileResponse:
            return FileResponse(static_path / "index.html")

        @app.get("/{path:path}")
        async def _spa(path: str) -> Any:  # noqa: ANN401
            # Don't shadow API routes (FastAPI matches more specific first)
            target = static_path / path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(static_path / "index.html")

    return app
