# manhwa_batch_logic_updated.py
# -----------------------------------------------------------
# Manhwa Batch Renderer (Fixed Zoom-Out + Fixed Vertical Pan + 7px Stroke)
# -----------------------------------------------------------

import os, shutil, subprocess, tempfile, threading
from pathlib import Path
from natsort import natsorted
from PIL import Image, ImageFilter
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QFileDialog, QLineEdit, QGroupBox, QMessageBox, QMainWindow
)
from PySide6.QtCore import QThread, Signal, Slot, Qt

# =======================
# Configuration
# =======================
TARGET_W, TARGET_H = 1920, 1080
FPS = 30
OUT_DIRNAME = "Output"

ENCODER = "libx264"
PRESET = "ultrafast"
CRF = "24"
AUDIO_BITRATE = "96k"

MAX_FOREGROUND_RATIO = 0.70
FG_GROWTH = 1.40
BORDER_PX = 0
PAN_BG_HEIGHT = int(3000 * 0.4)  # 2250px background for vertical pan
MIN_DURATION = 0.10


# ------------------------
# Utility
# ------------------------
def check_tools():
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            return False, t
    return True, None


def get_audio_duration(audio_path: Path) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dur = float(result.stdout.strip())
        return max(dur, MIN_DURATION)
    except Exception:
        return 1.0


def list_pairs(chapter_dir: Path):
    crop = chapter_dir / "Crop"
    voice = chapter_dir / "Voice"
    if not crop.exists() or not voice.exists():
        return []
    imgs = natsorted([p for p in crop.iterdir() if p.is_file()])
    auds = natsorted([p for p in voice.iterdir() if p.is_file()])
    m1, m2 = {p.stem: p for p in imgs}, {p.stem: p for p in auds}
    keys = natsorted(set(m1.keys()) & set(m2.keys()))
    return [(m1[k], m2[k]) for k in keys]


def make_even(n): return n + (n % 2)


def compute_fg_dims(image_path: Path):
    with Image.open(image_path) as im:
        w, h = im.size
    base_max_w = TARGET_W * MAX_FOREGROUND_RATIO
    base_max_h = TARGET_H * MAX_FOREGROUND_RATIO
    base_scale = min(base_max_w / w, base_max_h / h, 1.0)
    scale = base_scale * FG_GROWTH
    new_w = make_even(int(w * scale))
    new_h = make_even(int(h * scale))
    new_w = min(new_w, TARGET_W)
    new_h = min(new_h, TARGET_H)
    return new_w, new_h


# ==========================================================
# Composite Builder
# ==========================================================
def build_composite_png(src_img: Path, out_png: Path, canvas_h: int = TARGET_H):
    """Build blurred BG + FG + stroke."""
    img = Image.open(src_img).convert("RGBA")

    bg_src = img.convert("RGB")
    scale = max(TARGET_W / bg_src.width, canvas_h / bg_src.height)
    cover_w = int(bg_src.width * scale)
    cover_h = int(bg_src.height * scale)
    bg = bg_src.resize((cover_w, cover_h), Image.LANCZOS)
    left = (cover_w - TARGET_W) // 2
    top = (cover_h - canvas_h) // 2
    bg = bg.crop((left, top, left + TARGET_W, top + canvas_h))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=20)).convert("RGBA")

    fg_w, fg_h = compute_fg_dims(src_img)
    fg = img.copy().resize((fg_w, fg_h), Image.LANCZOS)
    pad = BORDER_PX
    bordered = Image.new("RGBA", (fg_w + pad * 2, fg_h + pad * 2), (0, 0, 0, 255))
    bordered.paste(fg, (pad, pad), fg)

    canvas = bg.copy()
    x = (TARGET_W - bordered.width) // 2
    y = (canvas_h - bordered.height) // 2
    canvas.alpha_composite(bordered, (x, y))
    canvas.save(out_png, "PNG")


# ==========================================================
# Dynamic Zoom Expression
# ==========================================================
def dynamic_zoom_expr(image_path: Path):
    """Adjust zoom-out strength based on image height."""
    try:
        with Image.open(image_path) as im:
            _, h = im.size
        # Gentle zoom for near-square / short images
        start_zoom = 1.5 if h < 1000 else 2.5
        return f"if(lte(zoom,1.0),{start_zoom},max(1.001,zoom-0.0015))"
    except Exception:
        return "if(lte(zoom,1.0),2.0,max(1.001,zoom-0.0015))"


