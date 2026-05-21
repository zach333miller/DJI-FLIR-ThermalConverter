"""DJI → FLIR Thermal Converter — single-window desktop app.

Flow (whole UI, no checkboxes, no path-typing):
    1. User clicks "Convert a folder".
    2. Native folder picker opens.
    3. Tool scans the folder, finds DJI thermal R-JPEGs, converts each to a
       FLIR-format R-JPEG, writes them to `<folder>_FLIR/`.
    4. "Done" screen with the output path and an "Open output folder" button.

No CLI, no browser, no Python required for the recipient.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import Tk, filedialog, ttk, messagebox
import tkinter as tk

from converter import __version__
from converter.pipeline import convert_folder, ConvertSummary


APP_TITLE = "DJI → FLIR Thermal Converter"


# ---------------------------------------------------------------------------
# Theming — match the dashboard's blueprint aesthetic (dark teal accents).

BG       = "#0c1117"
SURFACE  = "#161c25"
BORDER   = "#22303f"
TEXT     = "#e6edf3"
MUTED    = "#9aa5b1"
ACCENT   = "#00d4ff"
ACCENT_2 = "#0078a8"
OK       = "#3fdb6f"
BAD      = "#ff6b6b"


def _style_widgets(root: Tk) -> None:
    """Apply the dark theme to ttk widgets."""
    root.configure(bg=BG)
    style = ttk.Style(root)
    # Use 'clam' so background colors actually paint on Windows.
    style.theme_use("clam")
    style.configure(
        ".",
        background=BG,
        foreground=TEXT,
        fieldbackground=SURFACE,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        font=("Segoe UI", 10),
    )
    style.configure("TFrame", background=BG)
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure(
        "TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10),
    )
    style.configure(
        "Title.TLabel",
        background=BG, foreground=TEXT,
        font=("Segoe UI Semibold", 18),
    )
    style.configure(
        "Sub.TLabel",
        background=BG, foreground=MUTED,
        font=("Segoe UI", 9),
    )
    style.configure(
        "Card.TLabel",
        background=SURFACE, foreground=TEXT,
        font=("Segoe UI", 10),
    )
    style.configure(
        "CardMuted.TLabel",
        background=SURFACE, foreground=MUTED,
        font=("Segoe UI", 9),
    )
    style.configure(
        "Status.TLabel",
        background=SURFACE, foreground=ACCENT,
        font=("Consolas", 10),
    )
    style.configure(
        "Primary.TButton",
        background=ACCENT_2, foreground="#031018",
        borderwidth=0, padding=(20, 12),
        font=("Segoe UI Semibold", 11),
    )
    style.map(
        "Primary.TButton",
        background=[("active", ACCENT), ("disabled", "#1b2733")],
        foreground=[("disabled", MUTED)],
    )
    style.configure(
        "Secondary.TButton",
        background=SURFACE, foreground=TEXT,
        borderwidth=1, padding=(14, 8),
        font=("Segoe UI", 10),
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#1d2735")],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background=ACCENT, troughcolor=SURFACE,
        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT_2,
    )


# ---------------------------------------------------------------------------
# Main app

class ConverterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("640x460")
        self.root.minsize(560, 400)
        _style_widgets(root)

        self._progress_queue: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._summary: ConvertSummary | None = None
        self._output_dir: Path | None = None

        self._build_idle_view()

    # ---- views ----------------------------------------------------------

    def _clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def _build_idle_view(self) -> None:
        self._clear()
        outer = ttk.Frame(self.root, padding=(36, 28))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text=f"v{__version__}   ·   Factory-grade conversion via DJI Thermal SDK v1.8",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 22))

        card = ttk.Frame(outer, style="Surface.TFrame", padding=(28, 26))
        card.pack(fill="both", expand=True)

        ttk.Label(
            card,
            text=(
                "Pick a folder of DJI photos and this tool will write a new\n"
                "folder next to it containing FLIR-format radiometric JPEGs.\n\n"
                "  •  Mixed folders work — visible photos and videos are\n"
                "     ignored automatically.\n"
                "  •  Output goes to  <folder name>_FLIR/  beside the\n"
                "     selected folder.\n"
                "  •  Files open in FLIR Tools / Thermal Studio with the\n"
                "     same temperature values DJI captured."
            ),
            style="Card.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(0, 24))

        btn = ttk.Button(
            card,
            text="Convert a folder…",
            style="Primary.TButton",
            command=self._pick_and_run,
        )
        btn.pack(anchor="w")

        ttk.Label(
            outer,
            text="Supported cameras: H30T · H20T · M30T · M3T · M4T · M4TD",
            style="Sub.TLabel",
        ).pack(anchor="w", side="bottom", pady=(16, 0))

    def _build_progress_view(self, input_dir: Path, output_dir: Path) -> None:
        self._clear()
        outer = ttk.Frame(self.root, padding=(36, 28))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Converting…", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"Source:  {input_dir}", style="Sub.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"Output:  {output_dir}", style="Sub.TLabel").pack(
            anchor="w", pady=(0, 20)
        )

        card = ttk.Frame(outer, style="Surface.TFrame", padding=(24, 22))
        card.pack(fill="both", expand=True)

        self._progress_label = ttk.Label(
            card, text="Scanning folder…", style="Card.TLabel"
        )
        self._progress_label.pack(anchor="w")

        self._progress_bar = ttk.Progressbar(
            card,
            mode="determinate",
            length=480,
            style="Horizontal.TProgressbar",
        )
        self._progress_bar.pack(fill="x", pady=(12, 12))

        self._progress_counter = ttk.Label(
            card, text="", style="Status.TLabel"
        )
        self._progress_counter.pack(anchor="w")

        self._current_file = ttk.Label(
            card, text="", style="CardMuted.TLabel"
        )
        self._current_file.pack(anchor="w", pady=(6, 0))

    def _build_done_view(self, summary: ConvertSummary) -> None:
        self._clear()
        outer = ttk.Frame(self.root, padding=(36, 28))
        outer.pack(fill="both", expand=True)

        if summary.errors == 0 and summary.converted > 0:
            head = "Done."
        elif summary.converted == 0:
            head = "Finished — nothing converted."
        else:
            head = "Done (with some errors)."
        ttk.Label(outer, text=head, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=f"Output:  {summary.output_dir}",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 18))

        card = ttk.Frame(outer, style="Surface.TFrame", padding=(24, 22))
        card.pack(fill="both", expand=True)

        # Summary numbers
        lines = [
            f"Files scanned:      {summary.scanned}",
            f"Thermal photos:     {summary.thermal_found}",
            f"FLIR files written: {summary.converted}",
        ]
        if summary.errors:
            lines.append(f"Errors:             {summary.errors}")
        for line in lines:
            ttk.Label(card, text=line, style="Card.TLabel").pack(anchor="w")

        # Errors list (if any)
        if summary.errors:
            ttk.Label(
                card,
                text="\nFailures:",
                style="Card.TLabel",
            ).pack(anchor="w", pady=(8, 0))
            err_box = tk.Text(
                card,
                height=6,
                bg=BG,
                fg=BAD,
                insertbackground=TEXT,
                relief="flat",
                font=("Consolas", 9),
                wrap="word",
                borderwidth=0,
            )
            err_box.pack(fill="both", expand=True, pady=(4, 0))
            for r in summary.results:
                if r.status != "error":
                    continue
                err_box.insert("end", f"{r.src.name}\n    {r.detail.splitlines()[0]}\n\n")
            err_box.configure(state="disabled")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(
            actions,
            text="Open output folder",
            style="Primary.TButton",
            command=lambda: _open_in_explorer(summary.output_dir),
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Convert another folder",
            style="Secondary.TButton",
            command=self._build_idle_view,
        ).pack(side="left", padx=(10, 0))

    # ---- actions --------------------------------------------------------

    def _pick_and_run(self) -> None:
        # Native Windows folder picker. Returns "" if user cancelled.
        chosen = filedialog.askdirectory(title="Pick the DJI thermal folder")
        if not chosen:
            return
        input_dir = Path(chosen).resolve()
        if not input_dir.is_dir():
            messagebox.showerror(
                APP_TITLE,
                f"Not a folder:\n{chosen}",
            )
            return
        output_dir = input_dir.parent / f"{input_dir.name}_FLIR"
        self._output_dir = output_dir
        self._build_progress_view(input_dir, output_dir)
        self._start_worker(input_dir, output_dir)
        self.root.after(80, self._drain_queue)

    def _start_worker(self, input_dir: Path, output_dir: Path) -> None:
        def _run() -> None:
            try:
                summary = convert_folder(
                    input_dir,
                    output_dir,
                    on_progress=lambda d, t, f: self._progress_queue.put(
                        ("tick", d, t, f)
                    ),
                )
                self._progress_queue.put(("done", summary))
            except Exception as e:
                self._progress_queue.put(
                    ("error", str(e), traceback.format_exc())
                )

        self._worker = threading.Thread(target=_run, daemon=True)
        self._worker.start()

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self._progress_queue.get_nowait()
                kind = msg[0]
                if kind == "tick":
                    _, done, total, current = msg
                    if total > 0:
                        self._progress_bar.configure(maximum=total, value=done)
                        self._progress_counter.configure(
                            text=f"{done} / {total}"
                        )
                    self._progress_label.configure(
                        text="Converting thermal photos…"
                    )
                    self._current_file.configure(text=f"→ {current}")
                elif kind == "done":
                    summary = msg[1]
                    self._summary = summary
                    self._build_done_view(summary)
                    return
                elif kind == "error":
                    _, err, tb = msg
                    messagebox.showerror(
                        APP_TITLE,
                        f"Conversion failed:\n\n{err}\n\nDetails:\n{tb[-1200:]}",
                    )
                    self._build_idle_view()
                    return
        except queue.Empty:
            pass
        if self._worker and self._worker.is_alive():
            self.root.after(120, self._drain_queue)


def _open_in_explorer(path: Path) -> None:
    """Open `path` in Windows Explorer / Finder / xdg-open as appropriate."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showinfo(APP_TITLE, f"Output folder:\n{path}\n\n({e})")


def main() -> None:
    root = Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
