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
from tkinter import filedialog, messagebox
from pathlib import Path

import customtkinter as ctk

import steam_highlight_extractor as core

VERSION = "1.1.0"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Colour palette ────────────────────────────────────────────────────────────
C_BG       = "#0f1117"   # main window background
C_SURFACE  = "#161b27"   # sidebar / panel background
C_SURFACE2 = "#1e2433"   # slightly lighter surface (hover, selected rows)
C_BORDER   = "#2a3347"   # subtle borders
C_BLUE     = "#4f9eff"   # accent blue
C_GREEN    = "#00e5a0"   # accent green (success/done)
C_RED      = "#ff6b6b"   # kill red
C_TEXT     = "#e2e8f0"   # primary text
C_MUTED    = "#94a3b8"   # muted / secondary text — AA contrast ≥4.5:1 on all surfaces
C_BTN      = "#1e2433"   # button background
C_BTN_HOV  = "#2a3347"   # button hover

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_MONO = "IBM Plex Mono"
FONT_SANS = "IBM Plex Sans"


def _font(family, size, weight="normal"):
    return ctk.CTkFont(family=family, size=size, weight=weight)


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

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Steam Highlight Extractor  v{VERSION}")
        self.minsize(860, 620)
        self.configure(fg_color=C_BG)

        # Two-column grid, full height
        self.columnconfigure(0, weight=0, minsize=220)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)   # content row
        self.rowconfigure(1, weight=0)   # bottom action bar
        # row=2 is the full-width progress bar (3px tall, no weight needed)

        # App icon
        try:
            self.iconbitmap(str(_resource("app-icon.ico")))
        except Exception:
            try:
                icon = tk.PhotoImage(file=str(_resource("app-icon.png")))
                self.iconphoto(True, icon)
            except Exception:
                pass

        self._log_queue  = queue.Queue()
        self._running    = False
        self._sessions   = []        # list of Path
        self._session_vars    = []   # list of tk.BooleanVar
        self._session_widgets = []   # list of CTkFrame
        self._session_bullet_labels = []  # list of CTkLabel (bullets)
        self._stop_event = threading.Event()
        self._kill_groups = []       # list of dicts: {"group", "var", "row_frame", "accent_bar"}
        self._sidebar_section_count = 0

        self._build_ui()
        self._auto_detect()
        self._check_ffmpeg_startup()
        self._poll_log()

    # ── Sidebar section helper ────────────────────────────────────────────────

    def _sidebar_section(self, parent, title):
        if self._sidebar_section_count > 0:
            ctk.CTkFrame(parent, fg_color=C_BORDER, height=1, corner_radius=0).pack(
                fill="x", padx=0, pady=(6, 0)
            )
        self._sidebar_section_count += 1
        ctk.CTkLabel(
            parent,
            text=title.upper(),
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(4, 2))

    # ── Status helper ─────────────────────────────────────────────────────────

    def _set_status(self, text, color=None):
        self._var_status.set(text)
        self._lbl_status.configure(text_color=color or C_GREEN)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── LEFT SIDEBAR ──────────────────────────────────────────────────────
        sidebar = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)

        # ── Section 1: PATHS ──────────────────────────────────────────────────
        self._sidebar_section(sidebar, "PATHS")

        for (lbl_text, var_attr, default_val, browse_cmd) in [
            ("Recording path", "var_recording", None,                   self._browse_recording),
            ("Output folder",  "var_output",    str(core.OUTPUT_FOLDER), self._browse_output),
        ]:
            ctk.CTkLabel(
                sidebar,
                text=lbl_text,
                font=_font(FONT_MONO, 12),
                text_color=C_MUTED,
                anchor="w",
            ).pack(fill="x", padx=10, pady=(2, 0))

            row = ctk.CTkFrame(sidebar, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(0, 4))
            row.columnconfigure(0, weight=1)

            var = tk.StringVar(value=default_val or "")
            setattr(self, var_attr, var)

            entry = ctk.CTkEntry(
                row,
                textvariable=var,
                state="readonly",
                fg_color=C_BG,
                border_color=C_BORDER,
                text_color=C_TEXT,
                font=_font(FONT_MONO, 11),
                height=28,
            )
            entry.grid(row=0, column=0, sticky="ew", padx=(0, 2))

            ctk.CTkButton(
                row,
                text="…",
                width=28,
                height=28,
                fg_color=C_BTN,
                hover_color=C_BTN_HOV,
                font=_font(FONT_SANS, 12),
                command=browse_cmd,
            ).grid(row=0, column=1)

        # ── Section 2: CLIP SETTINGS ──────────────────────────────────────────
        self._sidebar_section(sidebar, "CLIP SETTINGS")

        spin_fields = [
            ("Padding before (s)", "pad_before", core.CLIP_PADDING_BEFORE),
            ("Padding after (s)",  "pad_after",  core.CLIP_PADDING_AFTER),
            ("Kill pre-shift (s)", "pre_shift",  core.KILL_EVENT_PRE_SHIFT),
            ("Merge window (s)",   "merge_win",  core.MULTI_KILL_MERGE_WINDOW),
        ]
        self._sv = {}
        for (label, key, default) in spin_fields:
            row = ctk.CTkFrame(sidebar, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)

            ctk.CTkLabel(
                row,
                text=label,
                font=_font(FONT_MONO, 12),
                text_color=C_TEXT,
                anchor="w",
            ).pack(side="left", expand=True, fill="x")

            v = tk.IntVar(value=default)
            ctk.CTkEntry(
                row,
                textvariable=v,
                width=48,
                height=26,
                fg_color="#152035",
                border_color=C_BLUE,
                text_color=C_BLUE,
                font=_font(FONT_MONO, 13, "bold"),
                justify="center",
            ).pack(side="right")
            self._sv[key] = v

        # ── Section 3: EXTRACT ────────────────────────────────────────────────
        self._sidebar_section(sidebar, "EXTRACT")

        self.chk_kills  = tk.BooleanVar(value=True)
        self.chk_deaths = tk.BooleanVar(value=False)

        ctk.CTkCheckBox(
            sidebar,
            text="Kills",
            variable=self.chk_kills,
            text_color=C_RED,
            fg_color=C_BLUE,
            border_color=C_BORDER,
            font=_font(FONT_MONO, 12),
        ).pack(anchor="w", padx=10, pady=2)

        ctk.CTkCheckBox(
            sidebar,
            text="Deaths",
            variable=self.chk_deaths,
            text_color=C_MUTED,
            fg_color=C_BLUE,
            border_color=C_BORDER,
            font=_font(FONT_MONO, 12),
        ).pack(anchor="w", padx=10, pady=2)

        # ── Section 4: SESSIONS ───────────────────────────────────────────────
        self._sidebar_section(sidebar, "SESSIONS")

        self._session_scroll = ctk.CTkScrollableFrame(
            sidebar,
            fg_color=C_BG,
            corner_radius=4,
            height=160,
        )
        self._session_scroll.pack(fill="x", padx=10, pady=(0, 4))

        # Session action buttons row
        btn_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkButton(
            btn_row,
            text="All",
            height=30,
            fg_color=C_BTN,
            text_color=C_TEXT,
            font=_font(FONT_SANS, 12),
            hover_color=C_BTN_HOV,
            command=self._select_all,
        ).pack(side="left", expand=True, fill="x")

        ctk.CTkButton(
            btn_row,
            text="Latest",
            height=30,
            fg_color=C_BTN,
            text_color=C_TEXT,
            font=_font(FONT_SANS, 12),
            hover_color=C_BTN_HOV,
            command=self._select_latest,
        ).pack(side="left", expand=True, fill="x")

        ctk.CTkButton(
            btn_row,
            text="↻ Refresh",
            height=30,
            fg_color=C_BLUE,
            text_color="#000000",
            font=_font(FONT_SANS, 12, "bold"),
            hover_color=C_BTN_HOV,
            command=self._scan_sessions,
        ).pack(side="left", expand=True, fill="x")

        # ── RIGHT MAIN AREA ───────────────────────────────────────────────────
        main_right = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        main_right.grid(row=0, column=1, sticky="nsew")
        main_right.columnconfigure(0, weight=1)
        main_right.rowconfigure(0, weight=0)   # kill feed header
        main_right.rowconfigure(1, weight=1)   # kill feed rows (expands)
        main_right.rowconfigure(2, weight=0)   # log label
        main_right.rowconfigure(3, weight=0)   # log (fixed height)

        # ── Kill Feed Header (row=0) ───────────────────────────────────────────
        kf_header = ctk.CTkFrame(main_right, fg_color="transparent")
        kf_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        ctk.CTkLabel(
            kf_header,
            text="KILL FEED",
            font=_font(FONT_MONO, 13, "bold"),
            text_color=C_TEXT,
        ).pack(side="left")

        self._lbl_highlights = ctk.CTkLabel(
            kf_header,
            text="",
            fg_color=C_BLUE,
            corner_radius=10,
            text_color="#000000",
            font=_font(FONT_MONO, 11, "bold"),
            padx=8,
            pady=1,
        )
        self._lbl_highlights.pack(side="left", padx=(8, 4))

        self._lbl_multikill = ctk.CTkLabel(
            kf_header,
            text="",
            fg_color=C_RED,
            corner_radius=10,
            text_color="#000000",
            font=_font(FONT_MONO, 11, "bold"),
            padx=8,
            pady=1,
        )
        self._lbl_multikill.pack(side="left", padx=(0, 8))

        self._lbl_selected = ctk.CTkLabel(
            kf_header,
            text="",
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
        )
        self._lbl_selected.pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            kf_header,
            text="✗ None",
            width=70,
            height=26,
            fg_color=C_BTN,
            hover_color=C_BTN_HOV,
            font=_font(FONT_SANS, 11),
            text_color=C_TEXT,
            command=self._killfeed_select_none,
        ).pack(side="right")

        ctk.CTkButton(
            kf_header,
            text="✓ All",
            width=70,
            height=26,
            fg_color=C_BTN,
            hover_color=C_BTN_HOV,
            font=_font(FONT_SANS, 11),
            text_color=C_TEXT,
            command=self._killfeed_select_all,
        ).pack(side="right", padx=(0, 4))

        # ── Kill Feed Rows (row=1) ─────────────────────────────────────────────
        self._killfeed_scroll = ctk.CTkScrollableFrame(
            main_right,
            fg_color=C_BG,
            corner_radius=0,
        )
        self._killfeed_scroll.grid(row=1, column=0, sticky="nsew", padx=0)

        # Empty state label (same grid cell as _killfeed_scroll)
        self._killfeed_empty = ctk.CTkLabel(
            main_right,
            text="Select a session and click  ⊙ Scan Kill Feed  to load highlights",
            font=_font(FONT_MONO, 12),
            text_color=C_MUTED,
        )
        self._killfeed_empty.grid(row=1, column=0)

        # ── Log label (row=2) ─────────────────────────────────────────────────
        ctk.CTkLabel(
            main_right,
            text="LOG",
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
        ).grid(row=2, column=0, sticky="w", padx=14, pady=(6, 2))

        # ── Log (row=3) ───────────────────────────────────────────────────────
        log_frame = ctk.CTkFrame(main_right, fg_color=C_SURFACE, corner_radius=0)
        log_frame.grid(row=3, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)

        self._log = tk.Text(
            log_frame,
            height=8,
            font=(FONT_MONO, 11),
            bg=C_SURFACE,
            fg=C_TEXT,
            bd=0,
            highlightthickness=0,
            state="disabled",
            wrap="word",
            padx=10,
            pady=6,
        )
        log_sb = tk.Scrollbar(
            log_frame,
            command=self._log.yview,
            bg=C_SURFACE,
            troughcolor=C_BG,
            borderwidth=0,
        )
        self._log.configure(yscrollcommand=log_sb.set)
        self._log.grid(row=0, column=0, sticky="ew")
        log_sb.grid(row=0, column=1, sticky="ns")

        # Log colour tags
        self._log.tag_config("ok",   foreground=C_GREEN)
        self._log.tag_config("warn", foreground="#f59e0b")
        self._log.tag_config("err",  foreground=C_RED)
        self._log.tag_config("info", foreground=C_BLUE)

        # ── BOTTOM ACTION BAR (row=1, spans both columns) ─────────────────────
        bar = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=0, height=52)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)

        # Left side buttons
        self._btn_scan = ctk.CTkButton(
            bar,
            text="⊙  Scan Kill Feed",
            fg_color=C_BTN,
            hover_color=C_BTN_HOV,
            text_color=C_TEXT,
            font=_font(FONT_SANS, 12),
            height=32,
            width=140,
            command=self._on_scan_btn,
        )
        self._btn_scan.pack(side="left", padx=10, pady=10)

        self._btn_export = ctk.CTkButton(
            bar,
            text="↓  Export Selected",
            fg_color=C_BLUE,
            hover_color=C_BTN_HOV,
            text_color="#000000",
            font=_font(FONT_SANS, 12, "bold"),
            height=32,
            width=148,
            state="disabled",
            command=self._on_export_btn,
        )
        self._btn_export.pack(side="left", padx=(6, 0), pady=10)

        self._btn_stop = ctk.CTkButton(
            bar,
            text="■  Stop",
            fg_color=C_BTN,
            hover_color=C_BTN_HOV,
            text_color=C_TEXT,
            font=_font(FONT_SANS, 12),
            height=32,
            width=80,
            state="disabled",
            command=self._request_stop,
        )
        self._btn_stop.pack(side="left", padx=(6, 0), pady=10)

        ctk.CTkButton(
            bar,
            text="📁  Open Output",
            fg_color=C_BTN,
            hover_color=C_BTN_HOV,
            text_color=C_TEXT,
            font=_font(FONT_SANS, 12),
            height=32,
            width=130,
            command=self._open_output,
        ).pack(side="left", padx=(6, 0), pady=10)

        self.chk_merge = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            bar,
            text="Merge into one clip",
            variable=self.chk_merge,
            fg_color=C_BLUE,
            border_color=C_BORDER,
            text_color=C_MUTED,
            font=_font(FONT_SANS, 11),
        ).pack(side="left", padx=(14, 0), pady=10)

        # Right side: status label
        self._var_status = tk.StringVar(value="Ready.")
        self._lbl_status = ctk.CTkLabel(
            bar,
            textvariable=self._var_status,
            font=_font(FONT_MONO, 12),
            text_color=C_GREEN,
        )
        self._lbl_status.pack(side="right", padx=14, pady=10)

        # ── Full-width progress bar (row=2, spans both columns) ───────────────
        self._progress = ctk.CTkProgressBar(
            self,
            height=3,
            fg_color=C_SURFACE,
            progress_color=C_BLUE,
            corner_radius=0,
        )
        self._progress.set(0)
        self._progress.grid(row=2, column=0, columnspan=2, sticky="ew")

        # Initial state: hide scroll, show empty state; status = Ready
        self._killfeed_scroll.grid_remove()
        self._killfeed_empty.grid()
        self._set_status("Ready.", C_MUTED)

    # ── Session helpers ───────────────────────────────────────────────────────

    def _toggle_session(self, i):
        self._session_vars[i].set(not self._session_vars[i].get())
        self._refresh_session_item(i)

    def _refresh_session_item(self, i):
        selected = self._session_vars[i].get()
        self._session_widgets[i].configure(fg_color=C_SURFACE2 if selected else "transparent")
        self._session_bullet_labels[i].configure(text_color=C_BLUE if selected else C_MUTED)

    def _get_selected_sessions(self):
        return [self._sessions[i] for i, v in enumerate(self._session_vars) if v.get()]

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _auto_detect(self):
        path = core.find_steam_recording_path()
        if path:
            self.var_recording.set(str(path))
            self._scan_sessions()
        else:
            self._log_append(
                "  Could not auto-detect Steam recording folder.\n"
                "  Set the path manually and click Scan.\n",
                "warn",
            )

    def _check_ffmpeg_startup(self):
        if core.FFMPEG_BIN:
            self._log_append(f"  ffmpeg: {core.FFMPEG_BIN}\n", "ok")
        else:
            self._btn_scan.configure(state="disabled")
            self._set_status("⚠ ffmpeg not found — install it to extract clips", "#f59e0b")
            self._log_append(
                "  ⚠  ffmpeg not found — clips cannot be exported.\n"
                "\n"
                "  Install it using one of these options:\n"
                "    1. Open a terminal and run:  winget install Gyan.FFmpeg\n"
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

        # Clear existing session widgets
        for widget in self._session_scroll.winfo_children():
            widget.destroy()
        self._sessions = []
        self._session_vars = []
        self._session_widgets = []
        self._session_bullet_labels = []

        if not root.exists():
            return

        self._sessions = sorted(core.find_all_sessions(root))

        for i, s in enumerate(self._sessions):
            item = ctk.CTkFrame(
                self._session_scroll,
                fg_color="transparent",
                corner_radius=3,
                cursor="hand2",
            )
            item.pack(fill="x", padx=4, pady=1)

            bullet = ctk.CTkLabel(
                item,
                text="●",
                font=_font(FONT_MONO, 12),
                text_color=C_MUTED,
                width=16,
            )
            bullet.pack(side="left", padx=(6, 2), pady=3)

            name_lbl = ctk.CTkLabel(
                item,
                text=s.name,
                font=_font(FONT_MONO, 11),
                text_color=C_TEXT,
                anchor="w",
            )
            name_lbl.pack(side="left", fill="x", expand=True, padx=(0, 6))

            for w in (item, bullet, name_lbl):
                w.bind("<Button-1>", lambda e, idx=i: self._toggle_session(idx))

            self._session_vars.append(tk.BooleanVar(value=False))
            self._session_widgets.append(item)
            self._session_bullet_labels.append(bullet)

        self._select_latest()
        self._set_status(f"{len(self._sessions)} session(s) found.", C_MUTED)

    def _select_all(self):
        for i in range(len(self._sessions)):
            self._session_vars[i].set(True)
            self._refresh_session_item(i)

    def _select_latest(self):
        for i in range(len(self._sessions)):
            self._session_vars[i].set(False)
            self._refresh_session_item(i)
        if self._sessions:
            self._session_vars[-1].set(True)
            self._refresh_session_item(len(self._sessions) - 1)

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

    def _request_stop(self):
        """Stop button — signals the worker to stop after the current clip."""
        self._stop_event.set()
        self._btn_stop.configure(state="disabled")
        self._set_status("Stopping after current clip…", "#f59e0b")

    # ── Selected count badge ──────────────────────────────────────────────────

    def _update_selected_count(self):
        total = len(self._kill_groups)
        checked = sum(1 for kg in self._kill_groups if kg["var"].get())
        if total > 0:
            self._lbl_selected.configure(text=f"{checked} / {total} selected")
        else:
            self._lbl_selected.configure(text="")

    # ── Scan flow ─────────────────────────────────────────────────────────────

    def _on_scan_btn(self):
        """Scan button — reads timeline data for selected sessions and populates kill feed."""
        indices = [i for i, v in enumerate(self._session_vars) if v.get()]
        if not indices:
            messagebox.showwarning("Nothing selected",
                                   "Select at least one session from the list.")
            return

        selected = [self._sessions[i] for i in indices]
        output_dir = Path(self.var_output.get())

        # Clear existing kill feed data and UI
        self._kill_groups = []
        for widget in self._killfeed_scroll.winfo_children():
            widget.destroy()

        # Show scroll frame, hide empty state
        self._killfeed_empty.grid_remove()
        self._killfeed_scroll.grid()

        self._log_clear()
        self._btn_scan.configure(state="disabled")
        self._btn_export.configure(state="disabled")
        self._set_status("Scanning…", C_BLUE)
        self._progress.set(0)

        def _run():
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = _QueueWriter(self._log_queue)
            try:
                for session in selected:
                    try:
                        groups = core.scan_session_groups(session, output_dir)
                    except Exception as exc:
                        print(f"\nERROR scanning {session.name}: {exc}\n")
                        groups = None
                    if groups is None:
                        continue
                    for group in groups:
                        self.after(0, lambda g=group: self._add_killfeed_row(g))
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            self.after(0, self._on_scan_done)

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_row_bg(self, frame, var, accent_bar=None):
        checked = var.get()
        frame.configure(fg_color=C_SURFACE2 if checked else "transparent")
        if accent_bar:
            accent_bar.configure(fg_color=C_BLUE if checked else "transparent")
        self._update_selected_count()

    def _add_killfeed_row(self, group):
        """Add one kill feed row to the scrollable panel and register it in self._kill_groups."""
        var = tk.BooleanVar(value=True)

        row_frame = ctk.CTkFrame(
            self._killfeed_scroll,
            fg_color="transparent",
            corner_radius=0,
            height=30,
        )
        row_frame.pack(fill="x", pady=0)

        # Separator line
        ctk.CTkFrame(
            self._killfeed_scroll,
            fg_color=C_BORDER,
            height=1,
            corner_radius=0,
        ).pack(fill="x")

        # Left accent bar (first child of row_frame)
        accent_bar = ctk.CTkFrame(row_frame, width=3, fg_color="transparent", corner_radius=0)
        accent_bar.pack(side="left", fill="y", padx=(0, 4))

        # 1. Checkbox
        ctk.CTkCheckBox(
            row_frame,
            width=20,
            text="",
            variable=var,
            fg_color=C_BLUE,
            border_color=C_BORDER,
            checkmark_color="#000000",
            command=lambda f=row_frame, v=var, a=accent_bar: self._refresh_row_bg(f, v, a),
        ).pack(side="left", padx=(10, 4), pady=2)

        # 2. Session name (first 18 chars)
        session_name = group["session_dir"].name[:18]
        ctk.CTkLabel(
            row_frame,
            text=session_name,
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
            width=120,
            anchor="w",
        ).pack(side="left")

        # 3. Timestamp
        ctk.CTkLabel(
            row_frame,
            text=group["ts_label"],
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
            width=60,
        ).pack(side="left")

        # 4. Kill badge
        kill_count = group["kill_count"]
        if kill_count >= 5:
            badge_text  = "ACE"
            badge_fg    = "#f0c040"
            badge_tc    = "#000000"
        elif kill_count == 4:
            badge_text  = "4K"
            badge_fg    = "#e07840"
            badge_tc    = "#000000"
        elif kill_count == 3:
            badge_text  = "3K"
            badge_fg    = "#d4a030"
            badge_tc    = "#000000"
        elif kill_count == 2:
            badge_text  = "2K"
            badge_fg    = C_BLUE
            badge_tc    = "#000000"
        else:
            badge_text  = "KILL"
            badge_fg    = C_RED
            badge_tc    = "#000000"

        ctk.CTkLabel(
            row_frame,
            text=badge_text,
            fg_color=badge_fg,
            text_color=badge_tc,
            corner_radius=4,
            font=_font(FONT_MONO, 11, "bold"),
            width=36,
            height=18,
        ).pack(side="left", padx=(0, 6))

        # 5. Event summary (truncated to 55 chars)
        raw_summary = " › ".join(e["label"] for e in group["events"])
        if len(raw_summary) > 55:
            raw_summary = raw_summary[:52] + "..."
        ctk.CTkLabel(
            row_frame,
            text=raw_summary,
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
        ).pack(side="left", padx=(0, 4))

        # 6. Duration label (right-aligned)
        ctk.CTkLabel(
            row_frame,
            text=f"{group['clip_duration']:.0f}s",
            font=_font(FONT_MONO, 11),
            text_color=C_MUTED,
        ).pack(side="right", padx=(0, 14))

        # 7. Already-exported indicator
        if group.get("out_path") and Path(group["out_path"]).exists():
            ctk.CTkLabel(
                row_frame,
                text="✓",
                font=_font(FONT_MONO, 11, "bold"),
                text_color=C_GREEN,
                width=16,
            ).pack(side="right", padx=(0, 4))

        # Click row (non-checkbox children) to toggle
        def _toggle_row(e, v=var, f=row_frame, a=accent_bar):
            v.set(not v.get())
            self._refresh_row_bg(f, v, a)
        for child in row_frame.winfo_children():
            if not isinstance(child, ctk.CTkCheckBox):
                child.bind("<Button-1>", _toggle_row)
        row_frame.bind("<Button-1>", _toggle_row)

        self._kill_groups.append({
            "group": group,
            "var": var,
            "row_frame": row_frame,
            "accent_bar": accent_bar,
        })

        # Set initial highlight (checked by default)
        self._refresh_row_bg(row_frame, var, accent_bar)

    def _on_scan_done(self):
        """Called on the main thread after all sessions have been scanned."""
        count = len(self._kill_groups)
        self._set_status(f"{count} highlight(s) found — select and click Export", C_MUTED)
        self._btn_scan.configure(state="normal")
        if count > 0:
            self._btn_export.configure(state="normal")

        # Update pill badges
        total = len(self._kill_groups)
        mk = sum(1 for kg in self._kill_groups if kg["group"]["is_multikill"])
        self._lbl_highlights.configure(text=f"  {total} highlights  ")
        self._lbl_multikill.configure(text=f"  {mk} multi-kill  ")

        self._update_selected_count()

    def _killfeed_select_all(self):
        for kg in self._kill_groups:
            kg["var"].set(True)
            self._refresh_row_bg(kg["row_frame"], kg["var"], kg.get("accent_bar"))

    def _killfeed_select_none(self):
        for kg in self._kill_groups:
            kg["var"].set(False)
            self._refresh_row_bg(kg["row_frame"], kg["var"], kg.get("accent_bar"))

    # ── Export flow ───────────────────────────────────────────────────────────

    def _on_export_btn(self):
        """Export button — exports all checked groups from the kill feed."""
        checked = [kg["group"] for kg in self._kill_groups if kg["var"].get()]
        if not checked:
            messagebox.showwarning("Nothing selected",
                                   "Check at least one highlight in the kill feed.")
            return

        if not core.FFMPEG_BIN:
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg.exe was not found on PATH or next to this script.\n\n"
                "Install it:\n  winget install Gyan.FFmpeg\n"
                "or download from https://ffmpeg.org/download.html")
            return

        output_dir = Path(self.var_output.get())
        output_dir.mkdir(parents=True, exist_ok=True)

        self._patch_core()
        self._stop_event.clear()
        self._running = True
        self._btn_scan.configure(state="disabled")
        self._btn_export.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._set_status("Exporting…", C_BLUE)
        self._progress.set(0)

        total = len(checked)
        do_merge = self.chk_merge.get()

        def _run():
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = _QueueWriter(self._log_queue)
            exported_paths = []
            try:
                for i, group in enumerate(checked, 1):
                    if self._stop_event.is_set():
                        print(f"\n  Stopped at clip {i}/{total}.")
                        self.after(0, lambda: self._finish(done=False))
                        return
                    self.after(0, lambda v=i, t=total: self._progress.set(v / t))
                    try:
                        result = core.export_single_group(group, stop_event=self._stop_event)
                    except Exception as exc:
                        print(f"\nERROR exporting {group.get('out_name', '?')}: {exc}\n")
                        result = False

                    out_name = group.get("out_name", "?")
                    safe_name = out_name.encode("ascii", errors="replace").decode("ascii")
                    if result == "skipped":
                        print(f"  [{i}/{total}] Already exported, skipping: {safe_name}")
                        exported_paths.append(group["out_path"])
                    elif result == "stopped":
                        print(f"\n  Stopped at clip {i}/{total}.")
                        self.after(0, lambda: self._finish(done=False))
                        return
                    elif result is True:
                        out_path = group["out_path"]
                        exported_paths.append(out_path)
                        if out_path.exists():
                            size_mb = out_path.stat().st_size / 1_000_000
                            print(f"  [{i}/{total}] Saved: {safe_name}  ({size_mb:.1f} MB)")
                        else:
                            print(f"  [{i}/{total}] Saved: {safe_name}")
                    else:
                        print(f"  [{i}/{total}] Failed: {safe_name}")

                if do_merge and len(exported_paths) > 1:
                    from datetime import datetime
                    merged_name = f"highlights_merged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                    merged_path = output_dir / merged_name
                    print(f"\n  Merging {len(exported_paths)} clips into {merged_name}…")
                    self.after(0, lambda: self._set_status("Merging…", C_BLUE))
                    ok = core.merge_clips(exported_paths, merged_path)
                    if ok and merged_path.exists():
                        size_mb = merged_path.stat().st_size / 1_000_000
                        print(f"  ✓  Merged clip saved: {merged_name}  ({size_mb:.1f} MB)")
                    else:
                        print(f"  WARNING: Merge failed.")
                elif do_merge and len(exported_paths) <= 1:
                    print("  (Merge skipped — need at least 2 clips)")

                print("\n✓  All done.")
                self.after(0, lambda: self._finish(done=True))
            finally:
                sys.stdout, sys.stderr = old_out, old_err

        threading.Thread(target=_run, daemon=True).start()

    def _finish(self, done=True, stopped_at=0):
        self._running = False
        self._btn_stop.configure(state="disabled")
        self._btn_scan.configure(state="normal")

        if done:
            self._progress.set(1.0)
            self._set_status("Done.", C_GREEN)
            if self._kill_groups:
                self._btn_export.configure(state="normal")
        else:
            self._set_status("Stopped.", "#f59e0b")
            if self._kill_groups:
                self._btn_export.configure(state="normal")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.configure(state="disabled")

    def _log_append(self, text, tag=None):
        self._log.configure(state="normal")
        self._log.insert(tk.END, text, tag or "")
        self._log.see(tk.END)
        self._log.configure(state="disabled")

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