# ==========================================================
# FFmpeg Helpers
# ==========================================================
def ff_zoom_from_image(img_path: Path, comp_png: Path, dur: float, out_vid: Path, zoom_in: bool):
    d_frames = max(1, int(round(dur * FPS)))
    zexpr = "zoom+0.001" if zoom_in else dynamic_zoom_expr(img_path)
    vf = (
        f"scale=8000:-1,"
        f"zoompan=z='{zexpr}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={d_frames}:s={TARGET_W}x{TARGET_H}:fps={FPS}"
    )
    return [
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(comp_png),
        "-t", f"{dur:.3f}", "-vf", vf,
        "-c:v", ENCODER, "-preset", PRESET, "-crf", CRF,
        "-pix_fmt", "yuv420p", str(out_vid)
    ]


def ff_pan_from_image(comp_png: Path, dur: float, out_vid: Path, direction: str):
    direction = direction.lower().strip()
    if direction not in ("down", "up"):
        raise ValueError("direction must be 'down' or 'up'")
    if direction == "down":
        crop_y = f"'(in_h-1080)*((t/{dur:.6f})*(t/{dur:.6f})*(3-2*(t/{dur:.6f})))'"
    else:
        crop_y = f"'(in_h-1080)*(1-(t/{dur:.6f})*(t/{dur:.6f})*(3-2*(t/{dur:.6f})))'"
    vf = f"crop={TARGET_W}:{TARGET_H}:0:{crop_y}"
    return [
        "ffmpeg", "-y", "-loop", "1", "-i", str(comp_png),
        "-t", f"{dur:.3f}", "-r", str(FPS),
        "-vf", vf, "-pix_fmt", "yuv420p",
        "-c:v", ENCODER, "-preset", PRESET, "-crf", CRF,
        str(out_vid)
    ]


def ff_mux(video: Path, audio: Path, outp: Path):
    return [
        "ffmpeg", "-y",
        "-i", str(video), "-i", str(audio),
        "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest", str(outp)
    ]


