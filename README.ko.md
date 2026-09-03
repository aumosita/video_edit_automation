# veauto (한국어)

> 영상 편집 자동화: **침묵 제거**와 무료·오프라인(faster-whisper) **자동 자막**으로
> **FCPXML** 파일을 생성합니다.
>
> 결과물은 **DaVinci Resolve**, **Final Cut Pro**, **Adobe Premiere Pro**에서
> 그대로 불러올 수 있습니다 — 클라우드, API 키, 워터마크 모두 불필요.

[![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](#)
[![Status](https://img.shields.io/badge/status-early%20alpha-orange)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) · [한국어](README.ko.md)

---

## ✨ 기능

| 단계 | 기능 | 상태 |
| --- | --- | --- |
| **P1** | `ffmpeg silencedetect` 기반 침묵 감지 | ✅ 구현 |
| **P1** | margin 설정 가능한 컷 세그먼트 생성 | ✅ 구현 |
| **P1** | FCPXML 1.10 빌더 (rational time, `<asset-clip>`) | ✅ 구현 |
| **P1** | 미디어 프로브 (재생 시간, 해상도, 프레임레이트) | ✅ 구현 |
| **P1** | CLI: `info`, `trim` | ✅ 구현 |
| **P2** | `faster-whisper` 오프라인 STT | ✅ 구현 완료 |
| **P2** | 오디오 추출 (ffmpeg → mono 16 kHz PCM) | ✅ 구현 완료 |
| **P2** | 자동 자막 생성 (최대 글자 수/줄 수/지속 시간) | ✅ 구현 완료 |
| **P2** | 자막 CLI: `subtitles` | ✅ 구현 완료 |
| **P3** | YAML 파이프라인 설정 (`from_yaml` / `to_yaml` / `write_yaml`) | ✅ 구현 완료 |
| **P3** | `veauto run`의 `--config` 및 `--write-config` 옵션 | ✅ 구현 완료 |
| **P4** | Markdown / JSON 실행 리포트 (컷 통계, 침묵 맵, 자막 미리보기) | ✅ 구현 완료 |
| **P5** | 원샷 `run` 명령 (침묵 + 자막) | ✅ 구현 완료 |

---

## 📦 시스템 요구 사항

- **Python** ≥ 3.11 (3.14에서 테스트 완료)
- **FFmpeg** ≥ 4.x, `silencedetect` 필터 포함 (8.0에서 테스트 완료)
- **운영체제**: macOS / Linux (macOS에서는 FFmpeg의 `videotoolbox`를 자동으로 활용)
- faster-whisper 모델 캐시를 위한 약 2GB의 여유 디스크 (첫 STT 실행 시 자동 다운로드)

> **macOS 사용자** — FFmpeg을 한 번만 설치하세요:
> ```bash
> brew install ffmpeg
> ```

---

## 🚀 설치

[`uv`](https://docs.astral.sh/uv/) 사용을 권장합니다. uv는 가상환경, 의존성 해결,
락 파일을 한 도구로 관리해줍니다.

### 1. `uv` 설치 (최초 1회)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # 영구 적용: ~/.zshrc에 추가
```

### 2. 저장소 복제 및 의존성 동기화

```bash
git clone https://github.com/aumosita/video_edit_automation.git
cd video_edit_automation
uv sync --extra dev
```

`.venv/`가 자동으로 만들어지고 런타임/개발 의존성(pytest, pytest-cov, ruff)이
함께 설치됩니다.

### 대안: 일반 `pip`

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## ⚡ 빠른 시작

```bash
# 1. 영상 메타데이터 확인 (FCPXML을 만들지 않음)
uv run veauto info ./samples/talk.mp4

# 2. 긴 침묵을 제거하고 FCPXML 생성
uv run veauto trim ./samples/talk.mp4 -o ./out/talk.fcpxml \
    --noise-db -30 \
    --min-silence 1.5 \
    --margin 0.3

# 3. 자막 자동 생성 (faster-whisper, 컷 없이 전체 타임라인)
uv run veauto subtitles ./samples/talk.mp4 -o ./out/talk.fcpxml \
    --model medium --language ko

# 4. 원샷 처리: 침묵 제거 + 자막 자동 생성, 자막 재매핑
uv run veauto run ./samples/talk.mp4 -o ./out/talk.fcpxml \
    --language ko --model medium

# 5. 결과 .fcpxml을 DaVinci Resolve / FCP / Premiere에서 열기
```

샘플 출력 (P1 — `trim`):

```
Probing: talk.mp4 (182.40s)
Silences: 14 (>= 1.5s, <= -30dB)
Kept: 15 segments, total 156.82s
Removed: 25.58s
Wrote: out/talk.fcpxml (8421 bytes)
```

샘플 출력 (P2 — `subtitles`):

```
Probing: talk.mp4 (182.40s)
Extracting audio: .temp/veauto/talk.wav
Transcribing: model=medium device=mps language=ko
Words: 1247
Subtitles: 89 lines
Wrote: out/talk.fcpxml (46820 bytes)
Removed: .temp/veauto/talk.wav
```

---

## 🛠 CLI 사용법

### `veauto info <video>`

영상 컨테이너/스트림 정보를 출력합니다. **출력 파일은 생성되지 않습니다.**

```text
File:       talk.mp4
Duration:   182.40s
Size:       1920x1080
Frame rate: 30.000 fps
```

### `veauto trim <video> -o out.fcpxml [옵션]`

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `-o`, `--output` | *(필수)* | 출력 FCPXML 경로 (필요 시 상위 디렉터리도 생성) |
| `--noise-db` | `-30.0` | 침묵 임계값 (dB) |
| `--min-silence` | `1.5` | 잘라낼 최소 침묵 길이 (초) |
| `--margin` | `0.3` | 컷 양쪽에 남길 패딩 (초) |
| `--min-keep-seconds` | `0.15` | 이보다 짧게 남은 조각 컷은 제거 (두 침묵 사이의 "글리치 컷" 방지) |
| `--project-name` | `Auto Edit` | FCPXML의 `<project name="…">` |
| `--event-name` | `veauto` | FCPXML의 `<event name="…">` |

**튜닝 팁:**

- **팟캐스트 / 강의** → `--noise-db -35 --min-silence 1.0`
- **캐주얼 브이로그** → `--noise-db -25 --min-silence 0.8 --margin 0.1`
- **소음 많은 환경** → `--noise-db -20 --min-silence 2.0`
- **자연스러운 이어붙임** → `--margin 0.4 --min-keep-seconds 0.3` (문장 사이 여유 확보 + 초단조각 제거)

### `veauto subtitles <video> -o out.fcpxml [옵션]`

**faster-whisper**(오프라인, MIT 라이선스)를 이용한 자동 자막 생성입니다.
**원본 타임라인을 그대로 보존**하며 컷은 적용하지 않습니다. 오디오는 임시
WAV로 추출되고, 전사 완료 후 삭제됩니다(`--keep-audio`로 보존 가능).

**STT 옵션:**

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--model` | `medium` | faster-whisper 모델: `tiny` / `base` / `small` / `medium` / `large-v3` / `distil-large-v3` |
| `--language` | *(자동)* | ISO 639-1 코드 (`ko`, `en`, …). 미지정 시 자동 감지 |
| `--device` | `auto` | `auto` / `cpu` / `cuda` / `mps` |
| `--compute-type` | `auto` | `auto` / `int8` / `int8_float16` / `float16` / `float32` |
| `--beam-size` | `5` | 빔 서치 크기 (1–20) |

**스타일 옵션:**

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--style-position` | `bottom` | `top` / `center` / `bottom` |
| `--style-font` | `Apple SD Gothic Neo` | 폰트 패밀리 |
| `--style-font-size` | `56` | 폰트 크기 (8–400) |
| `--style-max-chars` | `42` | 한 줄 최대 글자 수 |
| `--style-max-lines` | `2` | 최대 줄 수 (1–4) |
| `--style-min-duration` | `0.8` | 최소 표시 시간 (초) |
| `--style-max-duration` | `6.0` | 최대 표시 시간 (초) |

**기타:**

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--audio-dir` | `.temp/veauto` | 추출된 `.wav` 저장 경로 |
| `--keep-audio` | `false` | 전사 후에도 WAV 유지 (디버깅용) |
| `--project-name` | `Auto Edit` | FCPXML의 `<project name="…">` |
| `--event-name` | `veauto` | FCPXML의 `<event name="…">` |

**예시:**

```bash
# 한국어 토크쇼, Apple Silicon
uv run veauto subtitles talk.mp4 -o talk.fcpxml --language ko

# 영어 인터뷰, 가장 가벼운 모델
uv run veauto subtitles interview.mp4 -o interview.fcpxml \
    --model tiny --language en

# 상단 중앙 자막, 큰 폰트
uv run veauto subtitles lecture.mp4 -o lecture.fcpxml \
    --style-position top --style-font-size 64 --style-max-lines 1
```

> ⚠️ 최초 실행 시 선택한 모델을 다운로드합니다(`tiny` 약 75MB,
> `large-v3` 약 1.5GB). Hugging Face 캐시(`~/.cache/huggingface/`)에 저장됩니다.

### `veauto run <video> -o out.fcpxml [옵션]`

`trim`과 `subtitles`를 한 번에 수행하고, **자막을 컷 타임라인으로
재매핑**하는 원샷 파이프라인입니다:

- 원본 오디오는 소스 타임라인 기준으로 전사됩니다.
- 감지된 침묵으로부터 컷 세그먼트가 생성됩니다.
- 각 자막은 새로운(압축된) 타임라인으로 매핑됩니다.
  - **시작점**이 제거된 침묵 안에 있는 자막은 **드롭**됩니다.
  - 컷 경계를 가로지르는 자막은 컷 끝으로 **클리핑**됩니다.

`trim`과 `subtitles`의 **모든 옵션**(`--noise-db`, `--min-silence`,
`--margin`, `--model`, `--language`, `--style-*` 등)을 그대로 받아들이며,
두 개의 스테이지 스킵 플래그를 추가로 제공합니다:

| 옵션 | 설명 |
| --- | --- |
| `--no-silence` | 침묵 제거 단계 건너뛰기 (전체 타임라인 유지) |
| `--no-subtitles` | STT 단계 건너뛰기 (침묵 제거만) |
| `--keep-temp` | 추출된 `.wav`를 실행 후에도 보존 (디버깅) |
| `--audio-dir` | 임시 `.wav` 저장 경로 (기본 `.temp/veauto`) |

**예시:**

```bash
# 침묵 제거 + 자막 자동 생성을 한 번에
uv run veauto run talk.mp4 -o talk.fcpxml --language ko

# 자막 생략 (침묵 제거만)
uv run veauto run talk.mp4 -o talk.fcpxml --no-subtitles

# 침묵 제거 생략 (전체 타임라인에 자막만)
uv run veauto run talk.mp4 -o talk.fcpxml --no-silence --language ko
```

샘플 출력:

```
Probing: talk.mp4 (182.40s)
Silences: 14 (>= 1.5s, <= -30dB)
Kept: 15 segments, total 156.82s
Removed: 25.58s
Transcribing: model=medium device=mps language=ko
Words: 1247
Subtitles: 86 lines (cut-timeline)
Wrote: out/talk.fcpxml (46210 bytes)
Removed: .temp/veauto/talk.wav
```

---

## 🧑‍💻 개발

```bash
# 린트
uv run ruff check .

# 테스트 (95개, M시리즈 Mac 기준 약 2초)
uv run pytest
uv run pytest --cov=veauto   # 커버리지 포함
```

### 프로젝트 구조

```
src/veauto/
├── __init__.py
├── cli.py             # Typer CLI (info / trim / subtitles / run)
├── models.py          # Pydantic 데이터 모델 + PipelineConfig (YAML I/O)
├── silence.py         # ffmpeg silencedetect 래퍼
├── segments.py        # 침묵 → 컷 세그먼트 변환
├── audio.py           # ffmpeg 오디오 추출 (mono 16 kHz WAV)
├── transcriber.py     # faster-whisper 어댑터 + 단어→자막 그룹핑
├── pipeline.py        # 침묵 + STT → FCPXML 오케스트레이터
├── report.py          # JSON / Markdown 실행 리포트
└── fcpxml_builder.py  # FCPXML 1.10 빌더

tests/
├── test_silence.py
├── test_segments.py
├── test_fcpxml_builder.py
├── test_audio.py
├── test_transcriber.py
├── test_config_yaml.py
├── test_report.py
├── test_pipeline.py
├── test_cli.py
├── test_subtitles_cli.py
└── test_run_pipeline_cli.py
```

### 새 서브커맨드 추가하기

1. `src/veauto/`에 핵심 로직 구현 (순수 함수, 입출력은 Pydantic 모델).
2. `cli.py`에 `Typer` 명령 추가.
3. `typer.testing.CliRunner`로 `tests/test_*.py`에 테스트 추가.
4. 본 README의 **CLI 사용법** 섹션을 갱신.

---

## 🗺 로드맵

- [x] **P1** 침묵 제거 + FCPXML 파이프라인
- [x] **P2** `faster-whisper` STT 통합 (다국어, MPS / CUDA / CPU 자동 감지)
- [x] **P2** `max_chars_per_line`, `max_lines`, `min/max_duration`을 반영한 자막 생성
- [x] **P2** `veauto subtitles <video>` 서브커맨드 (컷 없이 전체 타임라인)
- [x] **P3** YAML 설정 (`PipelineConfig.from_yaml` / `to_yaml` / `write_yaml`)을 CLI에 연결 (`--config` / `--write-config`)
- [x] **P4** Markdown / JSON 리포트 (컷 통계, 침묵 맵, 자막 통계) (`--report` / `--report-format`)
- [x] **P5** `veauto run <video>` — 원샷 침묵 + 자막 (컷 타임라인, 자막 재매핑)
- [ ] **P6** 비-Python 사용자를 위한 PyInstaller 번들

---

## 🤝 기여

이슈와 PR을 환영합니다. 다음을 지켜주세요:

1. 푸시 전 `uv run ruff check .`와 `uv run pytest`를 실행하세요.
2. 새로운 동작에는 테스트를 추가하세요.
3. 한 PR에는 한 가지 기능/수정만 담는 것을 권장합니다.

## 📄 라이선스

[MIT](LICENSE) © veauto contributors

## 🙏 감사의 말

- [FFmpeg](https://ffmpeg.org/) — 이 프로젝트의 가능성을 만들어준 만능 도구
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — CTranslate2 기반 Whisper
- [Typer](https://typer.tiangolo.com/) & [Rich](https://rich.readthedocs.io/) — 훌륭한 CLI 프레임워크
- [lxml](https://lxml.de/) — 견고한 FCPXML 직렬화