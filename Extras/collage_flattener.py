import os
import shutil
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --------- Config ---------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

TITLE = "Collage Flattener — Chapter Export"
DARK_BG = "#111315"
MID_BG  = "#1a1d21"
FG      = "#e6e6e6"
SUB_FG  = "#a7adb5"
ENTRY_BG = "#2a2f36"
ENTRY_FG = "#e6e6e6"
BTN_BG   = "#f2f2f2"
BTN_FG   = "#000000"

# --------- Helpers ---------
def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS

def find_collage_dir(chapter_dir: Path) -> Path | None:
    """Return the FIRST folder named 'collage' (case-insensitive) under chapter_dir."""
    # direct child first
    for child in chapter_dir.iterdir():
        if child.is_dir() and child.name.lower() == "collage":
            return child
    # then search deeper
    for root, dirs, _ in os.walk(chapter_dir):
        for d in dirs:
            if d.lower() == "collage":
                return Path(root) / d
    return None

def find_extracttext_dir_exact(chapter_dir: Path) -> Path | None:
    """
    Return the FIRST folder named EXACTLY 'ExtractText' under chapter_dir (case-sensitive as requested).
    We check direct child first, then search deeper (still exact match on name).
    """
    # direct child first
    for child in chapter_dir.iterdir():
        if child.is_dir() and child.name == "ExtractText":
            return child
    # then search deeper
    for root, dirs, _ in os.walk(chapter_dir):
        for d in dirs:
            if d == "ExtractText":
                return Path(root) / d
    return None

def _safe_target(dest_dir: Path, filename: str, overwrite: bool) -> Path:
    base, ext = os.path.splitext(filename)
    candidate = dest_dir / filename
    if overwrite or not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = dest_dir / f"{base}_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1

def copy_images_flat_to_chapter(src_collage: Path, chapter_dst: Path, overwrite: bool, log_cb):
    """
    Copy ONLY image files from src_collage (recursively) **directly into chapter_dst**.
    No extra subfolder. If name collision occurs:
      - overwrite if overwrite=True
      - else auto-rename with suffix (_1, _2, ...) to avoid losing files.
    """
    chapter_dst.mkdir(parents=True, exist_ok=True)

    for root, _, files in os.walk(src_collage):
        for f in files:
            s = Path(root) / f
            if not is_image_file(s):
                continue
            d = _safe_target(chapter_dst, s.name, overwrite)
            try:
                if d.exists() and overwrite:
                    shutil.copy2(s, d)
                elif not d.exists():
                    shutil.copy2(s, d)
                else:
                    # overwrite=False and same name -> _safe_target already gave a unique name
                    shutil.copy2(s, d)
            except Exception as e:
                log_cb(f"   ! Failed to copy {s} -> {d}: {e}")

def copy_combined_ocr_if_present(chapter_dir: Path, chapter_dst: Path, overwrite: bool, log_cb):
    """
    If chapter_dir contains ExtractText/combined_ocr_merged.txt (exact folder name),
    copy that file into chapter_dst with overwrite/auto-rename behavior.
    """
    et_dir = find_extracttext_dir_exact(chapter_dir)
    if not et_dir:
        log_cb(" - No 'ExtractText' folder found. Skipping OCR file.")
        return

    src_txt = et_dir / "combined_ocr_merged.txt"
    if not src_txt.exists() or not src_txt.is_file():
        log_cb(" - 'ExtractText' found but 'combined_ocr_merged.txt' not present. Skipping OCR file.")
        return

    chapter_dst.mkdir(parents=True, exist_ok=True)
    d = _safe_target(chapter_dst, src_txt.name, overwrite)
    try:
        if d.exists() and overwrite:
            shutil.copy2(src_txt, d)
        elif not d.exists():
            shutil.copy2(src_txt, d)
        else:
            shutil.copy2(src_txt, d)  # unique name already provided when overwrite=False
        log_cb(" - OCR file copied: combined_ocr_merged.txt")
    except Exception as e:
        log_cb(f"   ! Failed to copy OCR: {src_txt} -> {d}: {e}")

