"""Command-line interface for veauto.

Subcommands:
  info      Show media info and exit (no FCPXML output).
  trim      Silence removal only -> FCPXML.
  subtitles Auto-subtitle via faster-whisper -> FCPXML (no cut).
  run       Combined: silence removal + auto-subtitles -> FCPXML.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import audio as audio
from . import pipeline as pipeline
from . import silence as silence
from . import transcriber as transcriber
from .audio import extract_audio
from .fcpxml_builder import build_fcpxml
from .models import (
    CutSegment,
    PipelineConfig,
    SilenceConfig,
    SubtitleConfig,
    SubtitleStyle,
)
from .segments import build_cut_segments
from .silence import detect_silence, ensure_ffmpeg_available, probe_media_info
from .transcriber import transcribe, words_to_subtitle_segments

app = typer.Typer(
    name="veauto",
    help="Video edit automation: silence removal and free auto-subtitling -> FCPXML.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value):
    if value:
        console.print(f'veauto {__version__}')
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, '--version', callback=_version_callback, is_eager=True)):
    '''veauto root callback.'''
    pass


@app.command()
def info(input_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True)):
    '''Show media information (no FCPXML is written).'''
    ensure_ffmpeg_available()
    media = probe_media_info(input_path)
    console.print('[bold]File:[/bold]      ' + str(media.path))
    console.print(f'[bold]Duration:[/bold]  {media.duration:.2f}s')
    console.print(f'[bold]Size:[/bold]      {media.width}x{media.height}')
    console.print(f'[bold]Frame rate:[/bold] {media.frame_rate:.3f} fps')


@app.command()
def trim(
    input_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., '-o', '--output', help='Output FCPXML path.'),
    noise_db: float = typer.Option(-30.0, '--noise-db', help='Silence threshold in dB.'),
    min_silence: float = typer.Option(1.5, '--min-silence', help='Min silence length (s).'),
    margin: float = typer.Option(0.2, '--margin', help='Padding kept on each side of cut (s).'),
    project_name: str = typer.Option('Auto Edit', '--project-name'),
    event_name: str = typer.Option('veauto', '--event-name'),
):
    '''Remove long audio silences and write FCPXML.'''
    ensure_ffmpeg_available()
    media = probe_media_info(input_path)
    console.print(f'[bold]Probing:[/bold] {media.path} ({media.duration:.2f}s)')

    sil_cfg = SilenceConfig(noise_db=noise_db, min_silence=min_silence, margin=margin, enabled=True)
    intervals = detect_silence(input_path, sil_cfg)
    console.print(f'[bold]Silences:[/bold] {len(intervals)} (>= {min_silence}s, <= {noise_db}dB)')
    kept, removed = build_cut_segments(media.duration, intervals, margin=margin)
    console.print(f'[bold]Kept:[/bold] {len(kept)} segments, total {sum(c.duration for c in kept):.2f}s')
    console.print(f'[bold]Removed:[/bold] {sum(r.duration for r in removed):.2f}s')
    xml = build_fcpxml(media, kept, project_name=project_name, event_name=event_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(xml, encoding='utf-8')
    console.print(f'[bold green]Wrote:[/bold green] {output} ({len(xml)} bytes)')


@app.command()
def subtitles(
    input_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., '-o', '--output', help='Output FCPXML path.'),
    model: str = typer.Option(
        'medium', '--model',
        help='faster-whisper model size (tiny|base|small|medium|large-v3|distil-large-v3).',
    ),
    language: str | None = typer.Option(
        None, '--language',
        help='ISO 639-1 code (e.g. "ko", "en"). None = auto-detect.',
    ),
    device: str = typer.Option(
        'auto', '--device',
        help='Inference device (auto|cpu|cuda|mps).',
    ),
    compute_type: str = typer.Option(
        'auto', '--compute-type',
        help='faster-whisper compute type (auto|int8|int8_float16|float16|float32).',
    ),
    beam_size: int = typer.Option(5, '--beam-size', min=1, max=20),
    style_position: str = typer.Option(
        'bottom', '--style-position',
        help='Subtitle position (top|center|bottom).',
    ),
    style_font: str = typer.Option('Apple SD Gothic Neo', '--style-font'),
    style_font_size: int = typer.Option(48, '--style-font-size', min=8, max=400),
    style_max_chars: int = typer.Option(42, '--style-max-chars', min=5, max=200),
    style_max_lines: int = typer.Option(2, '--style-max-lines', min=1, max=4),
    style_min_duration: float = typer.Option(0.8, '--style-min-duration', min=0.1),
    style_max_duration: float = typer.Option(6.0, '--style-max-duration', min=0.5),
    project_name: str = typer.Option('Auto Edit', '--project-name'),
    event_name: str = typer.Option('veauto', '--event-name'),
    keep_audio: bool = typer.Option(
        False, '--keep-audio',
        help='Keep the extracted .wav in --audio-dir (default: delete).',
    ),
    audio_dir: Path = typer.Option(
        Path('.temp/veauto'), '--audio-dir',
        help='Directory for the temporary extracted audio file.',
    ),
):
    '''Auto-subtitle via faster-whisper and write FCPXML (no cut).'''
    ensure_ffmpeg_available()
    media = probe_media_info(input_path)
    console.print(f'[bold]Probing:[/bold] {media.path} ({media.duration:.2f}s)')

    wav_path = audio_dir / (media.path.stem + '.wav')
    console.print(f'[bold]Extracting audio:[/bold] {wav_path}')
    extract_audio(media.path, wav_path)

    try:
        style = SubtitleStyle(
            position=style_position,  # type: ignore[arg-type]
            font=style_font,
            font_size=style_font_size,
            max_chars_per_line=style_max_chars,
            max_lines=style_max_lines,
            min_duration=style_min_duration,
            max_duration=style_max_duration,
        )
        cfg = SubtitleConfig(
            enabled=True,
            model=model,  # type: ignore[arg-type]
            language=language,
            device=device,  # type: ignore[arg-type]
            compute_type=compute_type,  # type: ignore[arg-type]
            beam_size=beam_size,
            style=style,
        )

        console.print(
            f'[bold]Transcribing:[/bold] model={cfg.model} '
            f'device={cfg.device} language={cfg.language or "auto"}'
        )
        words = transcribe(wav_path, cfg)
        console.print(f'[bold]Words:[/bold] {len(words)}')
        segments = words_to_subtitle_segments(
            words,
            max_chars_per_line=style.max_chars_per_line,
            max_lines=style.max_lines,
            min_duration=style.min_duration,
            max_duration=style.max_duration,
        )
        console.print(f'[bold]Subtitles:[/bold] {len(segments)} lines')

        kept = [CutSegment(source_in=0.0, source_out=media.duration)]
        xml = build_fcpxml(
            media, kept,
            subtitles=segments,
            subtitle_style=style,
            project_name=project_name,
            event_name=event_name,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(xml, encoding='utf-8')
        console.print(f'[bold green]Wrote:[/bold green] {output} ({len(xml)} bytes)')
    finally:
        if not keep_audio and wav_path.exists():
            wav_path.unlink()
            console.print(f'[dim]Removed:[/dim] {wav_path}')


@app.command()
def run(
    input_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., '-o', '--output', help='Output FCPXML path.'),
    # Silence stage
    noise_db: float = typer.Option(-30.0, '--noise-db'),
    min_silence: float = typer.Option(1.5, '--min-silence'),
    margin: float = typer.Option(0.2, '--margin'),
    no_silence: bool = typer.Option(False, '--no-silence', help='Skip the silence-removal stage.'),
    # Subtitle stage
    no_subtitles: bool = typer.Option(False, '--no-subtitles', help='Skip the STT stage.'),
    model: str = typer.Option('medium', '--model'),
    language: str | None = typer.Option(None, '--language'),
    device: str = typer.Option('auto', '--device'),
    compute_type: str = typer.Option('auto', '--compute-type'),
    beam_size: int = typer.Option(5, '--beam-size', min=1, max=20),
    # Subtitle style
    style_position: str = typer.Option('bottom', '--style-position'),
    style_font: str = typer.Option('Apple SD Gothic Neo', '--style-font'),
    style_font_size: int = typer.Option(48, '--style-font-size', min=8, max=400),
    style_max_chars: int = typer.Option(42, '--style-max-chars', min=5, max=200),
    style_max_lines: int = typer.Option(2, '--style-max-lines', min=1, max=4),
    style_min_duration: float = typer.Option(0.8, '--style-min-duration', min=0.1),
    style_max_duration: float = typer.Option(6.0, '--style-max-duration', min=0.5),
    # Output
    project_name: str = typer.Option('Auto Edit', '--project-name'),
    event_name: str = typer.Option('veauto', '--event-name'),
    keep_temp: bool = typer.Option(False, '--keep-temp', help='Keep the extracted .wav (debug).'),
    audio_dir: Path = typer.Option(Path('.temp/veauto'), '--audio-dir'),
    # Config (P3) and report (P4)
    config_file: Path | None = typer.Option(
        None, '--config',
        help='Load pipeline settings from a YAML file. CLI options override the file.',
    ),
    write_config: Path | None = typer.Option(
        None, '--write-config',
        help='Write the final (merged) config to a YAML file and exit. '
             'Useful for inspecting the effective configuration.',
    ),
    report: Path | None = typer.Option(
        None, '--report',
        help='Write a report next to the FCPXML output. If a path is '
             'given it is used as-is; otherwise "<output>.report.md" is created.',
    ),
    report_format: str = typer.Option(
        'md', '--report-format',
        help='Report format when --report is set: "json" | "md".',
    ),
):
    '''Single-shot pipeline: silence removal + auto-subtitles -> FCPXML.

    Subtitles are re-timed onto the cut timeline. Subtitles that fall
    inside a removed silence are dropped. Subtitles that span a cut
    boundary are clipped to the cut's end.

    Use --config <yaml> to load defaults from a file; explicit CLI
    options always win. Use --write-config <yaml> to dump the final
    effective configuration (handy for debugging / version control).
    Use --report <path> to emit a Markdown or JSON run report.
    '''
    from pydantic import ValidationError

    from .pipeline import run_pipeline  # local import: heavy module

    # 1. Load base config from YAML if provided
    if config_file is not None:
        try:
            cfg = PipelineConfig.from_yaml(config_file)
            console.print(f'[bold]Loaded config:[/bold] {config_file}')
        except (ValidationError, ValueError) as exc:
            console.print(f'[bold red]Invalid config:[/bold red] {config_file}')
            console.print(str(exc))
            raise typer.Exit(code=2) from exc
    else:
        cfg = PipelineConfig()

    # 2. CLI options always win — only override fields the user actually set.
    #    We treat Typer defaults as "not set" using the sentinel pattern.
    #    Simpler approach for now: always apply CLI options. This is what
    #    most CLI tools do, and it makes the config-file path act as a
    #    *defaults* layer.
    cfg.silence.enabled = not no_silence
    cfg.silence.noise_db = noise_db
    cfg.silence.min_silence = min_silence
    cfg.silence.margin = margin

    cfg.subtitle.enabled = not no_subtitles
    cfg.subtitle.model = model  # type: ignore[assignment]
    cfg.subtitle.language = language
    cfg.subtitle.device = device  # type: ignore[assignment]
    cfg.subtitle.compute_type = compute_type  # type: ignore[assignment]
    cfg.subtitle.beam_size = beam_size

    cfg.subtitle.style.position = style_position  # type: ignore[assignment]
    cfg.subtitle.style.font = style_font
    cfg.subtitle.style.font_size = style_font_size
    cfg.subtitle.style.max_chars_per_line = style_max_chars
    cfg.subtitle.style.max_lines = style_max_lines
    cfg.subtitle.style.min_duration = style_min_duration
    cfg.subtitle.style.max_duration = style_max_duration

    cfg.output.project_name = project_name
    cfg.output.event_name = event_name
    cfg.keep_temp = keep_temp

    # 3. --write-config short-circuits
    if write_config is not None:
        cfg.write_yaml(write_config)
        console.print(f'[bold green]Wrote config:[/bold green] {write_config}')
        raise typer.Exit(code=0)

    ensure_ffmpeg_available()

    # 4. Redirect temp audio to the user-chosen dir.
    _audio = audio

    original_extract = _audio.extract_audio

    def _extract_with_dir(media_path: Path, output_path: Path | None = None) -> Path:
        wav_path = audio_dir / (Path(media_path).stem + '.wav')
        return original_extract(media_path, wav_path)

    _audio.extract_audio = _extract_with_dir  # type: ignore[assignment]
    try:
        result = run_pipeline(input_path, cfg)
    finally:
        _audio.extract_audio = original_extract  # type: ignore[assignment]

    console.print(f'[bold]Probing:[/bold] {result.media.path} ({result.media.duration:.2f}s)')
    console.print(
        f'[bold]Silences:[/bold] {len(result.removed)} '
        f'(>= {min_silence}s, <= {noise_db}dB)'
    )
    console.print(
        f'[bold]Kept:[/bold] {len(result.cuts)} segments, '
        f'total {result.kept_duration:.2f}s'
    )
    console.print(f'[bold]Removed:[/bold] {result.removed_duration:.2f}s')
    if cfg.subtitle.enabled and result.media.has_audio:
        console.print(
            f'[bold]Transcribing:[/bold] model={cfg.subtitle.model} '
            f'device={cfg.subtitle.device} language={cfg.subtitle.language or "auto"}'
        )
        console.print(f'[bold]Words:[/bold] {len(result.words)}')
        console.print(f'[bold]Subtitles:[/bold] {len(result.subtitles)} lines (cut-timeline)')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.fcpxml, encoding='utf-8')
    console.print(f'[bold green]Wrote:[/bold green] {output} ({len(result.fcpxml)} bytes)')

    # 5. Report (P4)
    if report is not None or cfg.output.write_report:
        from .report import write_report
        report_path = report if report is not None else output.with_suffix(
            output.suffix + '.report.md' if output.suffix else output.name + '.report.md'
        )
        # Adjust extension if user picked JSON
        if report_format == 'json' and report_path.suffix not in ('.json',):
            report_path = report_path.with_suffix('.report.json' if not report_path.suffix else report_path.suffix + '.json')
        try:
            write_report(result, report_path, report_format)
        except ValueError as exc:
            console.print(f'[bold red]Report error:[/bold red] {exc}')
            raise typer.Exit(code=2) from exc
        console.print(f'[bold green]Wrote report:[/bold green] {report_path}')


@app.command()
def serve(
    host: str = typer.Option('127.0.0.1', '--host', help='Bind host.'),
    port: int = typer.Option(8000, '--port', help='Bind port.'),
    output_dir: Path = typer.Option(
        Path('./veauto-data'), '--output-dir', '-o',
        help='Where uploads and outputs are stored.',
    ),
    workers: int = typer.Option(2, '--workers', min=1, max=16,
                                help='Max concurrent jobs.'),
    reload_: bool = typer.Option(False, '--reload', help='Auto-reload (dev).'),
    no_ui: bool = typer.Option(
        False, '--no-ui',
        help='Skip serving the Svelte UI (API only).',
    ),
    ui_dir: Path | None = typer.Option(
        None, '--ui-dir',
        help='Path to the built Svelte SPA. Defaults to web/frontend/dist.',
    ),
    allow_origins: str = typer.Option(
        'auto', '--allow-origins',
        help='Comma-separated list of allowed WebSocket/HTTP origins. '
             '"auto" = localhost + 127.0.0.1. Use "*" to allow any origin '
             '(local-only; do not expose to the network).',
    ),
):
    '''Run the veauto web server (FastAPI + WebSocket).

    Uploaded videos are stored under <output-dir>/<job_id>/, and the
    produced FCPXML + Markdown / JSON report next to them. Open the
    printed URL in a browser to access the UI.
    '''
    try:
        import uvicorn  # noqa: F401 — imported for runtime check
    except ImportError as exc:  # pragma: no cover
        raise typer.Exit(
            code=1,
        ) from exc.__class__(
            "uvicorn is not installed. Run: uv add uvicorn[standard]"
        )

    # Auto-detect the built SPA: walk up from this file to the repo root
    # and pick `web/frontend/dist` if it exists.
    static_dir: Path | None = None
    if not no_ui:
        if ui_dir is not None:
            static_dir = ui_dir
        else:
            here = Path(__file__).resolve()
            for parent in here.parents:
                candidate = parent / "web" / "frontend" / "dist"
                if candidate.is_dir():
                    static_dir = candidate
                    break

    from .web.app import create_app

    app = create_app(
        output_root=Path(output_dir),
        max_workers=workers,
        static_dir=static_dir,
        allow_origins=allow_origins,
    )

    console.print(f'[bold green]veauto web[/bold green] listening on '
                  f'[bold]http://{host}:{port}[/bold]')
    if static_dir is not None:
        console.print(f'[bold]UI:[/bold]    {static_dir}')
    else:
        console.print('[yellow]UI:[/yellow]    not built — API only '
                      '(run `npm run build` in web/frontend or use '
                      '[bold]veauto run[/bold] CLI)')
    console.print(f'[bold]Data:[/bold]  {Path(output_dir).resolve()}')

    import uvicorn as _uvicorn

    # Origin allowlist. Uvicorn's built-in WebSocket origin check only
    # allows the same host:port as the server. Browsers typically open
    # ``ws://127.0.0.1:<port>`` while we may bind ``localhost`` (or
    # vice-versa), and the bare-host variants like
    # ``http://localhost`` / ``http://127.0.0.1`` are also common when
    # the browser sends the request. ``ws_origins`` widens the set to
    # include both with and without the explicit port, and both
    # ``http://`` and ``ws://`` schemes.
    if allow_origins == 'auto':
        port_str = str(port)
        ws_allowed_origins = [
            # Bare host (any port) — uvicorn matches these as prefixes
            'http://localhost',
            'http://127.0.0.1',
            'ws://localhost',
            'ws://127.0.0.1',
            # Explicit port — covers the most common cases
            f'http://localhost:{port_str}',
            f'http://127.0.0.1:{port_str}',
            f'ws://localhost:{port_str}',
            f'ws://127.0.0.1:{port_str}',
        ]
    elif allow_origins == '*':
        ws_allowed_origins = ['*']
    else:
        ws_allowed_origins = [
            o.strip() for o in allow_origins.split(',') if o.strip()
        ]

    config = _uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level='info',
        reload=reload_,
        # Force the ``websockets`` (PyPI) protocol implementation.
        # uvicorn's default on Linux picks ``wsproto`` which performs
        # its own Origin header check (rejecting ``localhost`` vs
        # ``127.0.0.1`` mismatches with 403). ``websockets`` is more
        # permissive and we validate origin explicitly in the
        # WebSocket handler (see ``veauto.web.routes``).
        ws='websockets',
        ws_origins=ws_allowed_origins,
    )
    server = _uvicorn.Server(config)
    server.run()
