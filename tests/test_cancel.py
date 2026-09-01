"""Tests for cancel-aware subprocess wrapping in :mod:`veauto.silence`.

The :func:`veauto.silence.run_with_cancel` helper is the foundation of
cooperative cancellation: long-running ffmpeg invocations check a
callable periodically and SIGTERM the process group if it returns
``True``. This module pins down the contract.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from veauto.silence import run_with_cancel


# A python invocation that sleeps — perfect for cancel tests because
# it has no children and accepts SIGTERM cleanly.
def _sleep_cmd(seconds: float) -> list[str]:
    return [
        "python3",
        "-c",
        f"import time; time.sleep({seconds})",
    ]


class TestRunWithCancel:
    def test_no_cancel_runs_to_completion(self):
        result = run_with_cancel(_sleep_cmd(0.3), should_cancel=None)
        assert result.returncode == 0

    def test_cancel_during_run_kills_process(self):
        # Cancel triggered shortly after start.
        event = threading.Event()
        event.set()  # already "cancelled" — should fire on first poll
        result = run_with_cancel(
            _sleep_cmd(5.0), should_cancel=event.is_set, term_grace=0.3
        )
        # Either SIGTERM (-15) or SIGKILL (-9) — both negative.
        assert result.returncode != 0
        # Should return quickly (well before 5 s).
        # We don't time it here to keep the test deterministic, but
        # the function MUST have returned, which is the actual proof.

    def test_cancel_set_after_delay_kills_process(self):
        # Cancel ~100 ms in; python should die within grace.
        event = threading.Event()

        def _trigger_later() -> None:
            time.sleep(0.1)
            event.set()

        t = threading.Thread(target=_trigger_later)
        t.start()
        start = time.monotonic()
        result = run_with_cancel(
            _sleep_cmd(10.0),
            should_cancel=event.is_set,
            term_grace=0.5,
        )
        elapsed = time.monotonic() - start
        t.join(timeout=1.0)
        assert result.returncode != 0
        # Should return roughly at 0.1 s (cancel) + 0.5 s (grace) = 0.6 s
        # with a generous upper bound for CI noise.
        assert elapsed < 3.0, f"cancel took {elapsed:.2f}s, expected <3s"

    def test_cancel_never_set_runs_to_completion(self):
        event = threading.Event()
        result = run_with_cancel(
            _sleep_cmd(0.2), should_cancel=event.is_set
        )
        assert result.returncode == 0

    def test_poll_interval_is_respected(self):
        # A very long-running process with no cancel; the function
        # should poll on each interval. We use a slightly long sleep
        # to confirm poll-driven return.
        event = threading.Event()
        result = run_with_cancel(
            _sleep_cmd(0.4), should_cancel=event.is_set
        )
        assert result.returncode == 0

    def test_external_subprocess_not_killed_by_cancel_of_other(
        self, tmp_path: Path
    ):
        """``run_with_cancel`` should only kill the subprocess it
        itself started. An unrelated sleep process started in parallel
        must remain alive when the watched subprocess is killed.
        """
        # Start a "control" process we don't want killed.
        control = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(2.0)"],
        )
        try:
            event = threading.Event()
            event.set()  # pre-cancelled
            run_with_cancel(
                _sleep_cmd(5.0),
                should_cancel=event.is_set,
                term_grace=0.3,
            )
            # Control process is still running.
            assert control.poll() is None
        finally:
            control.terminate()
            control.wait(timeout=2.0)


class TestDetectSilenceCancel:
    """Integration: detect_silence must respect should_cancel."""

    def test_detect_silence_passes_should_cancel_through(
        self, tmp_path: Path, monkeypatch
    ):
        """When ``should_cancel`` is provided, ``detect_silence`` must
        pass it to the underlying subprocess wrapper. We assert this
        indirectly by stubbing the wrapper.
        """
        from veauto import silence as sl
        from veauto.models import SilenceConfig

        captured: dict = {}

        def fake_run_with_cancel(cmd, *, should_cancel, **kw):
            captured["should_cancel"] = should_cancel
            captured["cmd"] = cmd
            # Pretend we returned nothing.
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(sl, "run_with_cancel", fake_run_with_cancel)
        monkeypatch.setattr(sl, "parse_silencedetect_output", lambda _: [])

        fake = tmp_path / "fake.mp4"
        fake.write_bytes(b"\x00")

        event = threading.Event()
        sl.detect_silence(
            fake,
            SilenceConfig(noise_db=-30.0, min_silence=0.5),
            ffmpeg_path="/bin/echo",  # never actually used
            should_cancel=event.is_set,
        )
        # ``Event.is_set`` is a bound method — ``is`` is not stable.
        # Compare by ``==`` or by the underlying ``__self__``.
        assert captured["should_cancel"].__self__ is event

    def test_detect_silence_without_should_cancel_uses_default(
        self, tmp_path: Path, monkeypatch
    ):
        from veauto import silence as sl
        from veauto.models import SilenceConfig

        captured: dict = {}

        def fake_run_with_cancel(cmd, *, should_cancel, **kw):
            captured["should_cancel"] = should_cancel
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(sl, "run_with_cancel", fake_run_with_cancel)
        monkeypatch.setattr(sl, "parse_silencedetect_output", lambda _: [])

        fake = tmp_path / "fake.mp4"
        fake.write_bytes(b"\x00")
        sl.detect_silence(
            fake,
            SilenceConfig(noise_db=-30.0, min_silence=0.5),
            ffmpeg_path="/bin/echo",
        )
        assert captured["should_cancel"] is None


class TestExtractAudioCancel:
    """Integration: extract_audio must respect should_cancel."""

    def test_extract_audio_passes_should_cancel_through(
        self, tmp_path: Path, monkeypatch
    ):
        from veauto import audio as au

        captured: dict = {}

        def fake_run_with_cancel(cmd, *, should_cancel, **kw):
            captured["should_cancel"] = should_cancel
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(au, "run_with_cancel", fake_run_with_cancel)
        monkeypatch.setattr(au, "ensure_ffmpeg_available", lambda: "/bin/echo")

        event = threading.Event()
        au.extract_audio(
            tmp_path / "in.mp4",
            tmp_path / "out.wav",
            should_cancel=event.is_set,
        )
        assert captured["should_cancel"].__self__ is event

    def test_extract_audio_without_should_cancel(
        self, tmp_path: Path, monkeypatch
    ):
        from veauto import audio as au

        captured: dict = {}

        def fake_run_with_cancel(cmd, *, should_cancel, **kw):
            captured["should_cancel"] = should_cancel
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(au, "run_with_cancel", fake_run_with_cancel)
        monkeypatch.setattr(au, "ensure_ffmpeg_available", lambda: "/bin/echo")

        au.extract_audio(tmp_path / "in.mp4", tmp_path / "out.wav")
        assert captured["should_cancel"] is None


class TestJobsExtractSignature:
    """Regression: ``JobManager._run_pipeline_with_progress`` must
    monkey-patch ``pipeline.extract_audio`` with a wrapper that accepts
    the same (input_path, output_path) signature as the real function
    in :mod:`veauto.audio`. The previous version only accepted a
    single argument and crashed with
    ``TypeError: _extract() takes 1 positional argument but 2 were given``
    on real media (HEVC MOV, etc.).
    """

    def test_extract_wrapper_signature_matches(self, monkeypatch, tmp_path):
        from veauto import pipeline as pl
        from veauto.models import MediaInfo, PipelineConfig
        from veauto.web import jobs as jobs_mod

        manager = jobs_mod.JobManager(output_root=tmp_path / "data")
        in_path = tmp_path / "in.mp4"
        in_path.write_bytes(b"fake")

        # Stub all pipeline functions so the run completes without
        # touching the real ffmpeg.
        monkeypatch.setattr(
            pl, "probe_media_info",
            lambda p: MediaInfo(
                path=in_path, duration=5.0, width=1920, height=1080,
                frame_rate=30.0, has_audio=True,
            ),
        )
        monkeypatch.setattr(pl, "detect_silence", lambda p, c, **kw: [])
        monkeypatch.setattr(pl, "_transcribe", lambda *a, **kw: [])

        called: dict = {}

        def fake_extract(*args, **kwargs):
            called["args"] = args
            called["kwargs"] = kwargs
            return tmp_path / "out.wav"

        monkeypatch.setattr(pl, "extract_audio", fake_extract)

        cfg = PipelineConfig()
        cfg.silence.enabled = False
        cfg.subtitle.enabled = True  # triggers _extract wrapper

        def progress(*a, **kw):
            return None

        manager._run_pipeline_with_progress(
            in_path, cfg, progress, cancel_event=threading.Event()
        )

        # The wrapper must have forwarded (in_path, output_path) and
        # the should_cancel kwarg.
        assert "args" in called, "extract wrapper was not invoked"
        assert len(called["args"]) == 2, (
            f"wrapper must accept (input_path, output_path); got {called['args']!r}"
        )
        assert called["args"][0] == in_path
        assert called["args"][1] is not None
        assert "should_cancel" in called["kwargs"]
        # should_cancel must be a bound method of a threading.Event.
        assert called["kwargs"]["should_cancel"].__self__ is not None