# ==========================================================
# Worker Thread
# ==========================================================
class ManhwaBatchWorker(QThread):
    log_message = Signal(str)
    finished_signal = Signal()
    error_signal = Signal(str)

    def __init__(self, root_path: Path):
        super().__init__()
        self.root = root_path
        self.stop_flag = threading.Event()

    def log(self, t): self.log_message.emit(t)

    def run(self):
        try:
            outd = self.root / OUT_DIRNAME
            outd.mkdir(exist_ok=True)
            chaps = [p for p in self.root.iterdir() if p.is_dir() and p.name != OUT_DIRNAME]
            chap_idx = 1
            for chap in natsorted(chaps):
                if self.stop_flag.is_set(): break
                pairs = list_pairs(chap)
                if not pairs:
                    self.log(f"⚠ No valid pairs in {chap.name}")
                    continue
                self.log(f"▶ Chapter: {chap.name}")

                with tempfile.TemporaryDirectory(prefix=f"mwb_{chap.name}_") as tmpd:
                    tmp = Path(tmpd)
                    segs = []
                    for i, (img, aud) in enumerate(pairs):
                        if self.stop_flag.is_set(): break
                        dur = get_audio_duration(aud)
                        self.log(f"  {i+1}) {img.stem} → {dur:.2f}s")

                        comp = tmp / f"comp_{i}.png"
                        try:
                            eff = i % 4
                            if eff in (2, 3):
                                build_composite_png(img, comp, canvas_h=PAN_BG_HEIGHT)
                            else:
                                build_composite_png(img, comp, canvas_h=TARGET_H)
                        except Exception as e:
                            self.error_signal.emit(str(e)); return

                        vid = tmp / f"vid_{i}.mp4"
                        if eff == 0:
                            cmd = ff_zoom_from_image(img, comp, dur, vid, zoom_in=True); effname="Zoom-In"
                        elif eff == 1:
                            cmd = ff_zoom_from_image(img, comp, dur, vid, zoom_in=False); effname="Zoom-Out"
                        elif eff == 2:
                            cmd = ff_pan_from_image(comp, dur, vid, "down"); effname="Pan-Down"
                        else:
                            cmd = ff_pan_from_image(comp, dur, vid, "up"); effname="Pan-Up"

                        if not self._run(cmd):
                            self.error_signal.emit(f"FFmpeg failed {effname}"); return

                        seg = tmp / f"seg_{i}.mp4"
                        if not self._run(ff_mux(vid, aud, seg)):
                            self.error_signal.emit(f"Mux failed {img.name}"); return
                        segs.append(seg)

                    if not segs: continue
                    lst = tmp / "list.txt"
                    with open(lst, "w") as f:
                        for s in segs: f.write(f"file '{s.name}'\n")
                    outp = outd / f"Chapter {chap_idx:02d}.mp4"
                    chap_idx += 1
                    concat_cmd = [
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c:v", ENCODER, "-preset", PRESET, "-crf", CRF,
                        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                        "-pix_fmt", "yuv420p", str(outp)
                    ]
                    if not self._run_concat(concat_cmd, tmp):
                        self.error_signal.emit(f"Concat failed {chap.name}"); return
                    self.log(f"✅ Saved: {outp.name}")

            self.finished_signal.emit()
            self.log("🎬 All done.")
        except Exception as e:
            self.error_signal.emit(str(e))

    def _run(self, cmd):
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        p.stderr.read(); p.wait()
        try: p.stderr.close()
        except: pass
        return p.returncode == 0

    def _run_concat(self, cmd, cwd):
        p = subprocess.Popen(cmd, cwd=cwd, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        p.stderr.read(); p.wait()
        try: p.stderr.close()
        except: pass
        return p.returncode == 0

    def stop(self): self.stop_flag.set()


# ==========================================================
# GUI
# ==========================================================
class ManhwaBatchTab(QWidget):
    operation_finished = Signal()

    def __init__(self):
        super().__init__()
        self.root_path = Path()
        self.worker = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        grp = QGroupBox("Root Folder")
        g_lay = QHBoxLayout(grp)
        self.root_entry = QLineEdit()
        browse = QPushButton("Browse"); browse.clicked.connect(self._browse)
        g_lay.addWidget(QLabel("Root:")); g_lay.addWidget(self.root_entry); g_lay.addWidget(browse)
        layout.addWidget(grp)

        ctrl = QGroupBox("Control")
        c_lay = QHBoxLayout(ctrl)
        self.start_btn = QPushButton("START Batch Render")
        self.start_btn.setStyleSheet("background:#4CAF50;color:white;padding:6px;")
        self.start_btn.clicked.connect(self.start_task)
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet("background:#f44336;color:white;padding:6px;")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        c_lay.addWidget(self.start_btn); c_lay.addWidget(self.stop_btn)
        layout.addWidget(ctrl)

        log_grp = QGroupBox("Log Output")
        log_lay = QVBoxLayout(log_grp)
        self.log_output = QTextEdit(); self.log_output.setReadOnly(True)
        log_lay.addWidget(self.log_output)
        layout.addWidget(log_grp)
        layout.addStretch()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Root Folder")
        if d: self.root_entry.setText(d); self.root_path = Path(d)

    @Slot()
    def start_task(self):
        d = self.root_entry.text().strip()
        if not d:
            QMessageBox.warning(self, "Error", "Select root folder first."); return
        self.root_path = Path(d)
        ok, miss = check_tools()
        if not ok:
            QMessageBox.critical(self, "Error", f"Missing tool: {miss}"); return
        self.log_output.clear()
        self._log(f"Starting render for {self.root_path}")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker = ManhwaBatchWorker(self.root_path)
        self.worker.log_message.connect(self._log)
        self.worker.error_signal.connect(self._task_error)
        self.worker.finished_signal.connect(self._task_finished)
        self.worker.start()

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._log("Stopping...")

    @Slot(str)
    def _log(self, t): self.log_output.append(t)

    @Slot()
    def _task_finished(self):
        self._log("Done.")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None

    @Slot(str)
    def _task_error(self, msg):
        QMessageBox.critical(self, "Error", msg)
        self._log(f"❌ {msg}")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None

    def cleanup(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop(); self.worker.wait(2000)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Manhwa Batch Renderer — Dynamic Zoom Fixed")
        self.tab = ManhwaBatchTab()
        self.setCentralWidget(self.tab)
    def closeEvent(self, e):
        self.tab.cleanup(); super().closeEvent(e)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    win = MainWindow(); win.resize(900, 600); win.show()
    sys.exit(app.exec())