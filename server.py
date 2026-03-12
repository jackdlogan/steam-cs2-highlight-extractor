"""
Steam Highlight Extractor — FastAPI Backend
============================================
Run:  python server.py
      (or: uvicorn server:app --port 7847)

All clip logic lives in steam_highlight_extractor.py — this file only
exposes it over HTTP + Server-Sent Events for the Tauri frontend.
"""

import os
import sys
import json
import queue
import threading
import asyncio
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import steam_highlight_extractor as core

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Steam Highlight Extractor", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global mutable state ──────────────────────────────────────────────────────

_stop_event: threading.Event = threading.Event()

# ── Pydantic models ───────────────────────────────────────────────────────────

class Config(BaseModel):
    recording_path: str = ""
    output_folder:  str = str(core.OUTPUT_FOLDER)
    pad_before:     int = core.CLIP_PADDING_BEFORE
    pad_after:      int = core.CLIP_PADDING_AFTER
    pre_shift:      int = core.KILL_EVENT_PRE_SHIFT
    merge_window:   int = core.MULTI_KILL_MERGE_WINDOW
    extract_kills:  bool = True
    extract_deaths: bool = False


class ScanRequest(BaseModel):
    session_names: list[str]
    config: Config


class ExportRequest(BaseModel):
    groups: list[dict]      # serialised group dicts (out_path/session_dir as strings)
    config: Config
    merge:  bool = False


# ── SSE helpers ───────────────────────────────────────────────────────────────

async def _stream_queue(q: queue.Queue) -> AsyncGenerator[str, None]:
    """
    Drain a thread-safe queue and yield SSE-formatted strings.
    Stops when a 'done' or 'error' event is received.
    """
    while True:
        try:
            event = q.get_nowait()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                break
        except queue.Empty:
            await asyncio.sleep(0.05)


class _QueueWriter:
    """Redirect sys.stdout/stderr into the SSE queue with colour tags."""
    def __init__(self, q: queue.Queue):
        self.queue = q

    def write(self, text: str):
        if not text.strip():
            return
        tl = text.lower()
        if "error" in tl or "failed" in tl:
            level = "err"
        elif "warning" in tl or "warn" in tl or "skipping" in tl:
            level = "warn"
        elif "saved:" in tl or "✓" in tl or "done" in tl:
            level = "ok"
        elif text.startswith("  [") or "session:" in tl:
            level = "info"
        else:
            level = ""
        self.queue.put({"type": "log", "text": text, "level": level})

    def flush(self):
        pass


# ── Config helpers ────────────────────────────────────────────────────────────

def _apply_config(cfg: Config):
    """Push GUI settings into the core module."""
    core.CLIP_PADDING_BEFORE     = cfg.pad_before
    core.CLIP_PADDING_AFTER      = cfg.pad_after
    core.KILL_EVENT_PRE_SHIFT    = cfg.pre_shift
    core.MULTI_KILL_MERGE_WINDOW = cfg.merge_window

    want_kills  = cfg.extract_kills
    want_deaths = cfg.extract_deaths

    def patched_is_interesting(event):
        title = event.get("label", "").lower()
        icon  = event.get("type",  "").lower()
        if want_kills  and ("you killed" in title or "kill" in icon):
            return True
        if want_deaths and ("you were killed" in title or icon == "cs2_death"):
            return True
        return False

    core.is_interesting = patched_is_interesting


# ── Serialisation ─────────────────────────────────────────────────────────────

def _serialize_group(group: dict) -> dict:
    """Make a group dict JSON-safe (Path → str)."""
    out = {}
    for k, v in group.items():
        out[k] = str(v) if isinstance(v, Path) else v
    return out


def _deserialize_group(group: dict) -> dict:
    """Restore Path objects from a JSON group dict."""
    g = dict(group)
    for key in ("out_path", "session_dir"):
        if key in g and isinstance(g[key], str):
            g[key] = Path(g[key])
    return g


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    """Health check + ffmpeg detection."""
    return {
        "ok":           True,
        "ffmpeg_path":  core.FFMPEG_BIN,
        "ffmpeg_found": bool(core.FFMPEG_BIN),
    }


@app.get("/api/config/defaults")
def get_defaults():
    """Return default config including auto-detected recording path."""
    recording_path = core.find_steam_recording_path()
    return Config(recording_path=str(recording_path) if recording_path else "")


@app.get("/api/sessions")
def list_sessions(recording_path: str = Query(default="")):
    """Return all session folder names found under recording_path."""
    root = Path(recording_path) if recording_path else core.find_steam_recording_path()
    if not root or not root.exists():
        return {"sessions": [], "recording_path": "", "error": "Path not found"}
    sessions = sorted(core.find_all_sessions(root))
    return {
        "sessions":       [s.name for s in sessions],
        "recording_path": str(root),
    }


