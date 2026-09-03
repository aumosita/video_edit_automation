# veauto

Video edit automation: FCPXML generator with silence removal and free offline (faster-whisper) auto-subtitling.

The output can be imported into DaVinci Resolve, Final Cut Pro, or Adobe Premiere Pro.

## Status

Alpha. Core pipeline is implemented and tested (49/49 tests passing):

| Stage | Module | Status |
| --- | --- | --- |
| P1 Silence detection | veauto.silence | done |
| P2 Cut segments | veauto.segments | done |
| P5 FCPXML 1.10 builder | veauto.fcpxml_builder | done |
| P6 CLI (info/trim) | veauto.cli | done |
| P3 STT (faster-whisper) | optional dep | planned |
| P4 Subtitle grouping | veauto.subtitle | planned |

## Features

- Detect long audio silences via FFmpeg silencedetect
- Build keep/cut segments with configurable margin
- Generate FCPXML 1.10 documents with rational frame time
- Lane-1 title subtitles (font, size, color, position)
- CLI: veauto info, veauto trim

## Requirements

- Python 3.11+
- FFmpeg 4.0+ (brew install ffmpeg)
- For STT: pip install faster-whisper (Intel macOS not yet supported)

## Install

    uv sync --extra dev

## Usage

    veauto info input.mp4

    veauto trim input.mp4 -o output.fcpxml --noise-db -30 --min-silence 1.5 --margin 0.3

The output FCPXML can be imported into DaVinci Resolve, Final Cut Pro, or Adobe Premiere Pro.

## Project Layout

    src/veauto/
      models.py
      silence.py
      segments.py
      fcpxml_builder.py
      cli.py
    tests/
      test_silence.py
      test_segments.py
      test_fcpxml_builder.py
      test_cli.py

## License

GPL v3. See LICENSE.
