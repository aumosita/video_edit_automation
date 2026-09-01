"""Command-line interface for veauto.

Subcommands:
  info      Show media info and exit (no FCPXML output).
  trim      Silence removal only -> FCPXML.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .fcpxml_builder import build_fcpxml
from .models import SilenceConfig
from .segments import build_cut_segments
from .silence import detect_silence, ensure_ffmpeg_available, probe_media_info

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
