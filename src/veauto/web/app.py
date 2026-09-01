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
