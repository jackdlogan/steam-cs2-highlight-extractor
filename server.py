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
import time
import tempfile
import concurrent.futures
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import steam_highlight_extractor as core

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Steam Highlight Extractor", version="2.0.2")

_thumbnail_dir = Path(tempfile.gettempdir()) / "steam_hl_thumbs"

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
    pad_before:         int  = core.CLIP_PADDING_BEFORE
    pad_after:          int  = core.CLIP_PADDING_AFTER
    pre_shift:          int  = core.KILL_EVENT_PRE_SHIFT
    jump_cut_threshold: int  = core.JUMP_CUT_THRESHOLD
    extract_kills:      bool = True
    extract_deaths:     bool = False


class ScanRequest(BaseModel):
    session_names: list[str]
    config: Config


class ExportRequest(BaseModel):
    groups:   list[dict]      # serialised group dicts (out_path/session_dir as strings)
    config:   Config
    merge:    bool = False
    workers:  int  = 1
    quality:  str  = "medium"  # "high" | "medium" | "low"


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
    core.JUMP_CUT_THRESHOLD      = cfg.jump_cut_threshold

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
    if "thumbnail_file" in out:
        out["thumbnail_url"] = f"http://127.0.0.1:7847/api/thumbnail/{out['thumbnail_file']}"
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
        "gpu_encoder":  core.GPU_ENCODER,
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

                # ── Kill / death highlights ────────────────────────────
                try:
                    groups = core.scan_session_groups(session, output_dir)
                except Exception as exc:
                    q.put({"type": "log",
                           "text": f"\nERROR scanning {session.name}: {exc}\n",
                           "level": "err"})
                    groups = None
                if groups:
                    for group in groups:
                        group["session_name"] = session.name
                        thumb = core.extract_thumbnail(
                            group["session_dir"],
                            group["clip_start"],
                            group["clip_duration"],
                            group["out_name"],
                            _thumbnail_dir,
                        )
                        if thumb:
                            group["thumbnail_file"] = thumb.name
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
    # Re-anchor out_path to the current output folder (scan-time path may differ)
    for g in groups:
        g["out_path"] = output_dir / g["out_path"].name
    total      = len(groups)
    do_merge   = req.merge

    _apply_config(req.config)

    q: queue.Queue = queue.Queue()

    def _run():
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(q)
        exported_by_idx = {}  # i -> out_path, kept in original order for merge
        new_paths       = []  # only clips we just created (deleted after merge)
        workers_count   = max(1, min(req.workers, total))
        try:
            def _do_one(i, group):
                """Run one export in a thread pool worker."""
                out_name  = group.get("out_name", "?")
                safe_name = out_name.encode("ascii", errors="replace").decode("ascii")
                q.put({"type": "clip_start",
                       "index": i, "total": total, "name": safe_name,
                       "tag": group.get("tag", ""), "duration": group.get("clip_duration", 0)})
                t0 = time.time()
                try:
                    result = core.export_single_group(group, stop_event=_stop_event, force=do_merge, quality=req.quality)
                except Exception as exc:
                    return i, group, safe_name, False, round(time.time() - t0, 1), str(exc)
                return i, group, safe_name, result, round(time.time() - t0, 1), None

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers_count) as executor:
                futures = {executor.submit(_do_one, i, group): i
                           for i, group in enumerate(groups, 1)}

                completed = 0
                stopped   = False
                for future in concurrent.futures.as_completed(futures):
                    i, group, safe_name, result, elapsed, exc_msg = future.result()
                    completed += 1

                    if exc_msg:
                        q.put({"type": "log",
                               "text": f"\nERROR: {exc_msg}\n", "level": "err"})

                    q.put({"type": "progress",
                           "value":   completed / total,
                           "current": completed,
                           "total":   total})

                    if result == "skipped":
                        q.put({"type": "log",
                               "text":  f"  [{i}/{total}] Already exists: {safe_name}\n",
                               "level": "info"})
                        q.put({"type": "clip_done",
                               "index": i, "status": "skipped", "size_mb": 0, "elapsed": elapsed})
                        exported_by_idx[i] = group["out_path"]
                    elif result == "stopped":
                        q.put({"type": "clip_done",
                               "index": i, "status": "stopped", "size_mb": 0, "elapsed": elapsed})
                        stopped = True
                    elif result is True:
                        out_path = group["out_path"]
                        exported_by_idx[i] = out_path
                        new_paths.append(out_path)
                        size_mb = round(out_path.stat().st_size / 1_000_000, 1) if out_path.exists() else 0
                        label = "Rendered" if do_merge else "Saved"
                        q.put({"type": "log",
                               "text":  f"  [{i}/{total}] {label}: {safe_name}  ({size_mb} MB)\n",
                               "level": "ok" if not do_merge else "info"})
                        q.put({"type": "clip_done",
                               "index": i, "status": "ok", "size_mb": size_mb, "elapsed": elapsed})
                    else:
                        q.put({"type": "log",
                               "text":  f"  [{i}/{total}] Failed: {safe_name}\n",
                               "level": "err"})
                        q.put({"type": "clip_done",
                               "index": i, "status": "failed", "size_mb": 0, "elapsed": elapsed})

            if stopped:
                q.put({"type": "done", "stopped": True})
                return

            # Merge in original clip order
            exported_paths = [exported_by_idx[i] for i in sorted(exported_by_idx)]

            # Merge
            if do_merge and len(exported_paths) > 1:
                from datetime import datetime
                merged_name = f"highlights_merged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                merged_path = output_dir / merged_name
                q.put({"type": "merging", "count": len(exported_paths)})
                q.put({"type": "log",
                       "text":  f"\n  Merging {len(exported_paths)} clips into {merged_name}…\n",
                       "level": "info"})
                ok = core.merge_clips(exported_paths, merged_path, quality=req.quality)
                if ok and merged_path.exists():
                    size_mb = merged_path.stat().st_size / 1_000_000
                    q.put({"type": "log",
                           "text":  f"  ✓ Merged: {merged_name}  ({size_mb:.1f} MB)\n",
                           "level": "ok"})
                    # Delete individual clips that were just created for this merge
                    for p in new_paths:
                        try:
                            Path(p).unlink(missing_ok=True)
                        except Exception:
                            pass
                else:
                    q.put({"type": "log",
                           "text": "  WARNING: Merge failed — individual clips kept.\n",
                           "level": "warn"})
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


