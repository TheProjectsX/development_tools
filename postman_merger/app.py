"""
Postman Collection Merger
-------------------------
Merges a "new" Postman collection into a "target" collection:
  - Adds new routes from "new" that don't exist in target
  - Removes routes from target that were deleted in "new"
  - Preserves saved responses & metadata in target for unchanged routes

Works with Postman Collection v2 and v2.1 JSON format.
"""

import json
import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import copy
import datetime


# ─── Postman merge logic ────────────────────────────────────────────────────

def get_route_key(item: dict) -> str:
    """Build a unique key for a request item: METHOD /path/path."""
    req = item.get("request", {})
    method = req.get("method", "GET").upper() if isinstance(req, dict) else "GET"
    url = req.get("url", "") if isinstance(req, dict) else ""
    if isinstance(url, dict):
        raw = url.get("raw", "")
        parts = url.get("path", [])
        path = "/" + "/".join(p if isinstance(p, str) else str(p) for p in parts) if parts else raw
    else:
        path = url
    # Strip protocol/host noise so keys are stable across base-url changes
    for prefix in ("{{", "http://", "https://"):
        if prefix in path:
            idx = path.find("/", path.index(prefix) + len(prefix))
            if idx != -1:
                path = path[idx:]
                break
    return f"{method} {path.strip()}"


def flatten_items(items: list, prefix: str = "") -> dict:
    """Recursively flatten folder→items into {route_key: item}."""
    result = {}
    for item in items:
        if "item" in item:  # it's a folder
            folder_name = item.get("name", "")
            sub = flatten_items(item["item"], prefix=f"{prefix}/{folder_name}" if prefix else folder_name)
            result.update(sub)
        else:
            key = get_route_key(item)
            result[key] = item
    return result


def collect_all_keys(items: list) -> set:
    return set(flatten_items(items).keys())


def remove_deleted_from_target(target_items: list, deleted_keys: set) -> list:
    """Recursively remove items whose route_key is in deleted_keys."""
    new_items = []
    for item in target_items:
        if "item" in item:  # folder
            item = copy.deepcopy(item)
            item["item"] = remove_deleted_from_target(item["item"], deleted_keys)
            new_items.append(item)
        else:
            key = get_route_key(item)
            if key not in deleted_keys:
                new_items.append(item)
    return new_items


def _folder_key(item: dict) -> str:
    """Build a stable key for a Postman folder."""
    return item.get("name", "")


def _find_matching_folder(items: list, folder_name: str):
    for item in items:
        if "item" in item and _folder_key(item) == folder_name:
            return item
    return None


def _build_new_subtree(new_items: list, existing_keys: set) -> list:
    """Return only the new items from the source tree, preserving folder nesting."""
    subtree = []
    for item in new_items:
        if "item" in item:
            child_subtree = _build_new_subtree(item.get("item", []), existing_keys)
            if child_subtree:
                folder_copy = {k: copy.deepcopy(v) for k, v in item.items() if k != "item"}
                folder_copy["item"] = child_subtree
                subtree.append(folder_copy)
        else:
            key = get_route_key(item)
            if key not in existing_keys:
                subtree.append(copy.deepcopy(item))
    return subtree


def _merge_new_subtree(target_items: list, new_subtree: list) -> list:
    """Merge a filtered subtree into the target tree, creating folders as needed."""
    merged_items = list(target_items)
    for item in new_subtree:
        if "item" in item:
            folder_name = _folder_key(item)
            target_folder = _find_matching_folder(merged_items, folder_name)
            if target_folder is None:
                merged_items.append(copy.deepcopy(item))
            else:
                target_folder["item"] = _merge_new_subtree(target_folder.get("item", []), item.get("item", []))
        else:
            merged_items.append(copy.deepcopy(item))
    return merged_items


def add_new_to_target(target_items: list, new_collection: dict, existing_keys: set) -> list:
    """Add only new requests while preserving the folder structure from the source tree."""
    new_subtree = _build_new_subtree(new_collection.get("item", []), existing_keys)
    if not new_subtree:
        return target_items
    return _merge_new_subtree(target_items, new_subtree)


def merge_collections(target: dict, new_col: dict) -> tuple:
    """
    Returns (merged_collection, stats_dict).
    stats_dict = {added: [...], removed: [...], preserved: int}
    """
    target_items = target.get("item", [])
    new_items = new_col.get("item", [])

    target_keys = collect_all_keys(target_items)
    new_keys_flat = flatten_items(new_items)
    new_keys = set(new_keys_flat.keys())

    added_keys = new_keys - target_keys
    removed_keys = target_keys - new_keys
    preserved = len(target_keys & new_keys)

    # Step 1: remove deleted
    merged_items = remove_deleted_from_target(target_items, removed_keys)
    # Step 2: add new
    merged_items = add_new_to_target(merged_items, new_col, target_keys)

    merged = copy.deepcopy(target)
    merged["item"] = merged_items

    stats = {
        "added": sorted(added_keys),
        "removed": sorted(removed_keys),
        "preserved": preserved,
    }
    return merged, stats


