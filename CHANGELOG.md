# Changelog

All notable changes to Steam Highlight Extractor are documented here.

## [2.3.0] - 2026-03-17

### Added
- **Quality selection** — Low / Medium / High dropdown next to Extract Highlights; maps to CRF 28 / 23 / 18 across all encoders (NVENC, AMF, QSV, libx264)
- **Merge progress bar** — progress bar resets and pulses yellow while ffmpeg merges clips, with "Merging N clips…" label in the footer

### Fixed
- **Death badge icon** — death clips now correctly show the red skull badge instead of a green star (was caused by `_is_kill_event` matching "killed" as a substring of "you were killed by")
- **Map icons in built app** — CSP was missing `data:` from `img-src`; Vite inlines small PNGs as base64 data URIs which were blocked in production

### Changed
- Footer rearranged: Re-scan + Open Folder on the left; Merge toggle, Quality, and Extract Highlights on the right

## [2.2.0] - 2026-03-17

### Added
- **In-app DASH video preview** — watch raw Steam recordings before exporting via a play button on every kill-feed row
- **Custom video player with clip-scoped controls** — progress bar shows only the clip window (0 → clip end), not the full session; event kill markers appear as yellow tick marks on the bar
- **Clip trim controls** — Start/End sliders (+/−5–20s) in the preview modal let you adjust clip boundaries; live seek preview; ✓ Apply saves the adjusted bounds back to the kill feed
- **TRIMMED badge** on kill-feed rows after a trim is applied, so trimmed clips are visually distinct before export
- **Modern player UI redesign** — cinematic blurred backdrop, entrance animation, 16:9 aspect-ratio video, glow effects on play button and event ticks, hover-expanding progress bar with hidden-until-hover playhead
- **LIVE PREVIEW / EXPORTED mode badge** in the player header
- **FFmpeg bundled into server.exe** via PyInstaller — users no longer need a separate FFmpeg installation
- `build_tauri.bat` prerequisite check: fails early with a clear message if `ffmpeg.exe` is missing from the project root
- `/api/session-stream/{session_name}/{filename}` endpoint in `server.py` for serving DASH segments; MPD manifest is rewritten on-the-fly to inject absolute URLs with `recording_path` so dash.js segment requests always resolve correctly

### Changed
- DASH preview player uses `import * as dashjs` (fixes undefined MediaPlayer in Vite ESM build)
- CSP updated to include `media-src blob:` (required for dash.js MediaSource API) and `asset:` for Tauri asset protocol
- MPD pre-validation fetch added before initialising dash.js — surfaces 404/network errors in the player UI immediately
- `seekToMarker` now uses absolute session time for DASH seeks and clip-relative time for MP4 seeks

## [2.1.0] - 2026-03-15

### Added
- Export bug fixes: QSV pixel format, output path handling, sidecar cleanup

## [2.0.2] - 2026-03-12

### Fixed
- Unused CSS warnings from Svelte build

## [2.0.1] - 2026-03-10

### Changed
- Updated screenshot and build script
