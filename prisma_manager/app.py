import json
import os
import queue
import re
import urllib.parse
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tkinter import font as tkfont
from tkinter import messagebox, ttk, simpledialog

SETTINGS_PATH = Path(__file__).with_name("prisma-checker.settings.json")
DEFAULT_SETTINGS = {
    "root_dir": str(Path.cwd()),
    "patterns": ["prisma/schema.prisma"],
    "run_command": 'npx prisma@6 studio --schema "{{PathOfSchema}}" --browser none',
    "auto_open_browser": False,
}
URL_RE = re.compile(r"https?://[^\s]+")
ENV_RE = re.compile(r'url\s*=\s*env\(\s*"([^"]+)"\s*\)')
# Matches inline url = "file:./dev.db" or url = "file:../data/app.db" etc.
DB_FILE_RE = re.compile(r'url\s*=\s*"([^"]+)"')


# ── helpers ────────────────────────────────────────────────────────────────────


def normalize_settings(raw):
    run_command_value = raw.get(
        "run_command", raw.get("runCommand", DEFAULT_SETTINGS["run_command"])
    )
    auto_open_value = raw.get(
        "auto_open_browser",
        raw.get("autoOpenBrowser", DEFAULT_SETTINGS["auto_open_browser"]),
    )
    root_dir_value = raw.get(
        "root_dir", raw.get("rootDir", DEFAULT_SETTINGS["root_dir"])
    )
    patterns = raw.get("patterns", DEFAULT_SETTINGS["patterns"])
    if not isinstance(patterns, list):
        patterns = DEFAULT_SETTINGS["patterns"]
    patterns = [str(p).strip().replace("\\", "/") for p in patterns if str(p).strip()]
    if not patterns:
        patterns = DEFAULT_SETTINGS["patterns"]
    root_dir = str(root_dir_value).strip() or DEFAULT_SETTINGS["root_dir"]
    run_command = str(run_command_value).strip() or DEFAULT_SETTINGS["run_command"]
    return {
        "root_dir": root_dir,
        "patterns": patterns,
        "run_command": run_command,
        "auto_open_browser": bool(auto_open_value),
    }


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return normalize_settings(
                json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError):
            pass
    save_settings(DEFAULT_SETTINGS)
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    normalized = normalize_settings(settings)
    SETTINGS_PATH.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    return normalized


def parse_env_file(env_path):
    if not env_path.exists():
        return None
    values = {}
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            values[k] = v
    return values


def extract_datasource_env_vars(schema_path):
    content = schema_path.read_text(encoding="utf-8", errors="ignore")
    return sorted(set(ENV_RE.findall(content)))


def project_dir_from_pattern(schema_path, root_dir, pattern):
    part_count = max(len(PurePosixPath(pattern).parts) - 1, 0)
    project_dir = schema_path.parent
    for _ in range(part_count):
        if project_dir == Path(root_dir):
            break
        project_dir = project_dir.parent
    return project_dir


def matches_pattern(relative_path, pattern):
    nr = relative_path.replace("\\", "/").strip("/")
    np = pattern.replace("\\", "/").strip("/")
    if any(t in np for t in ("*", "?", "[")):
        return PurePosixPath(nr).match(np)
    return nr == np or nr.endswith(f"/{np}")


def format_duration(started_at):
    if not started_at:
        return "—"
    elapsed = max(0, int(time.time() - started_at))
    h, r = divmod(elapsed, 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02}"


def resolve_db_files(schema_path: Path, env_values: dict | None) -> list[Path]:
    """
    Parse the schema and return a list of candidate database file paths.
    Handles:
      - url = "file:./dev.db"           (inline SQLite)
      - url = env("DATABASE_URL")       (env var, resolved if .env is present)
    Only returns paths that actually exist on disk.
    """
    content = schema_path.read_text(encoding="utf-8", errors="ignore")
    candidates = set()

    # Collect inline file: URLs
    for m in DB_FILE_RE.finditer(content):
        raw = m.group(1)
        if raw.startswith("file:"):
            candidates.add(raw[5:])  # strip "file:"

    # Collect env-var URLs
    if env_values:
        for var_name in ENV_RE.findall(content):
            val = env_values.get(var_name, "")
            if val.startswith("file:"):
                candidates.add(val[5:])

    result = []
    schema_dir = schema_path.parent
    for raw_path in candidates:
        # Strip query string (e.g. ?schema=public)
        raw_path = raw_path.split("?")[0].strip()
        if not raw_path:
            continue
        resolved = (schema_dir / raw_path).resolve()
        if resolved.exists():
            result.append(resolved)
    return result


def get_datasource_url(schema_path: Path, env_values: dict | None) -> str | None:
    """Return the datasource URL string from the schema (inline or resolved from .env), or None."""
    content = schema_path.read_text(encoding="utf-8", errors="ignore")
    # env("VAR") references
    env_refs = ENV_RE.findall(content)
    if env_refs and env_values:
        # prefer the first var that exists in env_values
        for name in env_refs:
            if env_values.get(name):
                return env_values.get(name)

    # quoted url = "..."
    m = DB_FILE_RE.search(content)
    if m:
        return m.group(1)
    return None


# ── data classes ───────────────────────────────────────────────────────────────


@dataclass
class SchemaRecord:
    schema_path: Path
    schema_relative: str
    project_dir: Path
    project_relative: str
    matched_pattern: str
    env_path: Path
    env_exists: bool
    datasource_env_vars: list[dict]
    has_database_value: bool


@dataclass
class ProcessState:
    schema_key: str
    command: str
    cwd: str
    popen: subprocess.Popen
    started_at: float = field(default_factory=time.time)
    url: str | None = None
    output_lines: list[str] = field(default_factory=list)
    auto_opened: bool = False
    stop_requested: bool = False
    stop_in_progress: bool = False
    stdout_closed: bool = False

    def is_running(self):
        return self.popen.poll() is None

    def append_output(self, text):
        if text:
            self.output_lines.append(text.rstrip())
            self.output_lines = self.output_lines[-200:]

    def output_text(self):
        text = "\n".join(ln for ln in self.output_lines if ln)
        return text.strip() or "No output captured yet."


