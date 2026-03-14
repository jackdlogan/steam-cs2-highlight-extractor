"""
Steam Highlight Extractor (Timeline-Aware Edition)
====================================================
Reads Steam's native timeline JSON files to find game-marked events
(kills, deaths, achievements, boss fights, etc.) and exports those
moments as MP4 clips — no AI frame analysis needed.

Requirements:
    - ffmpeg on PATH  OR  ffmpeg.exe placed next to this script
      Download: https://ffmpeg.org/download.html
      Or via winget: winget install Gyan.FFmpeg

    - Python 3.9+  (no extra pip packages needed)

How to run:
    python steam_highlight_extractor.py

Steam recording folder is auto-detected from the registry.
Override STEAM_RECORDING_PATH below if auto-detection fails.
"""

import os
import sys
import json
import re
import subprocess
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import timedelta, datetime

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

# Leave as None to auto-detect, or set manually e.g.:
# STEAM_RECORDING_PATH = r"C:\Users\YourName\Videos\SteamRecordings"
STEAM_RECORDING_PATH = None

# Highlights will be saved here (default: ~/Videos/SteamHighlights)
OUTPUT_FOLDER = Path.home() / "Videos" / "SteamHighlights"

# Seconds to include before the FIRST event and after the LAST event in a clip
CLIP_PADDING_BEFORE = 1   # seconds before first event
CLIP_PADDING_AFTER  = 5   # seconds after last event

# CS2 kill events are logged at the moment of death confirmation, which is
# typically 1–3 seconds AFTER you actually fired the shot. This shifts kill
# event times earlier so the clip captures the actual shot, not just the aftermath.
KILL_EVENT_PRE_SHIFT = 3  # extra seconds to rewind for kill events (0 to disable)

# Events within this many seconds of each other are merged into ONE clip.
# e.g. 3 kills at 0:32, 0:38, 0:45 → single multi-kill clip
MULTI_KILL_MERGE_WINDOW = 45  # seconds

# Set to True to also extract clips where you died
INCLUDE_DEATH_HIGHLIGHTS = False

# If two consecutive events within a group are more than this many seconds apart,
# export them as separate sub-clips and jump-cut them together instead of one long clip.
# Set to 0 to disable.
JUMP_CUT_THRESHOLD = 15  # seconds

# Kill-type events — these are grouped aggressively into multi-kill clips
KILL_EVENTS = [
    "kill",
    "elimination",
    "assist",
]

# Other highlight events merged with a tighter window
OTHER_EVENTS = [
    "death",
    "clutch",
    "achievement",
    "boss",
    "highlight",
    "marker",       # manual Ctrl+F12 markers you placed yourself
    "bookmark",
]

# Combined list used for filtering
INTERESTING_EVENTS = KILL_EVENTS + OTHER_EVENTS

# ──────────────────────────────────────────────────────────────────────────────

def _find_ffmpeg():
    """
    Return the path to ffmpeg, checking (in order):
      1. A bundled ffmpeg.exe / ffmpeg sitting next to this script
      2. ffmpeg on the system PATH
    Returns None if not found.
    """
    # Bundled binary next to the script
    script_dir = Path(__file__).parent
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = script_dir / name
        if candidate.exists():
            return str(candidate)

    # System PATH
    found = shutil.which("ffmpeg")
    return found  # may be None


FFMPEG_BIN = _find_ffmpeg()


_ENCODER_CACHE_PATH = Path.home() / ".config" / "steam_hl" / "encoder_cache.json"


def _ffmpeg_version():
    """Return ffmpeg version string, or empty string on failure."""
    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run([FFMPEG_BIN, "-version"], capture_output=True,
                           timeout=5, creationflags=no_window)
        first_line = r.stdout.decode(errors="replace").splitlines()[0]
        return first_line
    except Exception:
        return ""


