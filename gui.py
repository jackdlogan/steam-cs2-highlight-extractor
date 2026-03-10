"""
Steam Highlight Extractor — GUI
================================
Run this file:  python gui.py
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path

import steam_highlight_extractor as core

VERSION = "1.0.0"


def _resource(filename):
    """
    Resolve path to a bundled resource file.
    Works both when running from source and when packaged with PyInstaller
    (PyInstaller extracts bundled files to sys._MEIPASS at runtime).
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / filename


# ── Stdout → queue bridge ─────────────────────────────────────────────────────

class _QueueWriter:
    def __init__(self, q):
        self.queue = q
    def write(self, text):
        self.queue.put(text)
    def flush(self):
        pass


# ── Main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Steam Highlight Extractor  v{VERSION}")
        self.minsize(740, 580)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)   # log row expands

        # App icon — .ico for the window/taskbar, PNG fallback for iconphoto
        try:
            self.iconbitmap(str(_resource("app-icon.ico")))
        except Exception:
            try:
                icon = tk.PhotoImage(file=str(_resource("app-icon.png")))
                self.iconphoto(True, icon)
            except Exception:
                pass  # no icon is fine

        self._log_queue        = queue.Queue()
        self._running          = False
        self._sessions         = []   # list of Path
        self._stop_event       = threading.Event()
        self._resume_from      = 0    # session index to resume from
        self._selected_cache   = []   # sessions selected at last Extract/Resume click

        self._build_ui()
        self._auto_detect()
        self._check_ffmpeg_startup()
        self._poll_log()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=10, pady=5)

        # ── Paths ──────────────────────────────────────────────────────────
        pf = ttk.LabelFrame(self, text="Paths", padding=8)
        pf.grid(row=0, column=0, sticky="ew", **pad)
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="Recording path:").grid(row=0, column=0, sticky="w", pady=2)
        self.var_recording = tk.StringVar()
        ttk.Entry(pf, textvariable=self.var_recording).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(pf, text="Browse…", width=8,
                   command=self._browse_recording).grid(row=0, column=2)

        ttk.Label(pf, text="Output folder:").grid(row=1, column=0, sticky="w", pady=2)
        self.var_output = tk.StringVar(value=str(core.OUTPUT_FOLDER))
        ttk.Entry(pf, textvariable=self.var_output).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(pf, text="Browse…", width=8,
                   command=self._browse_output).grid(row=1, column=2)

        ttk.Button(pf, text="↻  Scan sessions",
                   command=self._scan_sessions).grid(row=2, column=1, sticky="e", pady=(6, 0))

        # ── Settings + Sessions (two columns) ──────────────────────────────
        mid = ttk.Frame(self)
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=0)
        mid.columnconfigure(1, weight=1)

        # Settings
        sf = ttk.LabelFrame(mid, text="Settings", padding=8)
        sf.grid(row=0, column=0, sticky="ns", padx=(0, 6))

        spin_fields = [
            ("Padding before (s):", "pad_before", core.CLIP_PADDING_BEFORE),
            ("Padding after (s):",  "pad_after",  core.CLIP_PADDING_AFTER),
            ("Kill pre-shift (s):", "pre_shift",  core.KILL_EVENT_PRE_SHIFT),
            ("Merge window (s):",   "merge_win",  core.MULTI_KILL_MERGE_WINDOW),
        ]
        self._sv = {}
        for r, (label, key, default) in enumerate(spin_fields):
            ttk.Label(sf, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = tk.IntVar(value=default)
            ttk.Spinbox(sf, from_=0, to=300, width=5,
                        textvariable=v).grid(row=r, column=1, padx=(8, 0))
            self._sv[key] = v

        ttk.Separator(sf, orient="horizontal").grid(
            row=len(spin_fields), column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(sf, text="Extract:").grid(
            row=len(spin_fields)+1, column=0, columnspan=2, sticky="w")
        self.chk_kills  = tk.BooleanVar(value=True)
        self.chk_deaths = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text="Kills",  variable=self.chk_kills).grid(
            row=len(spin_fields)+2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(sf, text="Deaths", variable=self.chk_deaths).grid(
            row=len(spin_fields)+3, column=0, columnspan=2, sticky="w")

        # Sessions
        sesf = ttk.LabelFrame(mid, text="Sessions  (select which to process)", padding=8)
        sesf.grid(row=0, column=1, sticky="nsew")
        sesf.columnconfigure(0, weight=1)
        sesf.rowconfigure(0, weight=1)

        self._listbox = tk.Listbox(sesf, selectmode=tk.MULTIPLE,
                                   height=8, activestyle="none",
                                   font=("Consolas", 9))
        self._listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sesf, command=self._listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._listbox.configure(yscrollcommand=sb.set)

        br = ttk.Frame(sesf)
        br.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(br, text="All",    width=7, command=self._select_all).pack(side="left", padx=2)
        ttk.Button(br, text="Latest", width=7, command=self._select_latest).pack(side="left")

        # ── Log ────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(self, text="Log", padding=4)
        lf.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._log = scrolledtext.ScrolledText(
            lf, height=10, state="disabled", wrap="word",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white")
        self._log.grid(row=0, column=0, sticky="nsew")

        # Colour tags for log
        self._log.tag_config("ok",   foreground="#4ec9b0")
        self._log.tag_config("warn", foreground="#ce9178")
        self._log.tag_config("err",  foreground="#f44747")
        self._log.tag_config("info", foreground="#9cdcfe")

        # ── Progress ───────────────────────────────────────────────────────
        self._progress = ttk.Progressbar(self, mode="determinate")
        self._progress.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 4))

        # ── Footer buttons ─────────────────────────────────────────────────
        ft = ttk.Frame(self, padding=(10, 4))
        ft.grid(row=4, column=0, sticky="ew")

        self._btn_extract = ttk.Button(ft, text="▶  Extract Highlights",
                                       command=self._on_extract_btn)
        self._btn_extract.pack(side="left")

        self._btn_stop = ttk.Button(ft, text="⏹  Stop",
                                    command=self._request_stop, state="disabled")
        self._btn_stop.pack(side="left", padx=6)

        ttk.Button(ft, text="📂  Open Output Folder",
                   command=self._open_output).pack(side="left", padx=8)

        self._var_status = tk.StringVar(value="Ready.")
        ttk.Label(ft, textvariable=self._var_status,
                  foreground="gray").pack(side="right")

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _auto_detect(self):
        path = core.find_steam_recording_path()
        if path:
            self.var_recording.set(str(path))
            self._scan_sessions()
        else:
            self._log_append("  Could not auto-detect Steam recording folder.\n"
                             "  Set the path manually and click ↻ Scan sessions.\n", "warn")

    def _check_ffmpeg_startup(self):
        if core.FFMPEG_BIN:
            self._log_append(f"  ffmpeg: {core.FFMPEG_BIN}\n", "ok")
        else:
            # Disable the Extract button and show a prominent warning
            self._btn_extract.config(state="disabled")
            self._var_status.set("⚠ ffmpeg not found — install it to extract clips")
            self._log_append(
                "  ⚠  ffmpeg not found — clips cannot be exported.\n"
                "\n"
                "  Install it using one of these options:\n"
                "    1. Open a terminal and run:  winget install ffmpeg\n"
                "    2. Download from https://ffmpeg.org/download.html\n"
                "       and add ffmpeg.exe to your PATH\n"
                "    3. Place ffmpeg.exe in the same folder as this app\n"
                "\n"
                "  Then restart the app.\n",
                "warn",
            )

    def _browse_recording(self):
        d = filedialog.askdirectory(title="Select Steam gamerecordings folder")
        if d:
            self.var_recording.set(d)
            self._scan_sessions()

    def _browse_output(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.var_output.set(d)

    def _scan_sessions(self):
        root = Path(self.var_recording.get())
        self._listbox.delete(0, tk.END)
        self._sessions = []
        if not root.exists():
            return
        self._sessions = sorted(core.find_all_sessions(root))
        for s in self._sessions:
            self._listbox.insert(tk.END, s.name)
        self._select_latest()
        self._var_status.set(f"{len(self._sessions)} session(s) found.")

    def _select_all(self):
        self._listbox.select_set(0, tk.END)

    def _select_latest(self):
        self._listbox.select_clear(0, tk.END)
        if self._sessions:
            self._listbox.select_set(tk.END)
            self._listbox.see(tk.END)

    def _open_output(self):
        out = Path(self.var_output.get())
        out.mkdir(parents=True, exist_ok=True)
        os.startfile(str(out))

    # ── Extraction ────────────────────────────────────────────────────────────

    def _patch_core(self):
        """Apply GUI settings to the core module before running."""
        core.CLIP_PADDING_BEFORE     = self._sv["pad_before"].get()
        core.CLIP_PADDING_AFTER      = self._sv["pad_after"].get()
        core.KILL_EVENT_PRE_SHIFT    = self._sv["pre_shift"].get()
        core.MULTI_KILL_MERGE_WINDOW = self._sv["merge_win"].get()

        want_kills  = self.chk_kills.get()
        want_deaths = self.chk_deaths.get()

        def patched_is_interesting(event):
            title = event.get("label", "").lower()
            icon  = event.get("type",  "").lower()
            if want_kills  and ("you killed" in title or "kill" in icon):
                return True
            if want_deaths and ("you were killed" in title or icon == "cs2_death"):
                return True
            return False

        core.is_interesting = patched_is_interesting

    def _on_extract_btn(self):
        """Extract button — starts a fresh run (resets resume position)."""
        self._resume_from = 0
        self._selected_cache = []
        self._start_extract()

    def _request_stop(self):
        """Stop button — signals the worker to stop after the current clip."""
        self._stop_event.set()
        self._btn_stop.config(state="disabled")
        self._var_status.set("Stopping after current clip…")

    def _start_extract(self):
        if self._running:
            return

        # On a fresh start use the listbox selection; on resume reuse the cache
        if self._resume_from == 0:
            indices = self._listbox.curselection()
            if not indices:
                messagebox.showwarning("Nothing selected",
                                       "Select at least one session from the list.")
                return
            self._selected_cache = [self._sessions[i] for i in indices]

        if not core.FFMPEG_BIN:
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg.exe was not found on PATH or next to this script.\n\n"
                "Install it:\n  winget install ffmpeg\n"
                "or download from https://ffmpeg.org/download.html")
            return

        selected   = self._selected_cache
        output_dir = Path(self.var_output.get())
        self._patch_core()
        self._stop_event.clear()

        if self._resume_from == 0:
            self._log_clear()

        self._running = True
        self._btn_extract.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._var_status.set("Extracting…")
        self._progress.config(maximum=len(selected))

        def _run():
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = _QueueWriter(self._log_queue)
            stopped_at = None
            try:
                for idx, session in enumerate(selected):
                    if idx < self._resume_from:
                        continue  # skip already-completed sessions
                    if self._stop_event.is_set():
                        stopped_at = idx
                        break
                    self.after(0, lambda v=idx: self._progress.config(value=v))
                    try:
                        completed = core.process_session(
                            session, output_dir, stop_event=self._stop_event)
                    except Exception as exc:
                        print(f"\nERROR processing {session.name}: {exc}\n")
                        completed = True  # don't retry on hard error
                    if completed is False:
                        # Stopped mid-session — resume should retry this session
                        stopped_at = idx
                        break

                if stopped_at is None:
                    self._progress.config(value=len(selected))
                    print("\n✓  All done.")
                    self.after(0, lambda: self._finish(done=True))
                else:
                    self.after(0, lambda s=stopped_at: self._finish(done=False, stopped_at=s))
            finally:
                sys.stdout, sys.stderr = old_out, old_err

        threading.Thread(target=_run, daemon=True).start()

    def _finish(self, done=True, stopped_at=0):
        self._running = False
        self._btn_stop.config(state="disabled")

        if done:
            self._resume_from = 0
            self._selected_cache = []
            self._btn_extract.config(text="▶  Extract Highlights",
                                     command=self._on_extract_btn, state="normal")
            self._var_status.set("Done.")
        else:
            self._resume_from = stopped_at
            self._btn_extract.config(text="▶  Resume",
                                     command=self._start_extract, state="normal")
            self._var_status.set(f"Stopped. Click Resume to continue.")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_clear(self):
        self._log.config(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.config(state="disabled")

    def _log_append(self, text, tag=None):
        self._log.config(state="normal")
        self._log.insert(tk.END, text, tag or "")
        self._log.see(tk.END)
        self._log.config(state="disabled")

    def _poll_log(self):
        """Drain the queue and colour-code lines."""
        try:
            while True:
                text = self._log_queue.get_nowait()
                tl = text.lower()
                if "error" in tl or "failed" in tl:
                    tag = "err"
                elif "warning" in tl or "warn" in tl or "skipping" in tl:
                    tag = "warn"
                elif "saved:" in tl or "done" in tl or "✓" in tl:
                    tag = "ok"
                elif text.startswith("  [") or "session:" in tl:
                    tag = "info"
                else:
                    tag = ""
                self._log_append(text, tag)
        except queue.Empty:
            pass
        self.after(50, self._poll_log)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