# ── main application ───────────────────────────────────────────────────────────


class PrismaCheckerApp:
    # palette
    BG = "#0f1117"
    SURFACE = "#161b27"
    SURFACE2 = "#1c2333"
    BORDER = "#2a3350"
    ACCENT = "#5b8dee"
    ACCENT2 = "#3ecf8e"
    DANGER = "#e05c6b"
    WARNING = "#f59e0b"
    TEXT = "#e2e8f0"
    TEXT_DIM = "#7a8ba8"
    TEXT_MUTE = "#4a5a78"
    GREEN_BG = "#0d2b1e"
    GREEN_FG = "#3ecf8e"
    RED_BG = "#2b0d12"
    RED_FG = "#e05c6b"
    HEADER_BG = "#131720"

    def __init__(self, root):
        self.root = root
        self.root.title("Prisma Studio Manager")
        self.root.geometry("1400x820")
        self.root.minsize(1100, 680)
        self.root.configure(bg=self.BG)

        self.settings = load_settings()
        self.schema_records = []
        self.schema_map = {}
        self.processes = {}
        self.message_queue = queue.Queue()
        self.selected_key = None
        self.scan_in_progress = False

        self._setup_fonts()
        self._setup_styles()
        self._build_ui()
        self.load_settings_into_form()
        self.scan_schemas()
        self.root.after(300, self._process_queue)
        self.root.after(1000, self._refresh_runtime_columns)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── fonts ──────────────────────────────────────────────────────────────────

    def _setup_fonts(self):
        self.f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.f_heading = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_body = tkfont.Font(family="Segoe UI", size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=9)
        self.f_label = tkfont.Font(family="Segoe UI", size=8)
        self.f_stat = tkfont.Font(family="Segoe UI", size=18, weight="bold")
        self.f_stat_lbl = tkfont.Font(family="Segoe UI", size=8)
        self.f_badge = tkfont.Font(family="Segoe UI", size=7, weight="bold")

    # ── ttk styles ─────────────────────────────────────────────────────────────

    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")

        s.configure(
            ".",
            background=self.BG,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            troughcolor=self.SURFACE,
            selectbackground=self.ACCENT,
            selectforeground="#ffffff",
        )

        # frames
        s.configure("App.TFrame", background=self.BG)
        s.configure("Card.TFrame", background=self.SURFACE)
        s.configure("Card2.TFrame", background=self.SURFACE2)
        s.configure("Header.TFrame", background=self.HEADER_BG)

        # labels
        for name, bg, fg, f in [
            ("Title.TLabel", self.HEADER_BG, self.TEXT, self.f_title),
            ("Sub.TLabel", self.HEADER_BG, self.TEXT_DIM, self.f_body),
            ("Card.TLabel", self.SURFACE, self.TEXT, self.f_body),
            ("CardMute.TLabel", self.SURFACE, self.TEXT_DIM, self.f_label),
            ("CardBold.TLabel", self.SURFACE, self.TEXT, self.f_heading),
            ("Card2.TLabel", self.SURFACE2, self.TEXT, self.f_body),
            ("Card2Mute.TLabel", self.SURFACE2, self.TEXT_DIM, self.f_label),
            ("Card2Bold.TLabel", self.SURFACE2, self.TEXT, self.f_heading),
            ("Stat.TLabel", self.SURFACE, self.ACCENT, self.f_stat),
            ("StatLbl.TLabel", self.SURFACE, self.TEXT_DIM, self.f_stat_lbl),
            ("Status.TLabel", self.SURFACE2, self.TEXT_DIM, self.f_label),
        ]:
            s.configure(name, background=bg, foreground=fg, font=f)

        # buttons
        s.configure(
            "Accent.TButton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            borderwidth=0,
            focuscolor="none",
            padding=(14, 8),
            font=self.f_heading,
        )
        s.map(
            "Accent.TButton",
            background=[("pressed", "#3a6dce"), ("active", self.ACCENT), ("!disabled", "#2a3555"), ("disabled", self.SURFACE2)],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("!disabled", self.TEXT), ("disabled", self.TEXT_DIM)],
        )
        s.configure(
            "Ghost.TButton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            borderwidth=0,
            focuscolor="none",
            padding=(12, 7),
            font=self.f_body,
        )
        s.map(
            "Ghost.TButton",
            background=[("pressed", self.SURFACE), ("active", self.BORDER), ("!disabled", "#252d3a"), ("disabled", self.SURFACE2)],
            foreground=[("active", self.TEXT), ("pressed", self.TEXT), ("!disabled", self.TEXT_DIM), ("disabled", self.TEXT_DIM)],
        )
        s.configure(
            "Danger.TButton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            borderwidth=0,
            focuscolor="none",
            padding=(12, 7),
            font=self.f_body,
        )
        s.map(
            "Danger.TButton",
            background=[("pressed", "#b03040"), ("active", "#c94a58"), ("!disabled", "#5a2a30"), ("disabled", self.SURFACE2)],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("!disabled", self.TEXT), ("disabled", self.TEXT_DIM)],
        )
        s.configure(
            "DeleteDB.TButton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            borderwidth=0,
            focuscolor="none",
            padding=(12, 7),
            font=self.f_body,
        )
        s.map(
            "DeleteDB.TButton",
            background=[("pressed", "#c0392b"), ("active", "#9b3e30"), ("!disabled", "#5a3a30"), ("disabled", self.SURFACE2)],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("!disabled", self.TEXT), ("disabled", self.TEXT_DIM)],
        )
        s.configure(
            "Success.TButton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            borderwidth=0,
            focuscolor="none",
            padding=(12, 7),
            font=self.f_body,
        )
        s.map(
            "Success.TButton",
            background=[("pressed", "#22a868"), ("active", self.ACCENT2), ("!disabled", "#2a5a3a"), ("disabled", self.SURFACE2)],
            foreground=[("active", "#0d2b1e"), ("pressed", "#0d2b1e"), ("!disabled", self.TEXT), ("disabled", self.TEXT_DIM)],
        )

        # treeview
        s.configure(
            "Prisma.Treeview",
            background=self.SURFACE,
            fieldbackground=self.SURFACE,
            foreground=self.TEXT,
            rowheight=32,
            borderwidth=0,
            font=self.f_body,
        )
        s.configure(
            "Prisma.Treeview.Heading",
            background=self.HEADER_BG,
            foreground=self.TEXT_DIM,
            font=self.f_label,
            relief="flat",
            borderwidth=0,
            padding=(8, 6),
        )
        s.map(
            "Prisma.Treeview",
            background=[("selected", "#1e2e4a")],
            foreground=[("selected", self.TEXT)],
        )

        # entry / text
        s.configure(
            "TEntry",
            fieldbackground=self.SURFACE2,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            insertcolor=self.TEXT,
            padding=(8, 6),
        )

        # scrollbar
        s.configure(
            "Vertical.TScrollbar",
            background=self.SURFACE2,
            troughcolor=self.SURFACE,
            borderwidth=0,
            arrowcolor=self.TEXT_MUTE,
        )
        s.map(
            "Vertical.TScrollbar",
            background=[("active", self.BORDER)],
        )

        # checkbutton
        s.configure(
            "TCheckbutton",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            font=self.f_body,
            focuscolor="none",
        )
        s.map(
            "TCheckbutton",
            foreground=[("active", self.TEXT)],
        )

        # labelframe
        s.configure(
            "TLabelframe",
            background=self.SURFACE2,
            bordercolor=self.BORDER,
            relief="flat",
        )
        s.configure(
            "TLabelframe.Label",
            background=self.SURFACE2,
            foreground=self.TEXT_DIM,
            font=self.f_label,
        )

        # paned window sash
        s.configure("TPanedwindow", background=self.BG)
        s.configure("Sash", sashrelief="flat", sashpad=2)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_header()
        self._build_body()

    def _build_header(self):
        hdr = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 14))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(1, weight=1)

        # logo / title group
        title_group = ttk.Frame(hdr, style="Header.TFrame")
        title_group.grid(row=0, column=0, sticky="w")

        # "P" badge
        badge_canvas = tk.Canvas(
            title_group, width=34, height=34, bg=self.HEADER_BG, highlightthickness=0
        )
        badge_canvas.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        badge_canvas.create_oval(2, 2, 32, 32, fill=self.ACCENT, outline="")
        badge_canvas.create_text(
            17,
            18,
            text="P",
            fill="#fff",
            font=tkfont.Font(family="Segoe UI", size=13, weight="bold"),
        )

        ttk.Label(title_group, text="Prisma Studio Manager", style="Title.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(
            title_group,
            text="Scan directories · verify datasource env · launch Prisma Studio",
            style="Sub.TLabel",
        ).grid(row=1, column=1, sticky="nw")

        # right side controls
        ctrl = ttk.Frame(hdr, style="Header.TFrame")
        ctrl.grid(row=0, column=2, sticky="e")

        tk.Label(
            ctrl,
            text="Root directory",
            bg=self.HEADER_BG,
            fg=self.TEXT_DIM,
            font=self.f_label,
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.root_dir_var = tk.StringVar()
        root_entry = tk.Entry(
            ctrl,
            textvariable=self.root_dir_var,
            width=52,
            bg=self.SURFACE2,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            font=self.f_body,
        )
        root_entry.grid(row=1, column=0, padx=(0, 8), ipady=6)

        ttk.Button(
            ctrl,
            text="Save & Rescan",
            command=self.save_settings_from_form,
            style="Accent.TButton",
        ).grid(row=1, column=1, padx=(0, 6))
        ttk.Button(
            ctrl,
            text="Rescan",
            command=self.scan_schemas,
            style="Ghost.TButton",
        ).grid(row=1, column=2)

        # separator line
        sep = tk.Frame(self.root, bg=self.BORDER, height=1)
        sep.grid(row=0, column=0, sticky="sew")

    def _build_body(self):
        body = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            bg=self.BG,
            sashwidth=6,
            sashpad=0,
            sashrelief="flat",
            handlesize=0,
        )
        body.grid(row=1, column=0, sticky="nsew")

        # ── LEFT PANEL ───────────────────────────────────────────────────────
        left = tk.Frame(body, bg=self.BG)
        body.add(left, minsize=480)

        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        # stat cards row
        stats_row = tk.Frame(left, bg=self.BG)
        stats_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        for i in range(3):
            stats_row.columnconfigure(i, weight=1)

        self.summary_vars = {
            "count": tk.StringVar(value="0"),
            "running": tk.StringVar(value="0"),
            "db_ready": tk.StringVar(value="0"),
        }
        stat_defs = [
            ("count", "Schemas Found", self.ACCENT),
            ("running", "Live Studios", self.ACCENT2),
            ("db_ready", "DB Configured", "#f59e0b"),
        ]
        for col, (key, lbl, color) in enumerate(stat_defs):
            self._build_stat_card(stats_row, col, lbl, self.summary_vars[key], color)

        # schema list
        list_card = tk.Frame(
            left, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER
        )
        list_card.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        list_card.rowconfigure(1, weight=1)
        list_card.columnconfigure(0, weight=1)

        # list header
        list_hdr = tk.Frame(list_card, bg=self.HEADER_BG)
        list_hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(
            list_hdr,
            text="DETECTED SCHEMAS",
            bg=self.HEADER_BG,
            fg=self.TEXT_MUTE,
            font=self.f_label,
            padx=12,
            pady=8,
        ).pack(side="left")

        self.scan_status_lbl = tk.Label(
            list_hdr,
            text="",
            bg=self.HEADER_BG,
            fg=self.TEXT_DIM,
            font=self.f_label,
            padx=12,
        )
        self.scan_status_lbl.pack(side="right")

        # treeview
        columns = ("db", "status", "uptime", "url")
        self.tree = ttk.Treeview(
            list_card,
            columns=columns,
            show="tree headings",
            height=22,
            style="Prisma.Treeview",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Schema Path", anchor="w")
        self.tree.heading("db", text="DB", anchor="center")
        self.tree.heading("status", text="Status", anchor="center")
        self.tree.heading("uptime", text="Uptime", anchor="center")
        self.tree.heading("url", text="Studio URL", anchor="w")
        self.tree.column("#0", width=360, minwidth=200, anchor="w")
        self.tree.column("db", width=52, minwidth=40, anchor="center")
        self.tree.column("status", width=70, minwidth=55, anchor="center")
        self.tree.column("uptime", width=80, minwidth=60, anchor="center")
        self.tree.column("url", width=210, minwidth=100, anchor="w")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.tag_configure("running", foreground=self.ACCENT2)
        self.tree.tag_configure("stopping", foreground=self.WARNING)
        self.tree.tag_configure("error", foreground=self.DANGER)

        vsb = ttk.Scrollbar(list_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=1, column=1, sticky="ns")

        # bottom status bar
        status_bar = tk.Frame(list_card, bg=self.HEADER_BG)
        status_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(
            status_bar,
            textvariable=self.status_var,
            bg=self.HEADER_BG,
            fg=self.TEXT_DIM,
            font=self.f_label,
            padx=12,
            pady=6,
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        # ── RIGHT PANEL ──────────────────────────────────────────────────────
        right = tk.Frame(body, bg=self.BG)
        body.add(right, minsize=380)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_settings_panel(right)
        self._build_detail_panel(right)

    def _build_stat_card(self, parent, col, label, var, color):
        card = tk.Frame(
            parent,
            bg=self.SURFACE,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        parent.columnconfigure(col, weight=1)

        tk.Label(
            card, textvariable=var, bg=self.SURFACE, fg=color, font=self.f_stat
        ).pack(anchor="w", padx=14, pady=(10, 0))
        tk.Label(
            card, text=label, bg=self.SURFACE, fg=self.TEXT_DIM, font=self.f_stat_lbl
        ).pack(anchor="w", padx=14, pady=(1, 10))

    def _build_settings_panel(self, parent):
        outer = tk.Frame(
            parent,
            bg=self.SURFACE,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        outer.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        outer.columnconfigure(0, weight=1)

        hdr = tk.Frame(outer, bg=self.HEADER_BG)
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(
            hdr,
            text="SETTINGS",
            bg=self.HEADER_BG,
            fg=self.TEXT_MUTE,
            font=self.f_label,
            padx=12,
            pady=8,
        ).pack(side="left")

        inner = tk.Frame(outer, bg=self.SURFACE2, padx=12, pady=10)
        inner.grid(row=1, column=0, sticky="ew")
        inner.columnconfigure(0, weight=1)

        # patterns
        tk.Label(
            inner,
            text="Schema path patterns (one per line)",
            bg=self.SURFACE2,
            fg=self.TEXT_DIM,
            font=self.f_label,
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.patterns_text = tk.Text(
            inner,
            height=3,
            width=1,
            bg=self.BG,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            font=self.f_mono,
            padx=8,
            pady=6,
        )
        self.patterns_text.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        # run command
        tk.Label(
            inner,
            text="Run command  ({{PathOfSchema}} and {{ProjectDir}} are replaced)",
            bg=self.SURFACE2,
            fg=self.TEXT_DIM,
            font=self.f_label,
        ).grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.command_text = tk.Text(
            inner,
            height=3,
            width=1,
            bg=self.BG,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            font=self.f_mono,
            padx=8,
            pady=6,
        )
        self.command_text.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.auto_open_var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(
            inner,
            variable=self.auto_open_var,
            text="Auto-open browser when Studio URL appears",
            bg=self.SURFACE2,
            fg=self.TEXT_DIM,
            selectcolor=self.BG,
            activebackground=self.SURFACE2,
            activeforeground=self.TEXT,
            font=self.f_body,
            bd=0,
            highlightthickness=0,
        )
        cb.grid(row=4, column=0, sticky="w")

    def _build_detail_panel(self, parent):
        outer = tk.Frame(
            parent,
            bg=self.SURFACE,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        outer.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)

        # panel header
        det_hdr = tk.Frame(outer, bg=self.HEADER_BG)
        det_hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(
            det_hdr,
            text="SELECTED SCHEMA",
            bg=self.HEADER_BG,
            fg=self.TEXT_MUTE,
            font=self.f_label,
            padx=12,
            pady=8,
        ).pack(side="left")

        # action buttons
        btn_bar = tk.Frame(det_hdr, bg=self.HEADER_BG)
        btn_bar.pack(side="right", padx=8)
        self.start_button = ttk.Button(
            btn_bar,
            text="▶  Start",
            command=self._start_selected,
            style="Success.TButton",
        )
        self.start_button.pack(side="left", padx=(0, 4))
        self.stop_button = ttk.Button(
            btn_bar,
            text="■  Stop",
            command=self._stop_selected,
            style="Danger.TButton",
        )
        self.stop_button.pack(side="left", padx=(0, 4))
        self.open_button = ttk.Button(
            btn_bar,
            text="⎋  Open",
            command=self._open_selected_url,
            style="Ghost.TButton",
        )
        self.open_button.pack(side="left", padx=(0, 0))

        # Bind hover events to trigger active state color change
        for btn in (self.start_button, self.stop_button, self.open_button):
            btn.bind("<Enter>", lambda e, b=btn: self._on_button_enter(b))
            btn.bind("<Leave>", lambda e, b=btn: self._on_button_leave(b))

        # separator
        # tk.Frame(btn_bar, bg=self.BORDER, width=1).pack(
        #     side="left", fill="y", pady=4, padx=(0, 12)
        # )

        # self.delete_db_button = ttk.Button(
        #     btn_bar,
        #     text="🗑  Delete DB",
        #     command=self._delete_db_selected,
        #     style="DeleteDB.TButton",
        # )
        # self.delete_db_button.pack(side="left")

        # ensure cursors reflect initial enabled/disabled states
        # for b in (self.start_button, self.stop_button, self.open_button, self.delete_db_button):
        #     try:
        #         self._update_button_cursor(b)
        #     except Exception:
        #         pass

        # meta grid
        meta = tk.Frame(outer, bg=self.SURFACE2, padx=12, pady=10)
        meta.grid(row=1, column=0, sticky="ew")
        meta.columnconfigure(1, weight=1)

        self.detail_vars = {
            k: tk.StringVar(value="—")
            for k in (
                "schema_path",
                "project_dir",
                "pattern",
                "env_path",
                "db_status",
                "running",
                "uptime",
                "url",
                "database_uri",
            )
        }

        detail_rows = [
            ("Schema", "schema_path"),
            ("Project Dir", "project_dir"),
            ("Matched Pattern", "pattern"),
            ("Env File", "env_path"),
            ("DB Value", "db_status"),
            ("Status", "running"),
            ("Uptime", "uptime"),
            ("URL", "url"),
            ("Database URI", "database_uri"),
        ]
        for r, (label, key) in enumerate(detail_rows):
            tk.Label(
                meta,
                text=label,
                bg=self.SURFACE2,
                fg=self.TEXT_MUTE,
                font=self.f_label,
                width=14,
                anchor="e",
            ).grid(row=r, column=0, sticky="ne", padx=(0, 10), pady=3)

            # Special handling for Database URI: masked by default, reveal on click, and copy button
            if key == "database_uri":
                container = tk.Frame(meta, bg=self.SURFACE2)
                container.grid(row=r, column=1, sticky="w", pady=3)

                self.database_uri_value_lbl = tk.Label(
                    container,
                    text="—",
                    bg=self.SURFACE2,
                    fg=self.TEXT,
                    font=self.f_body,
                    anchor="w",
                    wraplength=340,
                    justify="left",
                )
                self.database_uri_value_lbl.pack(side="left", fill="x", expand=True, padx=(0, 8))
                self.database_uri_value_lbl.bind(
                    "<Button-1>", lambda e: self._toggle_db_uri()
                )

                btn_frame = tk.Frame(container, bg=self.SURFACE2)
                btn_frame.pack(side="right")

                # Only show the copy button after the URI is revealed.
                self.db_copy_btn = ttk.Button(
                    btn_frame,
                    text="Copy",
                    command=self._copy_db_uri,
                    style="Ghost.TButton",
                    padding=(6, 4),
                )
                # start hidden
                self.db_copy_btn.pack_forget()

                # internal flag
                self._db_uri_shown = False
            else:
                tk.Label(
                    meta,
                    textvariable=self.detail_vars[key],
                    bg=self.SURFACE2,
                    fg=self.TEXT,
                    font=self.f_body,
                    anchor="w",
                    wraplength=340,
                    justify="left",
                ).grid(row=r, column=1, sticky="w", pady=3)

        # output terminal
        term_hdr = tk.Frame(outer, bg=self.HEADER_BG)
        term_hdr.grid(row=2, column=0, sticky="new")
        tk.Label(
            term_hdr,
            text="OUTPUT",
            bg=self.HEADER_BG,
            fg=self.TEXT_MUTE,
            font=self.f_label,
            padx=12,
            pady=6,
        ).pack(side="left")

        term_wrap = tk.Frame(outer, bg="#090d14")
        term_wrap.grid(row=2, column=0, sticky="nsew", pady=(26, 0))
        term_wrap.rowconfigure(0, weight=1)
        term_wrap.columnconfigure(0, weight=1)

        self.output_text = tk.Text(
            term_wrap,
            wrap="word",
            bg="#090d14",
            fg="#a8d8b0",
            insertbackground="#a8d8b0",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self.f_mono,
            padx=10,
            pady=8,
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")
        self.output_text.configure(state="disabled")

        out_vsb = ttk.Scrollbar(
            term_wrap, orient="vertical", command=self.output_text.yview
        )
        self.output_text.configure(yscrollcommand=out_vsb.set)
        out_vsb.grid(row=0, column=1, sticky="ns")

    # ── settings ───────────────────────────────────────────────────────────────

    def load_settings_into_form(self):
        self.root_dir_var.set(self.settings["root_dir"])
        self.patterns_text.delete("1.0", tk.END)
        self.patterns_text.insert("1.0", "\n".join(self.settings["patterns"]))
        self.command_text.delete("1.0", tk.END)
        self.command_text.insert("1.0", self.settings["run_command"])
        self.auto_open_var.set(self.settings["auto_open_browser"])

    def _current_form_settings(self):
        patterns = [
            ln.strip().replace("\\", "/")
            for ln in self.patterns_text.get("1.0", tk.END).splitlines()
            if ln.strip()
        ]
        return {
            "root_dir": self.root_dir_var.get().strip() or str(Path.cwd()),
            "patterns": patterns or DEFAULT_SETTINGS["patterns"],
            "run_command": self.command_text.get("1.0", tk.END).strip()
            or DEFAULT_SETTINGS["run_command"],
            "auto_open_browser": self.auto_open_var.get(),
        }

    def save_settings_from_form(self):
        self.settings = save_settings(self._current_form_settings())
        self.status_var.set("Settings saved.")
        self.scan_schemas()

    # ── scan ───────────────────────────────────────────────────────────────────

    def scan_schemas(self):
        self.settings = normalize_settings(self._current_form_settings())
        root_dir = Path(self.settings["root_dir"]).expanduser()
        if not root_dir.exists() or not root_dir.is_dir():
            messagebox.showerror(
                "Invalid Folder", "The scan root must be an existing directory."
            )
            return
        if self.scan_in_progress:
            self.status_var.set("Scan already in progress…")
            return
        self.scan_in_progress = True
        self.scan_status_lbl.config(text="Scanning…")
        self.status_var.set(f"Scanning {root_dir} …")
        threading.Thread(
            target=self._scan_worker,
            args=(root_dir, self.settings["patterns"]),
            daemon=True,
        ).start()

    def _scan_worker(self, root_dir, patterns):
        results, schema_map = [], {}
        try:
            np = [p.replace("\\", "/") for p in patterns]
            for schema_path in root_dir.rglob("*"):
                if not schema_path.is_file():
                    continue
                if any(part in {".git", "node_modules"} for part in schema_path.parts):
                    continue
                relative = schema_path.relative_to(root_dir).as_posix()
                matched = next((p for p in np if matches_pattern(relative, p)), None)
                if not matched:
                    continue
                project_dir = project_dir_from_pattern(schema_path, root_dir, matched)
                env_path = project_dir / ".env"
                env_values = parse_env_file(env_path)
                env_var_names = extract_datasource_env_vars(schema_path)
                datasource_vars = [
                    {"name": n, "has_value": bool(env_values and env_values.get(n))}
                    for n in env_var_names
                ]
                record = SchemaRecord(
                    schema_path=schema_path,
                    schema_relative=relative,
                    project_dir=project_dir,
                    project_relative=(
                        project_dir.relative_to(root_dir).as_posix()
                        if project_dir != root_dir
                        else "."
                    ),
                    matched_pattern=matched,
                    env_path=env_path,
                    env_exists=env_values is not None,
                    datasource_env_vars=datasource_vars,
                    has_database_value=any(i["has_value"] for i in datasource_vars),
                )
                key = str(schema_path.resolve())
                results.append(record)
                schema_map[key] = record

            results.sort(key=lambda r: r.schema_relative.lower())
            self.message_queue.put(
                (
                    "scan_complete",
                    {
                        "records": results,
                        "schema_map": schema_map,
                        "root_dir": str(root_dir),
                    },
                )
            )
        except Exception as exc:
            self.message_queue.put(("scan_error", str(exc)))

    # ── tree refresh ───────────────────────────────────────────────────────────

    def _refresh_tree(self):
        prev = self.selected_key
        for item in self.tree.get_children():
            self.tree.delete(item)

        for rec in self.schema_records:
            key = str(rec.schema_path.resolve())
            proc = self.processes.get(key)
            running = proc and proc.is_running()

            if proc and proc.stop_in_progress:
                status_txt = "Stopping"
                tag = "stopping"
            elif running:
                status_txt = "● On"
                tag = "running"
            else:
                status_txt = "○ Off"
                tag = ""

            self.tree.insert(
                "",
                tk.END,
                iid=key,
                text=rec.schema_relative,
                values=(
                    "✓" if rec.has_database_value else "✗",
                    status_txt,
                    format_duration(proc.started_at) if running else "—",
                    proc.url if proc and proc.url else "—",
                ),
                tags=(tag,),
            )

        if prev and self.tree.exists(prev):
            self.tree.selection_set(prev)
            self.tree.focus(prev)
        elif self.schema_records:
            first = str(self.schema_records[0].schema_path.resolve())
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.selected_key = first
        else:
            self.selected_key = None

        self._refresh_summary()
        self._refresh_details()

    def _refresh_summary(self):
        running = sum(1 for p in self.processes.values() if p.is_running())
        db_ready = sum(1 for r in self.schema_records if r.has_database_value)
        self.summary_vars["count"].set(str(len(self.schema_records)))
        self.summary_vars["running"].set(str(running))
        self.summary_vars["db_ready"].set(str(db_ready))

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        self.selected_key = sel[0] if sel else None
        self._refresh_details()

    def _refresh_details(self):
        key = self.selected_key or ""
        rec = self.schema_map.get(key)
        proc = self.processes.get(key)

        if not rec:
            for v in self.detail_vars.values():
                v.set("—")
            self._set_output("Select a schema from the list to see details.")
            self.start_button.state(["disabled"])
            self.stop_button.state(["disabled"])
            self.open_button.state(["disabled"])
            # self.delete_db_button.state(["disabled"])
            return

        # Resolve env values and datasource URL/files for display
        env_values = parse_env_file(rec.env_path) if rec.env_exists else None
        db_files = resolve_db_files(rec.schema_path, env_values)
        ds_url = get_datasource_url(rec.schema_path, env_values)
        running = bool(proc and proc.is_running())

        self.detail_vars["schema_path"].set(str(rec.schema_path))
        self.detail_vars["project_dir"].set(str(rec.project_dir))
        self.detail_vars["pattern"].set(rec.matched_pattern)
        self.detail_vars["env_path"].set(
            f'{rec.env_path}{"  (not found)" if not rec.env_exists else ""}'
        )
        self.detail_vars["db_status"].set(
            "✓ Ready" if rec.has_database_value else "✗ Missing or empty"
        )

        if proc and proc.stop_in_progress:
            running_label = "Stopping…"
        elif running:
            running_label = "● Running"
        else:
            running_label = "○ Stopped"

        self.detail_vars["running"].set(running_label)
        self.detail_vars["uptime"].set(
            format_duration(proc.started_at) if running else "—"
        )
        self.detail_vars["url"].set(proc.url if proc and proc.url else "Not available")
        # Build datasource vars display: show env var statuses and DB path/URL
        db_path = ""
        if db_files:
            db_path = ", ".join(str(p) for p in db_files)
        elif ds_url:
            db_path = ds_url
        else:
            db_path = "No DB path or URL found"

        self.detail_vars["database_uri"].set(db_path)
        # Update masked/revealed display if present and control copy button visibility
        if hasattr(self, "database_uri_value_lbl"):
            val = db_path or "—"
            if getattr(self, "_db_uri_shown", False):
                self.database_uri_value_lbl.config(text=val)
                # show copy button
                try:
                    if not self.db_copy_btn.winfo_ismapped():
                        self.db_copy_btn.pack(side="right")
                except Exception:
                    pass
            else:
                if val and val not in ("—", "No DB path or URL found"):
                    self.database_uri_value_lbl.config(text="•••••• (click to reveal)")
                else:
                    self.database_uri_value_lbl.config(text=val)
                # hide copy button while masked
                try:
                    if self.db_copy_btn.winfo_ismapped():
                        self.db_copy_btn.pack_forget()
                except Exception:
                    pass
        self._set_output(proc.output_text() if proc else "No output captured yet.")

        can_start = not running and not (proc and proc.stop_in_progress)
        can_stop = running and not (proc and proc.stop_in_progress)
        self.start_button.state(["!disabled"] if can_start else ["disabled"])
        self.stop_button.state(["!disabled"] if can_stop else ["disabled"])
        self.open_button.state(["!disabled"] if proc and proc.url else ["disabled"])
        # update cursors to reflect enabled/disabled states
        # for b in (self.start_button, self.stop_button, self.open_button, self.delete_db_button):
        #     try:
        #         self._update_button_cursor(b)
        #     except Exception:
        #         pass
        # also update copy button cursor if it exists
        try:
            if hasattr(self, "db_copy_btn"):
                self._update_button_cursor(self.db_copy_btn)
        except Exception:
            pass
        # copy URL button removed; nothing to set here

        # Delete DB: enabled whenever the studio isn't running for this schema.
        # The deletion flow will still check for files and require a hard confirmation.
        can_delete = not running
        # self.delete_db_button.state(["!disabled"] if can_delete else ["disabled"])

    def _set_output(self, value):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", value)
        self.output_text.see(tk.END)
        self.output_text.configure(state="disabled")

    def _on_button_enter(self, btn):
        """Show active state when mouse hovers over button."""
        try:
            if "disabled" not in btn.state():
                btn.state(["active"])
        except Exception:
            pass

    def _on_button_leave(self, btn):
        """Remove active state when mouse leaves button."""
        try:
            btn.state([])
        except Exception:
            pass

    def _update_button_cursor(self, btn: ttk.Button | None):
        """Set cursor to hand2 when enabled, arrow when disabled."""
        if not btn:
            return
        try:
            if getattr(btn, "instate", lambda *_: False)(["disabled"]):
                btn.configure(cursor="arrow")
            else:
                btn.configure(cursor="hand2")
        except Exception:
            try:
                btn.configure(cursor="arrow")
            except Exception:
                pass

    def _toggle_db_uri(self):
        if not hasattr(self, "database_uri_value_lbl"):
            return
        shown = getattr(self, "_db_uri_shown", False)
        val = self.detail_vars.get("database_uri", tk.StringVar()).get()
        if not val or val in ("—", "No DB path or URL found"):
            return
        self._db_uri_shown = not shown
        if self._db_uri_shown:
            self.database_uri_value_lbl.config(text=val)
            # reveal and show copy button
            try:
                if not self.db_copy_btn.winfo_ismapped():
                    self.db_copy_btn.pack(side="right")
                # ensure copy button cursor
                try:
                    self._update_button_cursor(self.db_copy_btn)
                except Exception:
                    pass
            except Exception:
                pass
            self.status_var.set("Database URI revealed")
        else:
            self.database_uri_value_lbl.config(text="•••••• (click to reveal)")
            # hide copy button while masked
            try:
                if self.db_copy_btn.winfo_ismapped():
                    self.db_copy_btn.pack_forget()
            except Exception:
                pass
            self.status_var.set("Database URI hidden")

    def _copy_db_uri(self):
        if not hasattr(self, "database_uri_value_lbl"):
            return
        val = self.detail_vars.get("database_uri", tk.StringVar()).get()
        if not val or val in ("—", "No DB path or URL found"):
            messagebox.showinfo("Copy Database URI", "No database URI available to copy.")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(val)
            self.status_var.set("Database URI copied to clipboard.")
        except Exception:
            messagebox.showerror("Copy Failed", "Unable to copy to clipboard.")

    # ── process control ────────────────────────────────────────────────────────

    def _selected_record(self):
        return self.schema_map.get(self.selected_key or "")

    def _start_selected(self):
        rec = self._selected_record()
        if not rec:
            return
        key = str(rec.schema_path.resolve())
        existing = self.processes.get(key)
        if existing and existing.is_running():
            self.status_var.set("Prisma Studio is already running for this schema.")
            return

        cmd = self.settings["run_command"]
        cmd = cmd.replace("{{PathOfSchema}}", str(rec.schema_path))
        cmd = cmd.replace("{{ProjectDir}}", str(rec.project_dir))

        try:
            flags = 0
            if os.name == "nt":
                flags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            popen = subprocess.Popen(
                cmd,
                cwd=str(rec.project_dir),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except OSError as err:
            messagebox.showerror("Start Failed", str(err))
            return

        state = ProcessState(
            schema_key=key, command=cmd, cwd=str(rec.project_dir), popen=popen
        )
        self.processes[key] = state
        threading.Thread(
            target=self._capture_output, args=(state,), daemon=True
        ).start()
        self.status_var.set(f"Started Prisma Studio for {rec.schema_relative}")
        self._refresh_tree()

    def _capture_output(self, ps: ProcessState):
        stream = ps.popen.stdout
        if stream is None:
            self.message_queue.put(("exit", ps.schema_key, None))
            return
        for line in iter(stream.readline, ""):
            self.message_queue.put(("output", ps.schema_key, line))
        self.message_queue.put(("exit", ps.schema_key, None))

    def _stop_selected(self):
        rec = self._selected_record()
        if not rec:
            return
        key = str(rec.schema_path.resolve())
        proc = self.processes.get(key)
        if not proc or not proc.is_running():
            self.status_var.set("This schema is not running.")
            return
        proc.stop_requested = True
        proc.stop_in_progress = True
        proc.url = None  # ← clear URL immediately
        proc.append_output("⏹  Stopping Prisma Studio…")
        self.status_var.set(f"Stopping Prisma Studio for {rec.schema_relative}")
        self._refresh_tree()
        threading.Thread(target=self._stop_worker, args=(key,), daemon=True).start()

    def _stop_worker(self, key):
        proc = self.processes.get(key)
        if not proc:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.popen.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.popen.terminate()
                try:
                    proc.popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.popen.kill()
        finally:
            self.message_queue.put(("stopped", key, None))

    def _open_selected_url(self):
        if not self.selected_key:
            return
        proc = self.processes.get(self.selected_key)
        if proc and proc.url:
            webbrowser.open(proc.url)

    # Copy URL/button removed; use displayed datasource info instead

    def _delete_db_selected(self):
        rec = self._selected_record()
        if not rec:
            return

        # Guard: refuse if studio is still running for this schema
        key = str(rec.schema_path.resolve())
        proc = self.processes.get(key)
        if proc and proc.is_running():
            messagebox.showwarning(
                "Studio Running",
                "Stop Prisma Studio for this schema before deleting its database.",
            )
            return

        # Resolve which DB files actually exist (SQLite file: URLs)
        env_values = parse_env_file(rec.env_path) if rec.env_exists else None
        db_files = resolve_db_files(rec.schema_path, env_values)

        # Build a readable list for the confirmation dialog (if any files)
        file_list = (
            "\n".join(f"  • {p}" for p in db_files)
            if db_files
            else "  (No SQLite file found)"
        )

        # Hard confirmation: require the user to type DELETE to proceed
        prompt_msg = (
            "This will permanently delete the following file(s):\n\n"
            f"{file_list}\n\n"
            "This action CANNOT be undone.\n\n"
            "To confirm, type DELETE exactly (without quotes) and press OK."
        )
        answer = simpledialog.askstring(
            "Confirm Deletion", prompt_msg, parent=self.root
        )
        if not answer or answer.strip() != "DELETE":
            messagebox.showinfo("Cancelled", "Database deletion cancelled.")
            return

        deleted, failed = [], []
        for db_path in db_files:
            try:
                db_path.unlink()
                deleted.append(str(db_path))
            except OSError as err:
                failed.append(f"{db_path}: {err}")

        # Also remove companion files Prisma creates alongside SQLite DBs
        for db_path in db_files:
            for suffix in ("-journal", "-wal", "-shm"):
                companion = db_path.with_name(db_path.name + suffix)
                if companion.exists():
                    try:
                        companion.unlink()
                    except OSError:
                        pass

        if deleted:
            self.status_var.set(f"Deleted {len(deleted)} database file(s).")
            # Rescan so DB-ready status updates
            self.scan_schemas()

        if failed:
            messagebox.showerror(
                "Deletion Errors",
                "Some files could not be deleted:\n\n" + "\n".join(failed),
            )

    # ── queue / timer ──────────────────────────────────────────────────────────

    def _process_queue(self):
        changed = False
        while True:
            try:
                msg = self.message_queue.get_nowait()
            except queue.Empty:
                break

            event = msg[0]

            if event == "scan_complete":
                payload = msg[1]
                self.schema_records = payload["records"]
                self.schema_map = payload["schema_map"]
                self.scan_in_progress = False
                self.scan_status_lbl.config(text="")
                self._refresh_tree()
                self.status_var.set(
                    f'Scan complete — {len(self.schema_records)} schema(s) found in {payload["root_dir"]}'
                )
                continue

            if event == "scan_error":
                self.scan_in_progress = False
                self.scan_status_lbl.config(text="Scan failed")
                self.status_var.set("Scan failed.")
                messagebox.showerror("Scan Failed", msg[1])
                self._refresh_details()
                continue

            _, key, payload = msg
            proc = self.processes.get(key)
            if not proc:
                continue

            if event == "output":
                proc.append_output(payload)
                m = URL_RE.search(payload)
                if m:
                    proc.url = m.group(0)
                    if self.settings["auto_open_browser"] and not proc.auto_opened:
                        proc.auto_opened = True
                        webbrowser.open(proc.url)
                changed = True

            elif event == "exit":
                proc.stop_in_progress = False
                proc.stop_requested = False
                proc.url = None  # ← clear URL on exit
                proc.append_output("Process exited.")
                changed = True

            elif event == "stopped":
                proc.stop_in_progress = False
                proc.url = None  # ← clear URL on manual stop
                changed = True

        # clean up closed processes
        for key, proc in list(self.processes.items()):
            if not proc.is_running() and not proc.stdout_closed:
                if proc.popen.stdout:
                    proc.popen.stdout.close()
                proc.stdout_closed = True

        if changed:
            self._refresh_tree()

        self.root.after(300, self._process_queue)

    def _refresh_runtime_columns(self):
        for key, proc in list(self.processes.items()):
            if self.tree.exists(key):
                rec = self.schema_map.get(key)
                if not rec:
                    continue
                running = proc.is_running()
                if proc.stop_in_progress:
                    s_txt = "Stopping"
                    tag = "stopping"
                elif running:
                    s_txt = "● On"
                    tag = "running"
                else:
                    s_txt = "○ Off"
                    tag = ""
                self.tree.item(
                    key,
                    values=(
                        "✓" if rec.has_database_value else "✗",
                        s_txt,
                        format_duration(proc.started_at) if running else "—",
                        proc.url or "—",
                    ),
                    tags=(tag,),
                )

        self._refresh_details()
        self._refresh_summary()
        self.root.after(1000, self._refresh_runtime_columns)

    # ── close ──────────────────────────────────────────────────────────────────

    def _on_close(self):
        running = [p for p in self.processes.values() if p.is_running()]
        if running:
            if not messagebox.askyesno(
                "Exit",
                f"{len(running)} Prisma Studio process(es) are still running.\nStop them all and exit?",
            ):
                return
            for p in running:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(p.popen.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    p.popen.terminate()
        self.root.destroy()


# ── entry point ────────────────────────────────────────────────────────────────


def main():
    root = tk.Tk()
    PrismaCheckerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