def _detect_gpu_encoder():
    """
    Probe ffmpeg for available GPU H.264 encoders.
    Result is cached to disk — probing only runs when ffmpeg version changes
    or the cache is missing. Returns encoder name or None (→ libx264 fallback).
    """
    if not FFMPEG_BIN:
        return None

    ffmpeg_ver = _ffmpeg_version()

    # Try reading cache
    try:
        cached = json.loads(_ENCODER_CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("ffmpeg_version") == ffmpeg_ver:
            return cached.get("encoder")  # may be None (no GPU encoder found)
    except Exception:
        pass  # cache missing or corrupt — probe fresh

    # Probe each candidate
    candidates = ["h264_nvenc", "h264_amf", "h264_qsv"]
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    found = None
    for enc in candidates:
        try:
            result = subprocess.run(
                [FFMPEG_BIN, "-y",
                 "-f", "lavfi", "-i", "nullsrc=s=32x32:d=0.1",
                 "-vframes", "1", "-c:v", enc, "-f", "null", "-"],
                capture_output=True, timeout=10, creationflags=no_window,
            )
            if result.returncode == 0:
                found = enc
                break
        except Exception:
            continue

    # Write cache
    try:
        _ENCODER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ENCODER_CACHE_PATH.write_text(
            json.dumps({"ffmpeg_version": ffmpeg_ver, "encoder": found}),
            encoding="utf-8",
        )
    except Exception:
        pass  # non-fatal — cache just won't persist

    return found


GPU_ENCODER = _detect_gpu_encoder()


def check_ffmpeg():
    if FFMPEG_BIN:
        return
    print("ERROR: ffmpeg not found.")
    print()
    print("  Install options:")
    print("    winget install Gyan.FFmpeg")
    print("    OR download from https://ffmpeg.org/download.html")
    print("    OR place ffmpeg.exe next to this script.")
    print()
    print("  After installing, re-run this script.")
    sys.exit(1)


# ── Path auto-detection (Windows) ────────────────────────────────────────────

def _steam_userdata_dirs():
    """
    Yield all Steam userdata/<uid> directories found on this Windows machine.
    Checks registry first, then common install locations.
    Scans all numeric user-ID subdirs so no Steam ID is hard-coded.
    """
    steam_roots = []

    # Registry — most reliable
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (
                r"SOFTWARE\WOW6432Node\Valve\Steam",
                r"SOFTWARE\Valve\Steam",
            ):
                try:
                    key = winreg.OpenKey(hive, subkey)
                    path, _ = winreg.QueryValueEx(key, "InstallPath")
                    steam_roots.append(Path(path))
                except Exception:
                    pass
    except ImportError:
        pass

    # Common fallback locations
    username = os.environ.get("USERNAME", "")
    steam_roots += [
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
        Path(f"C:/Users/{username}/AppData/Local/Steam"),
    ]

    seen = set()
    for root in steam_roots:
        userdata = root / "userdata"
        if not userdata.is_dir() or userdata in seen:
            continue
        seen.add(userdata)
        for uid_dir in sorted(userdata.iterdir()):
            if uid_dir.is_dir() and uid_dir.name.isdigit():
                yield uid_dir


def find_steam_recording_path():
    """Auto-detect Steam's gamerecordings folder on Windows."""
    for uid_dir in _steam_userdata_dirs():
        gr = uid_dir / "gamerecordings"
        if not gr.is_dir():
            continue
        if any(gr.glob("*/*/session.mpd")) or any(gr.glob("*/*/*/session.mpd")) \
                or any(gr.glob("*/timeline_*.json")) or any(gr.glob("timelines/timeline_*.json")):
            return gr
    return None


# ── Timeline parsing ──────────────────────────────────────────────────────────


# Fallback display-name lookup for maps not seen in timeline tags.
# code → display name
CS2_MAP_NAMES = {
    # Active Duty
    "de_ancient":   "Ancient",
    "de_anubis":    "Anubis",
    "de_dust2":     "Dust II",
    "de_inferno":   "Inferno",
    "de_mirage":    "Mirage",
    "de_nuke":      "Nuke",
    "de_overpass":  "Overpass",
    # Reserve / Competitive
    "de_vertigo":   "Vertigo",
    "de_train":     "Train",
    "de_golden":    "Golden",
    "de_basalt":    "Basalt",
    "de_edin":      "Edin",
    "de_palacio":   "Palacio",
    "de_cache":     "Cache",
    "de_cobblestone": "Cobblestone",
    # Hostage
    "cs_office":    "Office",
    "cs_italy":     "Italy",
    "cs_assault":   "Assault",
    # Wingman-exclusive
    "de_stmarc":    "St. Marc",
    "de_lake":      "Lake",
    "de_safehouse": "Safehouse",
    "de_bank":      "Bank",
    "de_rooftop":   "Rooftop",
    "gd_rialto":    "Rialto",
    # Arms Race
    "ar_baggage":   "Baggage",
    "ar_shoots":    "Shoots",
    "ar_monastery": "Monastery",
    "ar_dizzy":     "Dizzy",
    # Misc / older
    "de_austria":   "Austria",
    "de_sugarcane": "Sugarcane",
    "de_biome":     "Biome",
}


def parse_map_intervals(json_path, timeline_offset_sec=0.0):
    """
    Return a sorted list of (time_sec, map_code, display_name) tuples from
    phase entries that carry a Map tag (e.g. icon="map_icon_de_ancient").
    Used to assign the correct map to each clip when a session spans multiple maps.
    """
    intervals = []
    try:
        with open(json_path, "rb") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))
    except Exception:
        return intervals

    raw_entries = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        time_val = entry.get("time")
        if time_val is None:
            continue
        for tag in entry.get("tags", []):
            if not isinstance(tag, dict):
                continue
            if tag.get("group", "").lower() == "map":
                icon = tag.get("icon", "")
                if icon.startswith("map_icon_"):
                    map_code = icon[len("map_icon_"):]
                    # Prefer the display name from the tag; fall back to lookup table
                    display_name = tag.get("name", "") or CS2_MAP_NAMES.get(map_code, map_code)
                    try:
                        time_sec = float(time_val) / 1000.0 - timeline_offset_sec
                        intervals.append((time_sec, map_code, display_name))
                    except (ValueError, TypeError):
                        pass
                    break  # one map tag per entry is enough

    return sorted(intervals)