# ─── UI ─────────────────────────────────────────────────────────────────────

BG        = "#0f1117"
PANEL     = "#1a1d27"
ACCENT    = "#f97316"   # orange
ACCENT2   = "#3b82f6"   # blue
TEXT      = "#e2e8f0"
MUTED     = "#64748b"
SUCCESS   = "#22c55e"
DANGER    = "#ef4444"
BORDER    = "#2d3148"
FONT_MONO = ("Consolas", 10)
FONT_UI   = ("Segoe UI", 10)
FONT_HEAD = ("Segoe UI", 13, "bold")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Postman Collection Merger")
        self.geometry("820x640")
        self.minsize(700, 560)
        self.configure(bg=BG)
        self.resizable(True, True)

        self.target_path = tk.StringVar()
        self.new_path    = tk.StringVar()
        self.replace_var = tk.BooleanVar(value=False)

        self._build_ui()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header bar
        hdr = tk.Frame(self, bg=ACCENT, height=4)
        hdr.pack(fill="x")

        title_row = tk.Frame(self, bg=BG, pady=18)
        title_row.pack(fill="x", padx=28)
        tk.Label(title_row, text="⬡  Postman", font=("Segoe UI", 9), fg=ACCENT, bg=BG).pack(side="left")
        tk.Label(title_row, text=" Collection Merger", font=FONT_HEAD, fg=TEXT, bg=BG).pack(side="left")

        # Main content
        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True, padx=28, pady=(0, 20))

        # File pickers
        pickers = tk.Frame(content, bg=BG)
        pickers.pack(fill="x")
        pickers.columnconfigure(0, weight=1)
        pickers.columnconfigure(1, weight=1)

        self._file_card(pickers, "TARGET Collection",
                        "Your existing Postman file\n(has saved responses you want to keep)",
                        self.target_path, ACCENT2, 0)
        self._file_card(pickers, "NEW Collection",
                        "Updated file from your codebase\n(has new/removed routes)",
                        self.new_path, ACCENT, 1)

        # Options row
        opts = tk.Frame(content, bg=PANEL, padx=16, pady=12,
                        highlightbackground=BORDER, highlightthickness=1)
        opts.pack(fill="x", pady=(14, 0))

        chk_frame = tk.Frame(opts, bg=PANEL)
        chk_frame.pack(side="left")

        self.replace_chk = tk.Checkbutton(
            chk_frame, text="  Replace target file in-place",
            variable=self.replace_var,
            font=FONT_UI, fg=TEXT, bg=PANEL,
            activeforeground=TEXT, activebackground=PANEL,
            selectcolor=PANEL, relief="flat",
            command=self._on_replace_toggle
        )
        self.replace_chk.pack(side="left")

        self.replace_note = tk.Label(
            opts,
            text="A backup will be created automatically",
            font=("Segoe UI", 9), fg=MUTED, bg=PANEL
        )
        self.replace_note.pack(side="right", padx=4)

        # Merge button
        btn_row = tk.Frame(content, bg=BG)
        btn_row.pack(pady=16)

        self.merge_btn = tk.Button(
            btn_row, text="⚡  Merge Collections",
            font=("Segoe UI", 11, "bold"),
            fg="white", bg=ACCENT,
            activeforeground="white", activebackground="#ea6c0e",
            relief="flat", padx=28, pady=10,
            cursor="hand2",
            command=self._do_merge
        )
        self.merge_btn.pack(side="left", padx=6)

        self.save_btn = tk.Button(
            btn_row, text="💾  Save As…",
            font=FONT_UI,
            fg=TEXT, bg=PANEL,
            activeforeground=TEXT, activebackground=BORDER,
            relief="flat", padx=18, pady=10,
            cursor="hand2",
            command=self._save_result,
            state="disabled"
        )
        self.save_btn.pack(side="left", padx=6)

        # Log / results panel
        log_label = tk.Label(content, text="MERGE LOG", font=("Segoe UI", 8, "bold"),
                             fg=MUTED, bg=BG, anchor="w")
        log_label.pack(fill="x", pady=(4, 4))

        log_frame = tk.Frame(content, bg=PANEL,
                             highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True)

        self.log = tk.Text(
            log_frame, bg=PANEL, fg=TEXT,
            font=FONT_MONO, relief="flat",
            padx=14, pady=12, wrap="word",
            state="disabled", cursor="arrow"
        )
        self.log.pack(fill="both", expand=True, side="left")

        sb = tk.Scrollbar(log_frame, command=self.log.yview, bg=PANEL,
                          troughcolor=PANEL, relief="flat")
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        # Text tags
        self.log.tag_config("head",    foreground=ACCENT,  font=("Segoe UI", 10, "bold"))
        self.log.tag_config("added",   foreground=SUCCESS)
        self.log.tag_config("removed", foreground=DANGER)
        self.log.tag_config("info",    foreground=ACCENT2)
        self.log.tag_config("muted",   foreground=MUTED)
        self.log.tag_config("ok",      foreground=SUCCESS,  font=("Segoe UI", 10, "bold"))

        self._log("Ready. Choose two Postman collection JSON files and click Merge.\n", "muted")

        self._merged_data = None

    def _file_card(self, parent, title, desc, var, color, col):
        card = tk.Frame(parent, bg=PANEL,
                        highlightbackground=color, highlightthickness=1)
        card.grid(row=0, column=col, sticky="nsew",
                  padx=(0 if col else 0, 8 if col == 0 else 0), pady=4)
        if col == 1:
            card.grid(padx=(8, 0))

        inner = tk.Frame(card, bg=PANEL, padx=14, pady=14)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text=title, font=("Segoe UI", 9, "bold"),
                 fg=color, bg=PANEL).pack(anchor="w")
        tk.Label(inner, text=desc, font=("Segoe UI", 8),
                 fg=MUTED, bg=PANEL, justify="left").pack(anchor="w", pady=(2, 8))

        entry_row = tk.Frame(inner, bg=PANEL)
        entry_row.pack(fill="x")

        entry = tk.Entry(entry_row, textvariable=var,
                         font=("Segoe UI", 9), bg="#0f1117", fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         highlightbackground=BORDER, highlightthickness=1)
        entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))

        browse_btn = tk.Button(
            entry_row, text="Browse",
            font=("Segoe UI", 9), fg=TEXT, bg=BORDER,
            activeforeground=TEXT, activebackground=MUTED,
            relief="flat", padx=10, pady=5,
            cursor="hand2",
            command=lambda v=var: self._browse(v)
        )
        browse_btn.pack(side="left")

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _browse(self, var):
        path = filedialog.askopenfilename(
            filetypes=[("Postman Collection", "*.json"), ("All files", "*.*")]
        )
        if path:
            var.set(path)

    def _on_replace_toggle(self):
        if self.replace_var.get():
            self.replace_note.config(text="⚠  Target will be overwritten  (backup auto-saved)", fg="#fbbf24")
        else:
            self.replace_note.config(text="A backup will be created automatically", fg=MUTED)

    def _log(self, text, tag=None):
        self.log.config(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ── Merge ────────────────────────────────────────────────────────────────

    def _do_merge(self):
        target_p = self.target_path.get().strip()
        new_p    = self.new_path.get().strip()

        if not target_p or not new_p:
            messagebox.showerror("Missing files", "Please select both collection files.")
            return

        for p, label in [(target_p, "Target"), (new_p, "New")]:
            if not os.path.isfile(p):
                messagebox.showerror("File not found", f"{label} file not found:\n{p}")
                return

        try:
            with open(target_p, encoding="utf-8") as f:
                target_col = json.load(f)
            with open(new_p, encoding="utf-8") as f:
                new_col = json.load(f)
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON Error", f"Could not parse JSON:\n{e}")
            return

        self._clear_log()
        self._log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "muted")
        self._log(f"  Merging collections\n", "head")
        self._log(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n", "muted")
        self._log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n", "muted")

        merged, stats = merge_collections(target_col, new_col)
        self._merged_data = merged

        # Log results
        self._log(f"  ✦ Preserved  {stats['preserved']} route(s) with saved responses\n", "info")
        self._log(f"\n  ✚ Added  ({len(stats['added'])})\n", "added")
        for r in stats["added"]:
            self._log(f"      + {r}\n", "added")
        if not stats["added"]:
            self._log("      (none)\n", "muted")

        self._log(f"\n  ✖ Removed  ({len(stats['removed'])})\n", "removed")
        for r in stats["removed"]:
            self._log(f"      - {r}\n", "removed")
        if not stats["removed"]:
            self._log("      (none)\n", "muted")

        self._log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "muted")

        # Replace in-place?
        if self.replace_var.get():
            backup = target_p + f".backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            shutil.copy2(target_p, backup)
            with open(target_p, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
            self._log(f"\n  ✔  Target replaced in-place\n", "ok")
            self._log(f"     Backup: {os.path.basename(backup)}\n", "muted")
        else:
            self._log("\n  ✔  Merge complete — click 'Save As' to export\n", "ok")
            self.save_btn.config(state="normal")

    def _save_result(self):
        if not self._merged_data:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Postman Collection", "*.json")],
            initialfile="merged_collection.postman_collection.json"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._merged_data, f, indent=2, ensure_ascii=False)
            self._log(f"     Saved → {path}\n", "info")
            messagebox.showinfo("Saved", f"Merged collection saved to:\n{path}")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