# --------- App ---------
class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.master: tk.Tk = master
        self.master.title(TITLE)
        self.master.geometry("920x600")
        self.master.minsize(920, 600)
        self.master.configure(bg=DARK_BG)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=False)

        self._init_style()
        self._build_ui()

        self._worker = None
        self._stop_flag = threading.Event()
        self._set_running(False)

    def _init_style(self):
        style = ttk.Style()
        # Use a theme we can recolor
        theme = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(theme)

        # Base colors for ttk
        style.configure(".", background=DARK_BG, foreground=FG)
        style.map("TButton",
                  background=[("active", "#ffffff"), ("!active", BTN_BG)],
                  foreground=[("active", "#000000"), ("!active", BTN_FG)])
        style.configure("TButton", padding=8, relief="flat")
        style.configure("TCheckbutton", background=DARK_BG, foreground=FG, padding=4)
        style.configure("TLabel", background=DARK_BG, foreground=FG)
        style.configure("Header.TLabel", background=DARK_BG, foreground=FG, font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background=DARK_BG, foreground=SUB_FG)
        style.configure("TLabelframe", background=DARK_BG, foreground=FG)
        style.configure("TLabelframe.Label", background=DARK_BG, foreground=FG, font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=ENTRY_FG)
        style.configure("Horizontal.TProgressbar", troughcolor=MID_BG, background="#e0e0e0")

    def _build_ui(self):
        pad = {"padx": 12, "pady": 8}

        header = ttk.Label(self.master, text=TITLE, style="Header.TLabel")
        header.pack(anchor="w", padx=12, pady=(12, 4))

        subtitle = ttk.Label(self.master,
                             text="Copies images from each chapter’s ‘collage’ and the OCR file from ‘ExtractText’ directly into <Output>/<Chapter>/",
                             style="Sub.TLabel")
        subtitle.pack(anchor="w", padx=12, pady=(0, 10))

        # Paths frame
        paths = ttk.Frame(self.master)
        paths.pack(fill="x", **pad)

        ttk.Label(paths, text="Input Root (manhwa):").grid(row=0, column=0, sticky="w")
        e1 = ttk.Entry(paths, textvariable=self.input_var)
        e1.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        b1 = ttk.Button(paths, text="Browse…", command=self._pick_input)
        b1.grid(row=0, column=2)

        ttk.Label(paths, text="Output Root:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        e2 = ttk.Entry(paths, textvariable=self.output_var)
        e2.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(6, 0))
        b2 = ttk.Button(paths, text="Browse…", command=self._pick_output)
        b2.grid(row=1, column=2, pady=(6, 0))

        paths.columnconfigure(1, weight=1)

        # Options
        dest = ttk.LabelFrame(self.master, text="Options")
        dest.pack(fill="x", **pad)
        ttk.Checkbutton(dest, text="Overwrite existing files (if same name found in chapter)", variable=self.overwrite_var)\
            .grid(row=0, column=0, sticky="w")

        # Controls
        controls = ttk.Frame(self.master)
        controls.pack(fill="x", **pad)

        self.run_btn = ttk.Button(controls, text="Start", command=self._start)
        self.run_btn.pack(side="left")

        self.stop_btn = ttk.Button(controls, text="Stop", command=self._stop)
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(controls, orient="horizontal", mode="determinate", style="Horizontal.TProgressbar")
        self.progress.pack(side="right", fill="x", expand=True)

        # Log
        log_frame = ttk.LabelFrame(self.master, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, height=18, wrap="word", bg=MID_BG, fg=FG, insertbackground=FG,
                                relief="flat", highlightthickness=0)
        self.log_text.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        yscroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=yscroll.set)

    # --------- UI helpers ---------
    def _log(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.master.update_idletasks()

    def _pick_input(self):
        p = filedialog.askdirectory(title="Select Input Root (manhwa)")
        if p:
            self.input_var.set(p)

    def _pick_output(self):
        p = filedialog.askdirectory(title="Select Output Root")
        if p:
            self.output_var.set(p)

    def _validate(self):
        in_root = Path(self.input_var.get().strip())
        out_root = Path(self.output_var.get().strip())
        if not in_root.exists() or not in_root.is_dir():
            messagebox.showerror("Invalid Input Root", "Please select a valid input root (e.g., …\\manhwa).")
            return None
        if not out_root.exists():
            try:
                out_root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Invalid Output Root", f"Cannot create output root:\n{e}")
                return None
        return in_root, out_root

    def _set_running(self, running: bool):
        self.run_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def _start(self):
        validated = self._validate()
        if not validated:
            return
        self._stop_flag.clear()
        self._set_running(True)
        self.progress["value"] = 0

        in_root, out_root = validated
        self._worker = threading.Thread(
            target=self._work, args=(in_root, out_root, self.overwrite_var.get()), daemon=True
        )
        self._worker.start()

    def _stop(self):
        if self._worker and self._worker.is_alive():
            self._stop_flag.set()
            self._log("Stop requested…")

    def _work(self, in_root: Path, out_root: Path, overwrite: bool):
        try:
            chapters = [p for p in in_root.iterdir() if p.is_dir()]
            total = len(chapters)
            if total == 0:
                self._log("No chapter folders found under input root.")
                return

            self.progress["maximum"] = total
            self._log(f"Found {total} chapter folder(s). Starting…\n")

            for idx, chapter in enumerate(chapters, start=1):
                if self._stop_flag.is_set():
                    self._log("Stopped by user.")
                    break

                chap_name = chapter.name
                self._log(f"[{idx}/{total}] Chapter: {chap_name}")

                collage_src = find_collage_dir(chapter)
                if not collage_src:
                    self._log(" - No 'collage' folder found (case-insensitive). Skipping images.")
                else:
                    chapter_dst = out_root / chap_name  # <Output>/<Chapter> (no subfolder)
                    self._log(f" - Copying images to: {chapter_dst}")
                    try:
                        copy_images_flat_to_chapter(collage_src, chapter_dst, overwrite, self._log)
                        self._log(" - Images done.")
                    except Exception as e:
                        self._log(f" - ERROR copying images: {e}")
                        # still attempt OCR copy below

                # Always attempt OCR copy (even if collage was missing)
                chapter_dst = out_root / chap_name
                copy_combined_ocr_if_present(chapter, chapter_dst, overwrite, self._log)

                self._log("")  # spacer
                self.progress["value"] = idx
                self.master.update_idletasks()

            self._log("All done.")
        finally:
            self._set_running(False)

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
