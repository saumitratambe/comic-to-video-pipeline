import re
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------- Appearance (dark) ----------------
TITLE   = "Chapter TXT ➜ Narration (Crop Files)"
DARK_BG = "#111315"
MID_BG  = "#1a1d21"
FG      = "#e6e6e6"
SUB_FG  = "#a7adb5"
ENTRY_BG = "#2a2f36"
ENTRY_FG = "#e6e6e6"
BTN_BG   = "#f2f2f2"
BTN_FG   = "#000000"

# ---------------- Natural sort ----------------
import re as _re
def natsort_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in _re.split(r'(\d+)', s)]

# ---------------- Parsing ----------------
# Accepts headers like:
#   Crop1.txt
#   Crop1
#   Crop 1
#   Crop 1:
#   Crop 1 -
#   CROP1.TXT
CROP_HEADER_RE = re.compile(
    r'^\s*Crop\s*#?\s*(\d+)\s*(?:\.?txt)?\s*[:\-]?\s*$', re.IGNORECASE
)

def split_Crops_from_text(text: str) -> dict[int, str]:
    """
    Split a chapter text into {Crop_index: content}.
    If no headers found, treat entire file as Crop1.
    """
    lines = text.splitlines()
    Crops = {}
    current_idx = None
    buffer = []

    def flush(idx, buf):
        if idx is None:
            return
        # join and ensure trailing newline
        content = "\n".join(buf).rstrip() + ("\n" if buf else "")
        Crops[idx] = content

    for ln in lines:
        m = CROP_HEADER_RE.match(ln)
        if m:
            # new Crop starts
            flush(current_idx, buffer)
            buffer = []
            current_idx = int(m.group(1))
        else:
            buffer.append(ln)

    flush(current_idx, buffer)

    if not Crops:
        # No headers -> single Crop1
        Crops[1] = text.rstrip() + ("\n" if text and not text.endswith("\n") else "")
    return Crops

