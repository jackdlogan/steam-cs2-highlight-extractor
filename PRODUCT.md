# Product Document — Steam Highlight Extractor

**Version:** 1.0
**Date:** 2026-03-10
**Status:** Working prototype

---

## 1. Overview




---

## 2. Problem Statement

Steam's background recording captures everything, but gives users no easy way to extract specific moments. The only built-in option is manual clipping inside the Steam overlay, which requires:

- Remembering when a good moment happened
- Opening the Steam overlay mid-game or after
- Manually setting in/out points
- Waiting for Steam to process the clip

This workflow is slow, interrupts gameplay, and misses moments the player didn't consciously notice at the time. Players who want highlights from a 2-hour session have no practical option today.

---

## 3. Solution

Steam already records a `timeline_*.json` file alongside every session. This file contains timestamped entries for every in-game event — kills, deaths, round starts, bomb plants, and more. Steam uses this data to power its own clip suggestions, but does not expose it for bulk export.

Steam Highlight Extractor reads this timeline, maps each event back to the exact position in the recording buffer, and uses ffmpeg to extract and encode each moment as a standalone MP4. The entire process runs automatically after a session ends — no manual input required.

---

## 4. Target Users

| User | Need |
|---|---|
| Casual players | Want to save kills/highlights without effort |
| Content creators | Need raw footage for YouTube / TikTok edits |
| Streamers | Want a local backup of highlights independent of stream VODs |
| Competitive players | Want to review their own gameplay moments |

**Technical level:** Non-technical users are the primary target. The GUI is designed to work with zero configuration for most users.

---

## 5. Features

### 5.1 Core

| Feature | Description |
|---|---|
| Auto session detection | Finds Steam recording folder from Windows registry — no path configuration needed |
| Timeline-driven extraction | Uses Steam's own event data; no video analysis or AI required |
| Multi-kill merging | Consecutive kills within a configurable window are merged into one clip |
| Circular buffer awareness | Correctly handles sessions longer than the recording buffer (e.g. 3-hour game with 2-hour buffer) |
| Skip already-exported clips | Re-runs only process new sessions; existing files are untouched |
| ffmpeg auto-detection | Finds ffmpeg on PATH or bundled next to the script |

### 5.2 GUI

| Feature | Description |
|---|---|
| Auto-detect on launch | Recording path is populated immediately on open |
| Session picker | Lists all available sessions; defaults to the latest one |
| Live log with colour coding | Errors in red, warnings in orange, saved clips in green |
| Progress bar | Tracks completion across selected sessions |
| Open output folder button | Opens the clips folder in Explorer when done |
| Event type toggles | Checkboxes to enable/disable kills and deaths without editing code |

### 5.3 Clip quality

- Video: H.264 (libx264), source resolution (1080p from Steam's 12 Mbps recording)
- Audio: AAC stereo
- Container: MP4 with `faststart` flag (web-compatible)

---

## 6. Architecture

```
gui.py                          ← tkinter GUI, threading, log display
└── steam_highlight_extractor.py
        ├── find_steam_recording_path()   Windows registry + fallback paths
        ├── find_all_sessions()           Walks recording folder for session.mpd files
        ├── parse_mpd_info()              Reads DASH manifest for chunk layout + timing
        ├── parse_timeline_json()         Reads Steam timeline events (ms timestamps)
        ├── process_session()             Matches events to recording window, groups clips
        └── export_clip()                 Concatenates .m4s chunks → ffmpeg → MP4
```

### Recording format

Steam background recording uses MPEG-DASH:
- `session.mpd` — manifest with timing metadata (`Period start`, `startNumber`, segment duration)
- `init-streamN.m4s` — decoder initialisation segment
- `chunk-streamN-NNNNN.m4s` — 3-second media segments (circular buffer)

### Timing model

The key challenge is mapping a timeline event time (milliseconds from game session start) to a position in the recording buffer (which may have rolled over for long sessions).

```
timeline_offset = filename_offset + period_start

  filename_offset = wall-clock gap between timeline file creation and recording session start
  period_start    = MPD value encoding how many seconds of the buffer have been overwritten
```

This formula is exact because `period_start` is updated by Steam every time a 3-second chunk is overwritten by the circular buffer.

---

## 7. Known Limitations

| Limitation | Detail |
|---|---|
| Windows only | Path detection and `os.startfile` are Windows-specific |
| CS2 primary support | Kill detection relies on CS2 event format. Other games may produce events that are not filtered correctly |
| Events outside buffer window | If a game session exceeds the recording buffer length, events from the early portion of the session have no footage and are silently skipped |
| No hardware encoding | ffmpeg is called with `libx264` (CPU). Encoding 40+ clips is slow on low-end machines |
| Python + ffmpeg dependency | Users must have both installed. Not a zero-install tool yet |
| Single timeline per session | If a game session spans multiple timeline files, only the best-matched one is used |

---

## 8. File Structure

```
/
├── steam_highlight_extractor.py   Core logic + CLI entry point
├── gui.py                         Graphical interface
├── README.md                      User-facing setup and usage guide
├── PRODUCT.md                     This document
└── SteamHighlights/               Output folder (created on first run)
    └── <session>_<time>_<tag>.mp4
```

---

## 9. Configuration Reference

All settings live at the top of `steam_highlight_extractor.py`. The GUI exposes the four timing settings and event type toggles; the rest require editing the file.

| Constant | Default | Description |
|---|---|---|
| `STEAM_RECORDING_PATH` | `None` | Override auto-detection with a fixed path |
| `OUTPUT_FOLDER` | `./SteamHighlights` | Where clips are saved |
| `CLIP_PADDING_BEFORE` | `10` s | Lead-up time before each event |
| `CLIP_PADDING_AFTER` | `5` s | Tail time after each event |
| `KILL_EVENT_PRE_SHIFT` | `3` s | Extra rewind for kill events (death confirmation delay) |
| `MULTI_KILL_MERGE_WINDOW` | `45` s | Max gap between kills to merge into one clip |

---

## 10. Potential Future Work

| Item | Priority | Notes |
|---|---|---|
| PyInstaller + bundled ffmpeg | High | One-click `.exe` with no dependencies to install |
| Death / other event extraction | High | Code already exists, just commented out; needs UI toggle |
| Output filename with game name | Medium | Parse app ID to Steam game name via local Steam data |
| Hardware encoding (NVENC/QSV) | Medium | Faster export on modern GPUs |
| Multi-timeline session support | Low | Edge case for very long sessions with multiple timeline files |
| Other games support | Low | Needs per-game event format mapping |
| Clip preview in GUI | Low | Thumbnail or in-app playback before saving |