@app.post("/api/scan")
def scan_sessions(req: ScanRequest):
    """
    Scan selected sessions for kill highlights.
    Streams SSE events:
      {"type": "log",      "text": "...", "level": "ok|info|warn|err"}
      {"type": "group",    "data": {...}}   ← one per highlight group found
      {"type": "progress", "value": 0..1}
      {"type": "done"}
    """
    recording_root = Path(req.config.recording_path)
    all_sessions   = sorted(core.find_all_sessions(recording_root))
    session_map    = {s.name: s for s in all_sessions}
    selected       = [session_map[n] for n in req.session_names if n in session_map]
    output_dir     = Path(req.config.output_folder)
    total          = len(selected)

    _apply_config(req.config)

    q: queue.Queue = queue.Queue()

    def _run():
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(q)
        try:
            for idx, session in enumerate(selected):
                q.put({"type": "progress", "value": idx / total if total else 0})
                try:
                    groups = core.scan_session_groups(session, output_dir)
                except Exception as exc:
                    q.put({"type": "log",
                           "text": f"\nERROR scanning {session.name}: {exc}\n",
                           "level": "err"})
                    groups = None
                if not groups:
                    continue
                for group in groups:
                    q.put({"type": "group", "data": _serialize_group(group)})
            q.put({"type": "progress", "value": 1.0})
            q.put({"type": "done"})
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    threading.Thread(target=_run, daemon=True).start()

    return StreamingResponse(
        _stream_queue(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/export")
def export_groups(req: ExportRequest):
    """
    Export selected groups.
    Streams SSE events:
      {"type": "log",      "text": "...", "level": "..."}
      {"type": "progress", "value": 0..1, "current": n, "total": n}
      {"type": "done",     "stopped": false}
    """
    global _stop_event
    _stop_event.clear()

    groups     = [_deserialize_group(g) for g in req.groups]
    output_dir = Path(req.config.output_folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    total      = len(groups)
    do_merge   = req.merge

    _apply_config(req.config)

    q: queue.Queue = queue.Queue()

    def _run():
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(q)
        exported_paths = []
        try:
            for i, group in enumerate(groups, 1):
                if _stop_event.is_set():
                    q.put({"type": "log",
                           "text": f"\n  Stopped at clip {i}/{total}.\n",
                           "level": "warn"})
                    q.put({"type": "done", "stopped": True})
                    return

                q.put({"type": "progress",
                       "value":   (i - 1) / total,
                       "current": i - 1,
                       "total":   total})

                try:
                    result = core.export_single_group(group, stop_event=_stop_event)
                except Exception as exc:
                    q.put({"type": "log",
                           "text": f"\nERROR: {exc}\n", "level": "err"})
                    result = False

                out_name  = group.get("out_name", "?")
                safe_name = out_name.encode("ascii", errors="replace").decode("ascii")

                if result == "skipped":
                    q.put({"type": "log",
                           "text":  f"  [{i}/{total}] Skipped: {safe_name}\n",
                           "level": "info"})
                    exported_paths.append(group["out_path"])
                elif result == "stopped":
                    q.put({"type": "done", "stopped": True})
                    return
                elif result is True:
                    out_path = group["out_path"]
                    exported_paths.append(out_path)
                    size_mb = out_path.stat().st_size / 1_000_000 if out_path.exists() else 0
                    q.put({"type": "log",
                           "text":  f"  [{i}/{total}] Saved: {safe_name}  ({size_mb:.1f} MB)\n",
                           "level": "ok"})
                else:
                    q.put({"type": "log",
                           "text":  f"  [{i}/{total}] Failed: {safe_name}\n",
                           "level": "err"})

            # Optional merge
            if do_merge and len(exported_paths) > 1:
                from datetime import datetime
                merged_name = f"highlights_merged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                merged_path = output_dir / merged_name
                q.put({"type": "log",
                       "text":  f"\n  Merging {len(exported_paths)} clips into {merged_name}…\n",
                       "level": "info"})
                ok = core.merge_clips(exported_paths, merged_path)
                if ok and merged_path.exists():
                    size_mb = merged_path.stat().st_size / 1_000_000
                    q.put({"type": "log",
                           "text":  f"  ✓ Merged: {merged_name}  ({size_mb:.1f} MB)\n",
                           "level": "ok"})
                else:
                    q.put({"type": "log",
                           "text": "  WARNING: Merge failed.\n", "level": "warn"})
            elif do_merge:
                q.put({"type": "log",
                       "text": "  (Merge skipped — need at least 2 clips)\n",
                       "level": "info"})

            q.put({"type": "progress", "value": 1.0, "current": total, "total": total})
            q.put({"type": "log", "text": "\n✓  All done.\n", "level": "ok"})
            q.put({"type": "done", "stopped": False})

        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    threading.Thread(target=_run, daemon=True).start()

    return StreamingResponse(
        _stream_queue(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/stop")
def stop():
    """Signal the active export to stop after the current clip."""
    _stop_event.set()
    return {"ok": True}


@app.post("/api/open-output")
def open_output(output_folder: str = Query(default="")):
    """Open the output folder in Windows Explorer."""
    path = Path(output_folder) if output_folder else core.OUTPUT_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    return {"ok": True}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("Steam Highlight Extractor — API server")
    print("  http://127.0.0.1:7847")
    print("  http://127.0.0.1:7847/docs  (Swagger UI)")
    print()
    uvicorn.run(app, host="127.0.0.1", port=7847, log_level="warning")