# ---------------- App ----------------
class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.master = master
        self.master.title(TITLE)
        self.master.geometry("960x640")
        self.master.minsize(960, 640)
        self.master.configure(bg=DARK_BG)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.start_ch_var = tk.IntVar(value=1)      # first file -> chapter #
        self.overwrite_var = tk.BooleanVar(value=True)
        self.trim_var = tk.BooleanVar(value=True)   # trim lines / drop blank-only blocks
        self.Narration_folder_var = tk.StringVar(value="Narration")  # exact spelling you use
        self.prefix_var = tk.StringVar(value="Crop")

        self._init_style()
        self._build_ui()

        self._worker = None
        self._stop_flag = threading.Event()
        self._set_running(False)

    def _init_style(self):
        style = ttk.Style()
        theme = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(theme)

        style.configure(".", background=DARK_BG, foreground=FG)
        style.configure("TButton", padding=8, relief="flat")
        style.map("TButton",
                  background=[("active", "#ffffff"), ("!active", BTN_BG)],
                  foreground=[("active", "#000000"), ("!active", BTN_FG)])
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

        ttk.Label(self.master, text=TITLE, style="Header.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(
            self.master,
            text="Each TXT is one chapter. We parse Crop headers and write Crop#.txt into <Output>/<Chapter>/<Narration>.",
            style="Sub.TLabel"
        ).pack(anchor="w", padx=12, pady=(0, 10))

        # Paths
        paths = ttk.Frame(self.master)
        paths.pack(fill="x", **pad)

        ttk.Label(paths, text="Input Folder (chapter .txt files):").grid(row=0, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(paths, text="Browse…", command=self._pick_input).grid(row=0, column=2)

        ttk.Label(paths, text="Output Root (manhwa):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(paths, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(6, 0))
        ttk.Button(paths, text="Browse…", command=self._pick_output).grid(row=1, column=2, pady=(6, 0))

        paths.columnconfigure(1, weight=1)

        # Options
        opts = ttk.LabelFrame(self.master, text="Options")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="First file → Chapter #:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1, to=100000, textvariable=self.start_ch_var, width=8).grid(row=0, column=1, sticky="w", padx=(8, 16))

        ttk.Label(opts, text="Narration folder name:").grid(row=0, column=2, sticky="w")
        ttk.Entry(opts, textvariable=self.Narration_folder_var, width=16).grid(row=0, column=3, sticky="w", padx=(8, 16))

        ttk.Label(opts, text="Output file prefix:").grid(row=0, column=4, sticky="w")
        ttk.Entry(opts, textvariable=self.prefix_var, width=10).grid(row=0, column=5, sticky="w", padx=(8, 0))

        ttk.Checkbutton(opts, text="Overwrite existing Crop files", variable=self.overwrite_var)\
            .grid(row=1, column=0, sticky="w", pady=(6, 0), columnspan=2)
        ttk.Checkbutton(opts, text="Clean: trim lines + drop empty-only blocks", variable=self.trim_var)\
            .grid(row=1, column=2, sticky="w", pady=(6, 0), columnspan=4)

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

    # ---------- Utilities ----------
    def _log(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.master.update_idletasks()

    def _pick_input(self):
        p = filedialog.askdirectory(title="Select Input Folder (chapter TXT files)")
        if p:
            self.input_var.set(p)

    def _pick_output(self):
        p = filedialog.askdirectory(title="Select Output Root (manhwa)")
        if p:
            self.output_var.set(p)

    def _validate(self):
        in_dir = Path(self.input_var.get().strip())
        out_root = Path(self.output_var.get().strip())
        if not in_dir.exists() or not in_dir.is_dir():
            messagebox.showerror("Invalid Input", "Select a valid folder containing chapter .txt files.")
            return None
        if not out_root.exists():
            try:
                out_root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Invalid Output", f"Cannot create output root:\n{e}")
                return None

        txts = sorted([p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() == ".txt"],
                      key=lambda p: natsort_key(p.name))
        if not txts:
            messagebox.showerror("No TXT files", "No .txt files found in the selected input folder.")
            return None
        start_ch = max(1, int(self.start_ch_var.get()))
        Narration_name = self.Narration_folder_var.get().strip() or "Narration"
        prefix = self.prefix_var.get().strip() or "Crop"
        return in_dir, out_root, txts, start_ch, Narration_name, prefix

    def _set_running(self, running: bool):
        self.run_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def _start(self):
        v = self._validate()
        if not v:
            return
        self._stop_flag.clear()
        self._set_running(True)
        self.progress["value"] = 0

        in_dir, out_root, txts, start_ch, Narration_name, prefix = v
        self._worker = threading.Thread(
            target=self._work,
            args=(out_root, txts, start_ch, Narration_name, prefix,
                  self.overwrite_var.get(), self.trim_var.get()),
            daemon=True,
        )
        self._worker.start()

    def _stop(self):
        if self._worker and self._worker.is_alive():
            self._stop_flag.set()
            self._log("Stop requested…")

    def _clean_block(self, s: str, enable: bool) -> str:
        if not enable:
            return s
        # Trim per line and collapse consecutive blank lines to single blank (optional)
        lines = [ln.rstrip() for ln in s.splitlines()]
        # drop fully empty lines at start & end of block
        while lines and lines[0].strip() == "":
            lines.pop(0)
        while lines and lines[-1].strip() == "":
            lines.pop()
        return ("\n".join(lines) + ("\n" if lines else ""))

    def _work(self, out_root: Path, txts: list[Path], start_ch: int, Narration_name: str,
              prefix: str, overwrite: bool, do_trim: bool):
        try:
            total = len(txts)
            self.progress["maximum"] = total
            self._log(f"Found {total} chapter TXT file(s). Starting…\n")

            for file_idx, chapter_file in enumerate(txts, start=0):
                if self._stop_flag.is_set():
                    self._log("Stopped by user.")
                    break

                chap_num = start_ch + file_idx   # 1st file -> start_ch, 2nd -> start_ch+1, ...
                chap_dir = out_root / str(chap_num)
                Narration_dir = chap_dir / Narration_name
                Narration_dir.mkdir(parents=True, exist_ok=True)

                # Read and split Crops
                try:
                    raw = chapter_file.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    self._log(f"[{chap_num}] ERROR reading {chapter_file.name}: {e}")
                    self.progress["value"] = file_idx + 1
                    continue

                Crop_map = split_Crops_from_text(raw)
                count = len(Crop_map)
                self._log(f"[{chap_num}] {chapter_file.name}: {count} Crop section(s)")

                # Write Crop#.txt
                wrote = 0
                for idx in sorted(Crop_map.keys()):
                    content = self._clean_block(Crop_map[idx], do_trim)
                    dst = Narration_dir / f"{prefix}{idx}.txt"
                    try:
                        if dst.exists() and not overwrite:
                            # keep existing
                            pass
                        else:
                            dst.write_text(content, encoding="utf-8")
                            wrote += 1
                    except Exception as e:
                        self._log(f"   ! ERROR writing {dst.name}: {e}")

                self._log(f"   -> Saved {wrote}/{count} files to {Narration_dir}\n")

                self.progress["value"] = file_idx + 1
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