def _map_at(intervals, time_sec):
    """Return (map_code, display_name) active at time_sec given sorted intervals."""
    result_code, result_name = "", ""
    for t, code, name in intervals:
        if t <= time_sec:
            result_code, result_name = code, name
        else:
            break
    return result_code, result_name


def parse_phase_times(json_path, timeline_offset_sec=0.0):
    """
    Return a sorted list of session-relative times (seconds) where a phase
    change occurred (round start, freezetime, warmup, etc.).
    These are used as hard round boundaries — kills on opposite sides of a
    boundary are never merged into the same group.
    """
    times = []
    try:
        with open(json_path, "rb") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))
    except Exception:
        return times

    raw_entries = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type", "") == "event":
            continue  # skip kill/game events — we only want phase transitions
        time_val = entry.get("time")
        if time_val is None:
            continue
        try:
            time_sec = float(time_val) / 1000.0 - timeline_offset_sec
            times.append(time_sec)
        except (ValueError, TypeError):
            continue

    return sorted(times)


def parse_timeline_json(json_path, timeline_offset_sec=0.0):
    """
    Parse a Steam timeline JSON file and return a list of events.
    timeline_offset_sec: subtract this from each event time so times become
    session-relative (seconds from session video start).
    Each event: { "time_sec": float, "type": str, "label": str }
    """
    events = []
    try:
        with open(json_path, "rb") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: Could not read {json_path.name}: {e}")
        return events

    raw_entries = []
    if isinstance(data, dict):
        raw_entries = data.get("entries", [])
    elif isinstance(data, list):
        raw_entries = data

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue

        # CS2/Steam format: only "event" type entries are game events;
        # "phase", "gamemode", "state_description" are metadata
        entry_type = entry.get("type", "")
        if entry_type != "event":
            continue

        # Time is in milliseconds in CS2 timeline files
        time_val = entry.get("time")
        if time_val is None:
            continue
        try:
            time_sec = float(time_val) / 1000.0 - timeline_offset_sec
        except (ValueError, TypeError):
            continue

        title = entry.get("title", "")
        icon = entry.get("icon", "")
        description = entry.get("description", "")

        events.append({
            "time_sec": time_sec,
            "type": icon.lower(),
            "label": title,
            "description": description,
            "raw": entry,
        })

    return events


def is_interesting(event):
    title = event.get("label", "").lower()
    icon = event.get("type", "").lower()  # stored as icon name

    # Kill: "You killed X" or kill icons
    if "you killed" in title or "kill" in icon:
        return True

    # Death: "You were killed by X"
    if INCLUDE_DEATH_HIGHLIGHTS:
        if "you were killed" in title or "death" in icon:
            return True

    return False


# ── MPD / chunk helpers ───────────────────────────────────────────────────────

