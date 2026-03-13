# Steam Highlight Extractor

Automatically extracts kill highlights and game moments from Steam's background recording into individual MP4 clips — no manual clipping needed.

It reads the timeline data Steam already records alongside your gameplay, finds the interesting moments (kills, multi-kills, deaths, etc.), and exports each one as a trimmed video clip.

---

## Quick Start

1. **Download** `SteamHighlightExtractor_2.0.1_x64-setup.exe` from the [Releases](../../releases) page and install it
2. **Install ffmpeg** — pick one option:
   - **Winget** (Windows 10/11): open a terminal and run:
     ```
     winget install Gyan.FFmpeg
     ```
   - **Direct download**: get a Windows build from [ffmpeg.org](https://ffmpeg.org/download.html) → Windows → "Windows builds from gyan.dev" → download `ffmpeg-release-essentials.zip`, extract it, and add the `bin/` folder to your PATH
3. **Enable Steam Background Recording**
   Steam → Settings → Game Recording → turn on Background Recording
4. **Launch** `Steam Highlight Extractor` from the Start Menu

No Python installation required.

---

## Screenshot

![Steam Highlight Extractor GUI](screenshot_app.png)

---

## How to use

The app auto-detects your Steam recording folder on launch and scans the most recent session automatically.

1. **Select sessions** from the sidebar — add or remove sessions without re-scanning the ones you've already loaded
2. **Review highlights** in the kill feed — each row shows a thumbnail preview, kill type badge, clip name, and duration
3. **Check the clips** you want to export (all are selected by default)
4. Set your **output folder** and toggle **Merge** if you want a single combined file
5. Click **▶ Export Selected** — per-clip progress cards show encoding status in real time
6. Click **📂 Open Output Folder** when done

Use **⏹ Stop** to cancel at any time. Already-exported clips are always skipped automatically, so re-running is safe.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| Padding before | 10s | Lead-up time included before each event |
| Padding after | 5s | Time included after the last event in a clip |
| Kill pre-shift | 3s | Shifts kill clips earlier to capture the shot, not just the death animation |
| Merge window | 45s | Kills within this window are grouped into one multi-kill clip |
| Kills | ✅ | Extract kill events |
| Deaths | ☐ | Extract death events |
| Merge into one file | ☐ | Concatenate all exported clips into a single MP4 |

---

## Requirements

- **Windows 10/11 x64**
- **ffmpeg** — `winget install Gyan.FFmpeg` or download from [ffmpeg.org](https://ffmpeg.org/download.html)
- **Steam Background Recording enabled**

---

## Running from source

**Prerequisites:** Python 3.9+, Node.js 18+, Rust toolchain

```bash
# 1. Start the backend
python server.py

# 2. In a second terminal, start the Tauri dev window
cd frontend
npx tauri dev
```

---

## Building from source

```bash
# 1. Bundle the Python backend as a sidecar executable
pip install pyinstaller
pyinstaller server.spec --noconfirm

# 2. Copy the sidecar into the Tauri binaries folder
copy dist\server.exe frontend\src-tauri\binaries\server-x86_64-pc-windows-msvc.exe

# 3. Re-enable the sidecar in tauri.conf.json:
#    "externalBin": ["binaries/server"]

# 4. Build the Tauri installer
cd frontend
npx tauri build
```

Output: `frontend/src-tauri/target/release/bundle/`

Or just run `build_tauri.bat` which does all of the above.

---

## How it works

Steam's background recording saves a rolling buffer of your gameplay as MPEG-DASH chunks (`.m4s` files) alongside a `timeline_*.json` that logs in-game events with timestamps.

This tool:
1. Reads the timeline JSON to find kill/highlight events
2. Calculates which recording chunks cover each event, accounting for circular buffer rollover
3. Extracts a thumbnail frame from the clip midpoint during the scan phase
4. Concatenates the relevant chunks and uses ffmpeg to trim and encode each clip

GPU-accelerated encoding is used automatically when available (NVIDIA NVENC, AMD AMF, or Intel Quick Sync). Falls back to software libx264 if no GPU encoder is detected.

No screen capture, no AI analysis — it uses the data Steam already collected.

---

## Output

Clips are saved as `.mp4` files encoded in H.264/AAC, ready to share or edit. Multi-kill clips are labelled with the kill count (e.g. `_3k_`, `_ace_`).

---

## Supported games

Currently tested with **Counter-Strike 2 (CS2)**. Any game that writes events to Steam's timeline JSON should work, but kill/death detection relies on CS2's event format.

---

## Troubleshooting

**No sessions found**
- Make sure Background Recording is enabled in Steam settings
- Try manually browsing to your gamerecordings folder:
  `C:\Program Files (x86)\Steam\userdata\<your-steam-id>\gamerecordings`

**Clips are empty or very short**
- The event may be outside the recording buffer window. Steam only keeps the last ~2 hours by default. Events from earlier in a long session won't have footage.

**Wrong moment captured**
- Increase the **Kill pre-shift** setting. CS2 logs kill events at death confirmation, which is 1–3 seconds after the shot lands.

**Thumbnails not showing**
- This is normal for events near the edge of the recording buffer — the chunk needed for the thumbnail may have been overwritten. The clip itself may still export fine.

**ffmpeg not found**
- Run `winget install Gyan.FFmpeg` in a terminal (use the exact ID — `ffmpeg` alone won't work)
- Or place `ffmpeg.exe` in a folder on your PATH

---

## Files

| File | Purpose |
|---|---|
| `steam_highlight_extractor.py` | Core clip logic — parse, scan, encode, merge |
| `server.py` | FastAPI backend, serves the Tauri frontend over HTTP + SSE |
| `frontend/` | Tauri v2 + Svelte 4 desktop app |
| `server.spec` | PyInstaller spec for bundling the backend sidecar |
| `build_tauri.bat` | One-click production build script |
| `gui.py` | Legacy CustomTkinter UI (kept for reference) |
