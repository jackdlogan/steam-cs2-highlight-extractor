# Steam Highlight Extractor

Automatically extracts kill highlights and game moments from Steam's background recording into individual MP4 clips — no manual clipping needed.

It reads the timeline data Steam already records alongside your gameplay, finds the interesting moments (kills, multi-kills, deaths, etc.), and exports each one as a trimmed video clip.

---

## Quick Start

1. **Download** `SteamHighlightExtractor.exe` from the [Releases](../../releases) page
2. **Install ffmpeg** — pick one option:
   - **Winget** (Windows 10/11): open a terminal and run:
     ```
     winget install Gyan.FFmpeg
     ```
   - **Direct download**: get a Windows build from [ffmpeg.org](https://ffmpeg.org/download.html) → Windows → "Windows builds from gyan.dev" → download the `ffmpeg-release-essentials.zip`, extract it, and place `ffmpeg.exe` in the same folder as `SteamHighlightExtractor.exe`
3. **Enable Steam Background Recording**
   Steam → Settings → Game Recording → turn on Background Recording
4. **Double-click** `SteamHighlightExtractor.exe`

No Python installation required.

---

## Screenshot

![Steam Highlight Extractor GUI](screenshot_app.png)

---

## How to use

The app auto-detects your Steam recording folder on launch.

1. Select which sessions to process from the list (defaults to the latest)
2. Adjust settings if needed
3. Click **▶ Extract Highlights**
4. Click **📂 Open Output Folder** when done

Use **⏹ Stop** to pause at any time — click **▶ Resume** to continue from where you left off. Already-exported clips are always skipped automatically.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| Padding before | 10s | Lead-up time included before each event |
| Padding after | 5s | Time included after the last event in a clip |
| Kill pre-shift | 3s | Shifts kill clips earlier to capture the shot, not just the death animation |
| Merge window | 45s | Kills within this window are merged into one multi-kill clip |
| Kills | ✅ | Extract kill events |
| Deaths | ☐ | Extract death events |

---

## Requirements

- **Windows 10/11**
- **ffmpeg** — `winget install Gyan.FFmpeg` or download from [ffmpeg.org](https://ffmpeg.org/download.html)
- **Steam Background Recording enabled**

If running from source instead of the exe:
- **Python 3.9+** — no extra packages needed

---

## Running from source

```
python gui.py
```

Or CLI only (processes all sessions, no GUI):

```
python steam_highlight_extractor.py
```

To change defaults permanently, edit the constants at the top of `steam_highlight_extractor.py`.

---

## Building the exe yourself

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "SteamHighlightExtractor" gui.py
```

Output: `dist\SteamHighlightExtractor.exe`

---

## How it works

Steam's background recording saves a rolling buffer of your gameplay as MPEG-DASH chunks (`.m4s` files) alongside a `timeline_*.json` that logs in-game events with timestamps.

This tool:
1. Reads the timeline JSON to find kill/highlight events
2. Calculates which recording chunks cover each event, accounting for circular buffer rollover
3. Concatenates the relevant chunks and uses ffmpeg to trim and encode each clip

No screen capture, no AI analysis — it uses the data Steam already collected.

---

## Output

Clips are saved to `SteamHighlights/` (or your chosen folder) as `.mp4` files encoded in H.264/AAC, ready to share or edit. Multi-kill clips are labelled with the kill count (e.g. `_3k_`).

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

**ffmpeg not found**
- Run `winget install Gyan.FFmpeg` in a terminal (use the exact ID — `ffmpeg` alone won't be found)
- Or place `ffmpeg.exe` directly next to the exe — download from [ffmpeg.org](https://ffmpeg.org/download.html) → Windows → gyan.dev builds → `ffmpeg-release-essentials.zip`

---

## Files

| File | Purpose |
|---|---|
| `SteamHighlightExtractor.exe` | Standalone app — download from Releases |
| `gui.py` | GUI source — run with `python gui.py` |
| `steam_highlight_extractor.py` | Core logic, also runnable as a CLI script |