def format_ts(seconds):
    td = timedelta(seconds=seconds)
    total_s = int(td.total_seconds())
    ms = int((seconds - int(seconds)) * 1000)
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _parse_pt_duration(pt_str):
    """Parse ISO 8601 duration like PT32M6.0S into seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", pt_str or "")
    if not m:
        return 0.0
    h = float(m.group(1) or 0)
    mins = float(m.group(2) or 0)
    s = float(m.group(3) or 0)
    return h * 3600 + mins * 60 + s


def parse_mpd_info(mpd_path):
    """Return (start_number, seg_duration_sec, total_duration_sec, period_start_sec) from session.mpd."""
    try:
        tree = ET.parse(mpd_path)
        root = tree.getroot()
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}

        total_dur = _parse_pt_duration(root.get("mediaPresentationDuration", ""))

        period = root.find(".//mpd:Period", ns)
        period_start = _parse_pt_duration(period.get("start", "PT0S")) if period is not None else 0.0

        seg = root.find(".//mpd:SegmentTemplate", ns)
        if seg is None:
            return 643, 3.0, total_dur, period_start

        start_number = int(seg.get("startNumber", 643))
        timescale = int(seg.get("timescale", 1000000))
        seg_dur = int(seg.get("duration", 3000000))
        seg_dur_sec = seg_dur / timescale
        return start_number, seg_dur_sec, total_dur, period_start
    except Exception as e:
        print(f"  WARNING: Could not parse MPD ({mpd_path.name}): {e}")
        return 643, 3.0, 0.0, 0.0


def export_clip(session_dir, start_sec, duration, output_path):
    """Export a clip by directly concatenating the relevant .m4s chunk files."""
    start_sec = max(0.0, start_sec)
    mpd_path = session_dir / "session.mpd"
    start_number, seg_dur, total_dur, period_start = parse_mpd_info(mpd_path)

    # Calculate which chunks cover [start_sec, start_sec+duration]
    first_idx = int(start_sec / seg_dur)
    last_idx = int((start_sec + duration) / seg_dur) + 1
    first_chunk = start_number + first_idx
    last_chunk = start_number + last_idx

    # offset within the first chunk (0 to seg_dur seconds)
    offset_in_chunk = start_sec - first_idx * seg_dur

    def chunk_files(stream_id):
        init = session_dir / f"init-stream{stream_id}.m4s"
        files = [str(init)] if init.exists() else []
        for n in range(first_chunk, last_chunk + 1):
            p = session_dir / f"chunk-stream{stream_id}-{n:05d}.m4s"
            if p.exists():
                files.append(str(p))
        return files

    v_files = chunk_files(0)
    a_files = chunk_files(1)

    if len(v_files) < 2:  # only init (or nothing), no actual chunks found
        available = sorted(session_dir.glob("chunk-stream0-*.m4s"))
        hint = ""
        if available:
            lo = int(available[0].stem.split("-")[-1])
            hi = int(available[-1].stem.split("-")[-1])
            hint = f" (available chunks: {lo}–{hi}, requested: {first_chunk}–{last_chunk})"
        print(f"    WARNING: No video chunks found for this time range.{hint}")
        print(f"    The event may be outside the circular buffer's retained window.")
        return False

    v_concat = "|".join(v_files)
    a_concat = "|".join(a_files)

    # Chunks carry absolute PTS (e.g. ~1926s). We reset PTS to 0 with setpts/asetpts,
    # then use trim/atrim to cut exactly offset_in_chunk seconds from the start.
    vf = f"setpts=PTS-STARTPTS,trim=start={offset_in_chunk:.6f}:duration={duration:.6f},setpts=PTS-STARTPTS"
    af = f"asetpts=PTS-STARTPTS,atrim=start={offset_in_chunk:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS"

    cmd = [FFMPEG_BIN, "-y"]
    cmd += ["-i", f"concat:{v_concat}"]
    if len(a_files) >= 2:
        cmd += ["-i", f"concat:{a_concat}"]
        cmd += ["-vf", vf, "-af", af]
    else:
        # No audio chunks — inject a silent stereo track so the output always
        # has an audio stream (required for seamless concat filter during merge).
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000"]
        cmd += ["-vf", vf, "-af", f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"]
    if GPU_ENCODER == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4",
                "-rc:v", "vbr", "-cq:v", "23"]
    elif GPU_ENCODER == "h264_amf":
        cmd += ["-c:v", "h264_amf", "-quality", "speed",
                "-qp_i", "23", "-qp_p", "23", "-qp_b", "23"]
    elif GPU_ENCODER == "h264_qsv":
        cmd += ["-c:v", "h264_qsv", "-preset", "faster",
                "-global_quality", "23"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    cmd += ["-c:a", "aac", "-shortest", "-movflags", "+faststart"]
    cmd += [str(output_path)]

    if not FFMPEG_BIN:
        print("    ERROR: ffmpeg is not installed. Cannot export clips.")
        print("    Run:  winget install Gyan.FFmpeg")
        return False

    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(cmd, capture_output=True, timeout=300,
                                creationflags=no_window)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")
            # Extract the most useful line from the ffmpeg error output
            error_lines = [l for l in err.splitlines() if "Error" in l or "Invalid" in l or "No such" in l]
            short_err = error_lines[-1] if error_lines else err[-300:]
            print(f"    WARNING: ffmpeg error: {short_err}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("    WARNING: ffmpeg timed out after 5 minutes. Skipping this clip.")
        return False
    except (FileNotFoundError, TypeError):
        print("    ERROR: ffmpeg not found. Run:  winget install Gyan.FFmpeg")
        return False


def extract_thumbnail(session_dir, clip_start, clip_duration, out_name, thumbnail_dir):
    """Extract a single JPEG frame at the clip midpoint from the raw DASH recording."""
    if not FFMPEG_BIN:
        return None
    midpoint = clip_start + clip_duration / 2
    mpd_path = session_dir / "session.mpd"
    start_number, seg_dur, _, _ = parse_mpd_info(mpd_path)
    chunk_idx = int(midpoint / seg_dur)
    chunk_num = start_number + chunk_idx
    offset_in_chunk = midpoint - chunk_idx * seg_dur

    init  = session_dir / "init-stream0.m4s"
    chunk = session_dir / f"chunk-stream0-{chunk_num:05d}.m4s"
    if not chunk.exists():
        return None

    files  = ([str(init)] if init.exists() else []) + [str(chunk)]
    concat = "|".join(files)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumbnail_dir / f"{Path(out_name).stem}.jpg"

    vf = (f"setpts=PTS-STARTPTS,"
          f"trim=start={offset_in_chunk:.3f}:duration=0.5,"
          f"setpts=PTS-STARTPTS,scale=320:180")
    cmd = [FFMPEG_BIN, "-y",
           "-i", f"concat:{concat}",
           "-vf", vf,
           "-vframes", "1",
           "-q:v", "4",
           str(thumb_path)]

    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(cmd, capture_output=True, timeout=30,
                                creationflags=no_window)
        if result.returncode == 0 and thumb_path.exists():
            return thumb_path
    except Exception:
        pass
    return None


# ── Session processing ────────────────────────────────────────────────────────

def _parse_datetime_from_name(name):
    """Extract datetime from a filename containing YYYYMMDD_HHMMSS."""
    m = re.search(r"(\d{8})_(\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def _is_kill_event(event):
    label = (event.get("type", "") + " " + event.get("label", "")).lower()
    return any(kw in label for kw in KILL_EVENTS)


def _is_self_kill(event):
    """Return True for 'You killed yourself' — should never be grouped as a highlight."""
    return "yourself" in event.get("label", "").lower()


def _round_index(t, phase_times):
    """Return which round interval time t falls into (0-based)."""
    for i, boundary in enumerate(phase_times):
        if t < boundary:
            return i
    return len(phase_times)


def _parse_session_groups(session_dir):
    """
    Shared internal: parse a session directory and return (groups, error).
    groups is a list of raw group dicts with keys:
        events, start, end, is_multikill, kill_count
    Returns (None, reason_str) on any error condition.
    """
    mpd_path = session_dir / "session.mpd"
    if not mpd_path.exists():
        return None, "no_mpd"

    # 1. Look in the session dir itself (original layout)
    timeline_dir = session_dir / "timelines"
    json_files = list(timeline_dir.glob("timeline_*.json")) if timeline_dir.exists() else []
    json_files += list(session_dir.glob("timeline_*.json"))

    # 2. Fallback: look in gamerecordings/timelines/ (Steam's new layout)
    if not json_files:
        gr_timelines = session_dir.parent.parent / "timelines"
        if gr_timelines.exists():
            all_timelines = list(gr_timelines.glob("timeline_*.json"))
            session_dt = _parse_datetime_from_name(session_dir.name)
            if session_dt and all_timelines:
                def timeline_key(p):
                    dt = _parse_datetime_from_name(p.stem)
                    if dt is None:
                        return float("inf")
                    diff = (session_dt - dt).total_seconds()
                    if diff < -300:
                        return float("inf")
                    return abs(diff)
                best = min(all_timelines, key=timeline_key)
                if timeline_key(best) < float("inf"):
                    json_files = [best]

    if not json_files:
        print(f"  WARNING: No timeline JSON found for {session_dir.name}")
        print(f"           Expected in {session_dir / 'timelines'}")
        print(f"           or in {session_dir.parent.parent / 'timelines'}")
        return None, "no_timeline"

    _, _, session_duration, period_start = parse_mpd_info(session_dir / "session.mpd")

    if session_duration == 0:
        print(f"  WARNING: Could not read session duration for {session_dir.name}, skipping.")
        return None, "no_duration"

    session_dt = _parse_datetime_from_name(session_dir.name)
    jf = json_files[0]
    timeline_dt = _parse_datetime_from_name(jf.stem)

    filename_offset = 0.0
    if session_dt and timeline_dt:
        filename_offset = (session_dt - timeline_dt).total_seconds()
    elif not (session_dt and timeline_dt):
        print(f"  WARNING: Could not parse timestamps from filenames.")
        print(f"           Session: {session_dir.name}  |  Timeline: {jf.stem}")
        print(f"           Clip timing may be inaccurate.")

    timeline_offset_sec = filename_offset + period_start
    print(f"  Timeline offset: {timeline_offset_sec:.1f}s  "
          f"(filename_offset={filename_offset:.1f}s + period_start={period_start:.1f}s)")

    all_events = []
    phase_times = []
    map_intervals = []
    for jf in json_files:
        events = parse_timeline_json(jf, timeline_offset_sec)
        all_events.extend(events)
        phase_times.extend(parse_phase_times(jf, timeline_offset_sec))
        map_intervals.extend(parse_map_intervals(jf, timeline_offset_sec))
    phase_times = sorted(set(phase_times))
    map_intervals = sorted(map_intervals)

    if map_intervals:
        unique_maps = list(dict.fromkeys(name for _, _, name in map_intervals))
        print(f"  Maps: {', '.join(unique_maps)}")
    else:
        print(f"  Map: (not detected — check timeline entry types)")

    all_events = [e for e in all_events
                  if 0 <= e["time_sec"] <= session_duration]

    if not all_events:
        print(f"  No timeline events fall within the recording window for {session_dir.name}")
        print(f"  (Recording covers {timeline_offset_sec:.0f}s – "
              f"{timeline_offset_sec + session_duration:.0f}s of the timeline)")
        return None, "no_events"

    highlights = [e for e in all_events if is_interesting(e)]

    print(f"\nSession: {session_dir.name}")
    print(f"   Found {len(all_events)} total events, {len(highlights)} highlights, {len(phase_times)} round boundaries")

    if not highlights:
        print(f"   No kill/highlight events to extract.")
        return None, "no_highlights"

    # ── Grouping: one round = one clip ────────────────────────────────────────
    highlights.sort(key=lambda e: e["time_sec"])

    # Filter self-kills before grouping
    self_kills = [h for h in highlights if _is_self_kill(h)]
    highlights  = [h for h in highlights if not _is_self_kill(h)]
    for h in self_kills:
        ts = f"{int(h['time_sec']//60)}m{int(h['time_sec']%60):02d}s"
        print(f"     - skipped self-kill [{ts}] \"{h['label']}\"")

    groups = []

    if phase_times:
        # Round-based grouping: bucket every highlight by which round it falls in
        buckets = {}
        for h in highlights:
            r = _round_index(h["time_sec"], phase_times)
            buckets.setdefault(r, []).append(h)

        for r in sorted(buckets):
            evts = buckets[r]
            groups.append({
                "events": evts,
                "start":  evts[0]["time_sec"],
                "end":    evts[-1]["time_sec"],
                "is_multikill": False,
            })

        print(f"   Round-based grouping: {len(phase_times)} boundaries → {len(groups)} round group(s)")
    else:
        # Fallback when no phase data: merge window
        print(f"   No round boundaries found — using {MULTI_KILL_MERGE_WINDOW}s merge window fallback")
        for h in highlights:
            t   = h["time_sec"]
            ts  = f"{int(t//60)}m{int(t%60):02d}s"
            placed = False
            for group in reversed(groups):
                gap    = t - group["end"]
                window = MULTI_KILL_MERGE_WINDOW if (group["is_multikill"] or _is_kill_event(h)) else 15
                if gap <= window:
                    group["events"].append(h)
                    group["end"] = t
                    group["is_multikill"] = group["is_multikill"] or (
                        _is_kill_event(h) and any(_is_kill_event(e) for e in group["events"][:-1])
                    )
                    placed = True
                    print(f"     + merged  [{ts}] \"{h['label']}\"  gap={gap:.1f}s ≤ {window}s")
                    break
            if not placed:
                groups.append({"events": [h], "start": t, "end": t, "is_multikill": False})
                print(f"     + new grp [{ts}] \"{h['label']}\"" )

    for group in groups:
        kill_count = sum(1 for e in group["events"] if _is_kill_event(e))
        group["kill_count"] = kill_count
        group["is_multikill"] = kill_count >= 2
        map_code, map_display = _map_at(map_intervals, group["start"])
        group["map_name"]    = map_code
        group["map_display"] = map_display

    print(f"   Grouped into {len(groups)} clip(s) "
          f"({sum(1 for g in groups if g['is_multikill'])} multi-kill)\n")

    return groups, None


def scan_session_groups(session_dir, output_folder):
    """
    Parse a session directory and return a list of enriched group dicts.
    Returns None on any error (no mpd, no timeline, no events, etc.).

    Each group dict contains all base keys (events, start, end, is_multikill,
    kill_count) plus computed fields:
        pre_shift, clip_start, clip_end, clip_duration,
        tag, ts_label, out_name, out_path, session_dir
    """
    groups, error = _parse_session_groups(session_dir)
    if groups is None:
        return None

    enriched = []
    for i, group in enumerate(groups, 1):
        kill_count = group["kill_count"]
        pre_shift = KILL_EVENT_PRE_SHIFT if kill_count > 0 else 0
        clip_start = max(0.0, group["start"] - CLIP_PADDING_BEFORE - pre_shift)
        clip_end = group["end"] + CLIP_PADDING_AFTER + pre_shift
        clip_duration = clip_end - clip_start

        if group["is_multikill"]:
            tag = f"{kill_count}k"
        else:
            events = group["events"]
            raw_tag = events[0]["label"].encode("ascii", errors="replace").decode("ascii")
            tag = re.sub(r"[^\w\s-]", "", raw_tag)[:20].strip().replace(" ", "_")

        ts_label = f"{int(group['start']//60)}m{int(group['start']%60):02d}s"
        out_name = f"{session_dir.name}_{ts_label}_{tag}_{i}.mp4"
        out_path = output_folder / out_name

        enriched_group = dict(group)
        enriched_group["pre_shift"] = pre_shift
        enriched_group["clip_start"] = clip_start
        enriched_group["clip_end"] = clip_end
        enriched_group["clip_duration"] = clip_duration
        enriched_group["tag"] = tag
        enriched_group["ts_label"] = ts_label
        enriched_group["out_name"] = out_name
        enriched_group["out_path"] = out_path
        enriched_group["session_dir"] = session_dir
        enriched.append(enriched_group)

    return enriched


def merge_clips(clip_paths, output_path):
    """
    Concatenate a list of MP4 files into one seamless output file.
    Uses the ffmpeg concat filter (frame-accurate, no PTS drift between clips)
    with full re-encode. All input clips must have both video and audio streams
    — export_clip guarantees this by injecting silent audio when needed.
    Returns True on success, False on failure.
    """
    if not FFMPEG_BIN:
        print("  ERROR: ffmpeg not found.")
        return False

    n = len(clip_paths)
    if n < 2:
        print("  WARNING: merge requires at least 2 clips.")
        return False

    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        cmd = [FFMPEG_BIN, "-y"]
        for p in clip_paths:
            cmd += ["-i", str(p)]

        streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        # Pipe concat audio through aformat to normalise sample rate/layout
        # before AAC encoding — mixed rates (e.g. 44100 vs 48000) cause -22.
        filter_complex = (
            f"{streams}concat=n={n}:v=1:a=1[v][a_raw];"
            f"[a_raw]aformat=sample_rates=48000:channel_layouts=stereo:sample_fmts=fltp[a]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]"]

        # Always use libx264 for merge — GPU encoders (QSV/NVENC/AMF) require
        # hardware surface input and can't directly consume a software concat
        # filter graph, causing "Invalid argument" errors on some drivers.
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        cmd += ["-c:a", "aac", "-movflags", "+faststart", str(output_path)]

        result = subprocess.run(cmd, capture_output=True, timeout=600,
                                creationflags=no_window)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")
            error_lines = [l for l in err.splitlines()
                           if "Error" in l or "Invalid" in l or "No such" in l]
            short_err = error_lines[-1] if error_lines else err[-300:]
            print(f"  WARNING: merge error: {short_err}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("  WARNING: ffmpeg merge timed out.")
        return False
    except (FileNotFoundError, TypeError):
        print("  ERROR: ffmpeg not found.")
        return False


def _group_events_into_segments(events, threshold):
    """
    Split a sorted list of events into segments where the gap between
    consecutive events exceeds `threshold` seconds.
    Returns a list of lists (each inner list is one segment of events).
    """
    if not events or threshold <= 0:
        return [events]
    segments, current = [], [events[0]]
    for e in events[1:]:
        if e["time_sec"] - current[-1]["time_sec"] > threshold:
            segments.append(current)
            current = [e]
        else:
            current.append(e)
    segments.append(current)
    return segments


def export_single_group(group, stop_event=None, force=False):
    """
    Export one enriched group dict produced by scan_session_groups.

    If JUMP_CUT_THRESHOLD > 0 and any two consecutive events within the group
    are more than that many seconds apart, each segment is exported as a
    separate sub-clip and then jump-cut together into the final file.

    Returns:
        "skipped"  — output file already exists
        "stopped"  — stop_event was set before export started
        True       — export succeeded
        False      — export failed
    """
    if not force and group["out_path"].exists():
        return "skipped"
    if stop_event and stop_event.is_set():
        return "stopped"

    events = sorted(group["events"], key=lambda e: e["time_sec"])
    pre_shift = KILL_EVENT_PRE_SHIFT if group["kill_count"] > 0 else 0
    segments = _group_events_into_segments(events, JUMP_CUT_THRESHOLD)

    if len(segments) > 1:
        print(f"    Jump-cut: {group['kill_count']}K group split into {len(segments)} segment(s) "
              f"(threshold={JUMP_CUT_THRESHOLD}s):")
        for i, seg in enumerate(segments):
            times = ", ".join(f"{int(e['time_sec']//60)}m{int(e['time_sec']%60):02d}s" for e in seg)
            print(f"      seg {i+1}: [{times}]  ({len(seg)} kill(s))")

    if len(segments) == 1:
        # No jump cuts needed — single continuous clip
        return True if export_clip(
            group["session_dir"],
            group["clip_start"],
            group["clip_duration"],
            group["out_path"],
        ) else False

    # Multiple segments — export each to a temp file then merge
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="steamhl_"))
    tmp_clips = []
    try:
        for i, seg in enumerate(segments):
            if stop_event and stop_event.is_set():
                return "stopped"
            seg_start = max(0.0, seg[0]["time_sec"] - CLIP_PADDING_BEFORE - pre_shift)
            seg_end   = seg[-1]["time_sec"] + CLIP_PADDING_AFTER + pre_shift
            tmp_path  = tmp_dir / f"seg_{i:03d}.mp4"
            if not export_clip(group["session_dir"], seg_start, seg_end - seg_start, tmp_path):
                return False
            tmp_clips.append(tmp_path)

        print(f"    Jump-cutting {len(tmp_clips)} segments together…")
        return merge_clips(tmp_clips, group["out_path"])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_session(session_dir, output_folder, stop_event=None):
    mpd_path = session_dir / "session.mpd"
    if not mpd_path.exists():
        return

    groups, error = _parse_session_groups(session_dir)
    if groups is None:
        return

    output_folder.mkdir(parents=True, exist_ok=True)

    total_groups = len(groups)
    exported = 0
    skipped = 0
    failed = 0
    t_start = time.time()

    for i, group in enumerate(groups, 1):
        if stop_event and stop_event.is_set():
            print(f"\n  Stopped at clip {i}/{total_groups}. "
                  f"Resume will continue from here (already-saved clips are skipped).")
            return False  # interrupted

        # Enrich the group with computed fields (reuse scan_session_groups logic)
        kill_count = group["kill_count"]
        pre_shift = KILL_EVENT_PRE_SHIFT if kill_count > 0 else 0
        clip_start = max(0.0, group["start"] - CLIP_PADDING_BEFORE - pre_shift)
        clip_end   = group["end"] + CLIP_PADDING_AFTER + pre_shift
        clip_duration = clip_end - clip_start

        events = group["events"]
        if group["is_multikill"]:
            tag = f"{kill_count}k"
        else:
            raw_tag = events[0]["label"].encode("ascii", errors="replace").decode("ascii")
            tag = re.sub(r"[^\w\s-]", "", raw_tag)[:20].strip().replace(" ", "_")

        ts_label = f"{int(group['start']//60)}m{int(group['start']%60):02d}s"
        out_name = f"{session_dir.name}_{ts_label}_{tag}_{i}.mp4"
        out_path = output_folder / out_name

        # Progress + summary
        elapsed = time.time() - t_start
        eta_str = ""
        if i > 1 and exported > 0:
            avg = elapsed / exported
            remaining = (total_groups - i + 1) * avg
            eta_str = f"  ETA {remaining:.0f}s"

        event_summary = " > ".join(e["label"] for e in events)
        event_summary = event_summary.encode("ascii", errors="replace").decode("ascii")
        duration_str = f"{clip_duration:.0f}s"
        marker = "* MULTI-KILL" if group["is_multikill"] else "  highlight"
        print(f"  [{i}/{total_groups}] {marker}  {ts_label}  ({duration_str}){eta_str}  {event_summary}")

        if out_path.exists():
            print(f"       Already exported, skipping.")
            skipped += 1
            continue

        success = export_clip(session_dir, clip_start, clip_duration, out_path)
        safe_name = out_name.encode("ascii", errors="replace").decode("ascii")
        if success:
            size_mb = out_path.stat().st_size / 1_000_000
            print(f"       Saved: {safe_name}  ({size_mb:.1f} MB)")
            exported += 1
        else:
            failed += 1
            print(f"       Failed: {safe_name}")

    # Session summary
    total_elapsed = time.time() - t_start
    print(f"\n   Session done in {total_elapsed:.0f}s — "
          f"{exported} exported, {skipped} skipped, {failed} failed.\n")
    return True  # completed fully


def find_all_sessions(root):
    """
    Find all session directories under root.
    Steam's structure is always root/<game_id>/<session>/session.mpd,
    so we search exactly 2 and 3 levels deep instead of rglob-ing everything.
    """
    seen = set()
    sessions = []
    # Covers: root/session/session.mpd  (flat)
    #         root/game_id/session/session.mpd  (standard Steam layout)
    #         root/game_id/sub/session/session.mpd  (occasional extra nesting)
    for pattern in ("*/session.mpd", "*/*/session.mpd", "*/*/*/session.mpd"):
        for p in root.glob(pattern):
            if "clips" in p.parts:
                continue
            session_dir = p.parent
            if session_dir not in seen:
                seen.add(session_dir)
                sessions.append(session_dir)
    return sessions


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Steam Highlight Extractor  —  Timeline Edition")
    print("=" * 60)

    check_ffmpeg()
    print(f"  ffmpeg: {FFMPEG_BIN}")

    recording_root = Path(STEAM_RECORDING_PATH) if STEAM_RECORDING_PATH else find_steam_recording_path()

    if not recording_root or not recording_root.exists():
        print("\nERROR: Could not find Steam game recording folder.")
        print()
        print("  Make sure Game Recording is enabled in Steam:")
        print("    Steam → Settings → Game Recording")
        print()
        print("  Then either:")
        print("    A) Set STEAM_RECORDING_PATH at the top of this script, OR")
        print("    B) Make sure Steam is installed in a standard location.")
        print()
        print("  Expected folder structure:")
        print("    <gamerecordings>/")
        print("      timelines/timeline_*.json")
        print("      video/bg_<appid>_<date>/session.mpd")
        sys.exit(1)

    print(f"\n  Scanning:  {recording_root}")
    print(f"  Output:    {OUTPUT_FOLDER}")
    print(f"  Clip window: -{CLIP_PADDING_BEFORE}s / +{CLIP_PADDING_AFTER}s  |  "
          f"kill pre-shift: -{KILL_EVENT_PRE_SHIFT}s")
    print()

    sessions = find_all_sessions(recording_root)

    if not sessions:
        print("ERROR: No background recording sessions found.")
        print("  Make sure Background Recording is enabled in Steam settings.")
        print(f"  Looking for session.mpd files under: {recording_root}")
        sys.exit(1)

    print(f"Found {len(sessions)} recording session(s).\n")

    total_start = time.time()
    for session in sorted(sessions):
        try:
            process_session(session, OUTPUT_FOLDER)
        except Exception as e:
            print(f"  ERROR processing {session.name}: {e}")
            print(f"  Skipping this session and continuing.\n")

    total_elapsed = time.time() - total_start
    print(f"{'=' * 60}")
    print(f"  All done in {total_elapsed:.0f}s.")
    print(f"  Highlights saved to: {OUTPUT_FOLDER}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