@app.get("/api/thumbnail/{filename}")
def get_thumbnail(filename: str):
    """Serve a generated thumbnail JPEG."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = _thumbnail_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/api/session-stream/{session_name}/{filename}")
def serve_session_file(session_name: str, filename: str, recording_path: str = Query(default="")):
    """
    Serve DASH manifest and segment files for in-app preview.
    Allows dash.js to stream a session directly without exporting first.

    For the .mpd manifest: rewrites relative segment URLs to absolute so that
    dash.js requests always include the recording_path query parameter (which
    would otherwise be stripped when resolving relative URLs).

    Security: session_name must not contain path traversal; filename is
    validated against the exact patterns Steam uses.
    """
    import re as _re
    from urllib.parse import quote
    if ".." in session_name or "/" in session_name or "\\" in session_name:
        raise HTTPException(status_code=400, detail="Invalid session name")
    if not _re.match(r"^(session\.mpd|init-stream\d+\.m4s|chunk-stream\d+-\d{5}\.m4s)$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    root = Path(recording_path) if recording_path else core.find_steam_recording_path()
    if not root or not root.exists():
        raise HTTPException(status_code=404, detail="Recording path not found")

    sessions = {s.name: s for s in core.find_all_sessions(root)}
    if session_name not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    file_path = sessions[session_name] / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if filename.endswith(".mpd"):
        # Rewrite relative segment URLs → absolute, baking in recording_path.
        # dash.js resolves relative URLs from the MPD base directory, which
        # strips the ?recording_path= query param — this patch fixes that.
        content = file_path.read_text(encoding="utf-8")
        base    = f"http://127.0.0.1:7847/api/session-stream/{quote(session_name, safe='')}"
        rp      = quote(recording_path, safe="")

        def _make_absolute(m):
            attr, val = m.group(1), m.group(2)
            if val.startswith("http"):  # already absolute, leave it
                return m.group(0)
            return f'{attr}="{base}/{val}?recording_path={rp}"'

        content = _re.sub(r'(media|initialization)="([^"]+\.m4s[^"]*)"', _make_absolute, content)
        from fastapi.responses import Response as _Resp
        return _Resp(content=content.encode("utf-8"), media_type="application/dash+xml")

    return FileResponse(str(file_path), media_type="video/mp4")


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
