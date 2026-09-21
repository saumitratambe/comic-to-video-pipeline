# from dotenv import load_dotenv # type: ignore
# load_dotenv()
import os
import sys, os, subprocess
from pathlib import Path
DOWNLOADER_SCRIPT = str(Path(__file__).parent / "manhwa_image_download.py")

import os
import threading
from PySide6.QtWidgets import QTextEdit, QLineEdit, QFileDialog
from PySide6.QtCore import Qt, Signal
import os, re, json, threading, time, queue, random, requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog,
    QPlainTextEdit, QHBoxLayout, QMessageBox, QCheckBox, QSpinBox
)


from PySide6.QtWidgets import QTextEdit, QLineEdit, QFileDialog, QPlainTextEdit, QCheckBox

import sys
import time
import subprocess
from pathlib import Path
from natsort import natsorted
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QFileDialog, QLineEdit, QTabWidget,
    QMessageBox, QGroupBox
)
from PySide6.QtCore import QEvent, QObject, Qt, QSize, Slot, Signal
from PySide6.QtGui import QIcon, QFont, QColor



# --- UTILITY CLASS FOR MISSING MODULES ---

class ImageDownloaderTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        title = QLabel("Manhwa Image Downloader (Original)")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)

        info = QLabel("Click to open the original image downloader tool.")
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        btn = QPushButton("Open Downloader")
        btn.setMinimumHeight(42)
        btn.clicked.connect(self.open_downloader)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

        layout.addStretch()

    def open_downloader(self):
        if not os.path.exists(DOWNLOADER_SCRIPT):
            QMessageBox.critical(self, "Missing File",
                f"Cannot find:\n{DOWNLOADER_SCRIPT}")
            return

        python = sys.executable or "python"
        subprocess.Popen([python, DOWNLOADER_SCRIPT])

class MissingModulePlaceholder(QWidget):
    def __init__(self, name):
        super().__init__()
        self.operation_finished = Signal()
        self.setLayout(QVBoxLayout())
        label = QLabel(f'The <b>{name}</b> tab requires external file logic or dependencies.')
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout().addWidget(label)
    def cleanup(self):
        pass
    @Slot()
    def start_task(self):
        self.operation_finished.emit()

# --- IMPORT FUNCTIONAL TABS ---

try:
    from narration_logic import NarrationGeminiTab
except ImportError:
    NarrationGeminiTab = lambda: MissingModulePlaceholder("Narration")

try:
    from narration_logic import NarrationGeminiTab1
except ImportError:
    NarrationGeminiTab1 = lambda: MissingModulePlaceholder("Narration")

try:
    from tts_logic import TtsTab
except ImportError:
    TtsTab = lambda: MissingModulePlaceholder("TTS")

try:
    from polish_logic import PolishTab
except ImportError:
    PolishTab = lambda: MissingModulePlaceholder("Polish")

try:
    from manhwa_batch_logic import ManhwaBatchTab
except ImportError:
    ManhwaBatchTab = lambda: MissingModulePlaceholder("Videos Compose (Batch)")

# --- IMPORT THE MERGE ALL TAB FROM merge_logic.py ---
try:
    from merge_logic import MergeAllTab
except ImportError as e:
    print(f"FATAL ERROR: Could not import 'MergeAllTab'. Check that 'merge_logic.py' exists and all PySide6 classes are imported correctly in that file. Error: {e}")
    MergeAllTab = lambda: MissingModulePlaceholder("Merge All (FATAL)")

# --- Manhwa Image Downloader (launches Tkinter app in separate process) ---
from pathlib import Path as _Path

DOWNLOADER_PATH = str(_Path(__file__).parent / "manhwa_image_download.py")

class DownloaderTab(QWidget):
    log_signal = Signal(str)
    finish_signal = Signal()

    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()

        lay = QVBoxLayout(self)

        title = QLabel("Manhwa Image Downloader (Embedded)")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        lay.addWidget(title)

        # URLs
        lay.addWidget(QLabel("Chapter URLs (one per line):"))
        self.url_box = QPlainTextEdit()
        self.url_box.setMinimumHeight(120)
        lay.addWidget(self.url_box)

        # Output + browse
        row = QHBoxLayout()
        self.out_edit = QLineEdit()
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_folder)
        row.addWidget(QLabel("Output:"))
        row.addWidget(self.out_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        # Options row: Reverse, Resize (enable + W/H)
        opts = QHBoxLayout()
        self.reverse_chk = QCheckBox("Auto-sort chapters (1 → 2 → 3)")
        self.reverse_chk.setChecked(True)
        opts.addWidget(self.reverse_chk)

        self.resize_chk = QCheckBox("Resize pages")
        self.resize_chk.setChecked(False)
        opts.addWidget(self.resize_chk)

        opts.addWidget(QLabel("Max W:"))
        self.w_spin = QSpinBox(); self.w_spin.setRange(200, 4096); self.w_spin.setValue(1080)
        opts.addWidget(self.w_spin)

        opts.addWidget(QLabel("Max H:"))
        self.h_spin = QSpinBox(); self.h_spin.setRange(200, 4096); self.h_spin.setValue(1920)
        opts.addWidget(self.h_spin)

        lay.addLayout(opts)

        # Buttons
        row2 = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        row2.addStretch(1)
        row2.addWidget(self.start_btn)
        row2.addWidget(self.stop_btn)
        lay.addLayout(row2)

        # Status + logs
        self.status_lbl = QLabel("Idle")
        lay.addWidget(self.status_lbl)
        self.log_box = QPlainTextEdit(); self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(240)
        lay.addWidget(self.log_box, 1)

        # Signals
        self.log_signal.connect(self._log)
        self.finish_signal.connect(self._done)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

        # Defaults
        self.session = requests.Session()
        self.session.headers.update({"User-Agent":"Mozilla/5.0"})

    # ---------- UI helpers ----------
    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if d: self.out_edit.setText(d)

    def _log(self, msg:str):
        self.log_box.appendPlainText(msg)

    def _done(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText("Completed ✅")
        QMessageBox.information(self, "Done", "All downloads completed.")

    # ---------- Start/Stop ----------
    def _start(self):
        urls = [u.strip() for u in self.url_box.toPlainText().splitlines() if u.strip().startswith("http")]
        if not urls:
            QMessageBox.warning(self, "No URLs", "Paste chapter URLs first.")
            return
        out = self.out_edit.text().strip()
        if not out:
            QMessageBox.warning(self, "No folder", "Pick output folder.")
            return

        # sort chapters 1->N regardless of paste order
        if self.reverse_chk.isChecked():
            urls = self._sort_chapter_urls(urls)

        self.log_box.clear()
        self.status_lbl.setText("Running…")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.stop_event.clear()

        # pack settings for thread
        settings = {
            "out_dir": out,
            "resize": self.resize_chk.isChecked(),
            "max_w": self.w_spin.value(),
            "max_h": self.h_spin.value(),
            "timeout": 25,
            "threads": 6,
            "min_delay": 80,
            "max_delay": 180,
        }

        def run():
            try:
                self._download_chapters(urls, settings)
            except Exception as e:
                self.log_signal.emit(f"[ERROR] {e}")
            finally:
                self.finish_signal.emit()

        threading.Thread(target=run, daemon=True).start()

    def _stop(self):
        self.stop_event.set()
        self.log_signal.emit("🛑 Stop requested…")
        self.status_lbl.setText("Stopping…")

    # ---------- Core (inlined) ----------
    def _sort_chapter_urls(self, urls):
        def num(u):
            m = re.search(r"/chapter[-_]?(\d+)", u)
            if m: return int(m.group(1))
            m = re.search(r"(\d+)(?:/)?$", u.rstrip("/"))
            return int(m.group(1)) if m else 10**9
        return [u for _,u in sorted([(num(u),u) for u in urls], key=lambda x:x[0])]

    def _extract_ts_reader(self, html):
        out = []
        try:
            m = re.search(r"ts_reader\.run\(\s*(\{.*?\})\s*\)", html, re.DOTALL)
            if not m: return out
            data = json.loads(m.group(1))
            imgs = data.get("images") or data.get("sources") or []
            for x in imgs:
                if isinstance(x, str): out.append(x.split("?")[0])
                elif isinstance(x, dict) and "src" in x: out.append(x["src"].split("?")[0])
        except Exception:
            pass
        return out

    def _best_from_srcset(self, srcset):
        try:
            parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
            return parts[-1] if parts else None
        except Exception:
            return None

    def _extract_images(self, html):
        imgs = self._extract_ts_reader(html)
        if imgs:  # JSON preserves order
            seen, keep = set(), []
            for u in imgs:
                if u and u not in seen:
                    seen.add(u); keep.append(u)
            return keep

        soup = BeautifulSoup(html, "lxml")
        scope = soup.select_one(".reading-content") or soup
        cand, seen = [], set()
        for img in scope.find_all("img"):
            u = img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
            if not u and img.get("srcset"): u = self._best_from_srcset(img["srcset"])
            if not u: u = img.get("src")
            if not u: continue
            u = u.split("?")[0]
            if u.startswith("http") and u not in seen:
                seen.add(u); cand.append(u)
        return cand

    def _save_resized(self, content, dest, max_w, max_h):
        try:
            im = Image.open(BytesIO(content))
            im.load()
            im.thumbnail((max_w, max_h), Image.LANCZOS)
            # keep format if possible
            ext = os.path.splitext(dest)[1].lower()
            if ext in (".jpg",".jpeg"): fmt = "JPEG"
            elif ext == ".png": fmt = "PNG"
            elif ext == ".webp": fmt = "WEBP"
            else: fmt = "JPEG"; dest = dest + ".jpg"
            im.save(dest, fmt, quality=90, optimize=True)
            return dest
        except Exception:
            # fallback write as-is
            with open(dest, "wb") as f:
                f.write(content)
            return dest

    def _download_one(self, url, dest, timeout, resize, max_w, max_h):
        try:
            r = self.session.get(url, timeout=timeout, stream=True)
            if r.status_code != 200:
                self.log_signal.emit(f"  ❌ HTTP {r.status_code}: {url}")
                return False
            # guess extension
            ext = os.path.splitext(urlparse(url).path)[1].lower()
            if not ext or len(ext) > 5:
                ct = r.headers.get("Content-Type","")
                if "png" in ct: ext = ".png"
                elif "webp" in ct: ext = ".webp"
                elif "gif" in ct: ext = ".gif"
                else: ext = ".jpg"
            if not dest.endswith(ext): dest = dest + ext

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            content = r.content
            if resize:
                saved = self._save_resized(content, dest, max_w, max_h)
            else:
                with open(dest, "wb") as f: f.write(content)
                saved = dest
            self.log_signal.emit(f"  ✅ {os.path.basename(saved)}")
            return True
        except Exception as e:
            self.log_signal.emit(f"  ❌ failed: {url} ({e})")
            return False

    def _download_chapters(self, urls, s):
        for idx, url in enumerate(urls, start=1):
            if self.stop_event.is_set():
                self.log_signal.emit("🛑 Stopped.")
                return

            # page html
            try:
                host = urlparse(url).scheme + "://" + urlparse(url).netloc
                self.session.headers.update({"Referer": host})
                html = self.session.get(url, timeout=s["timeout"]).text
            except Exception as e:
                self.log_signal.emit(f"[ERR] {url} -> {e}")
                continue

            chapter_num = self._chapter_num(url) or (idx)
            folder = os.path.join(s["out_dir"], f"Chapter-{chapter_num:03d}")
            self.log_signal.emit(f"\n🔗 Chapter {chapter_num}: {url}")

            images = self._extract_images(html)
            self.log_signal.emit(f"  Found {len(images)} images")

            if not images:
                continue

            q = queue.Queue()
            for i,u in enumerate(images, start=1):
                q.put((i,u))

            def worker():
                while not q.empty() and not self.stop_event.is_set():
                    i,u = q.get()
                    dest = os.path.join(folder, f"{i:03d}")
                    self._download_one(u, dest, s["timeout"], s["resize"], s["max_w"], s["max_h"])
                    q.task_done()
                    time.sleep(random.uniform(s["min_delay"]/1000, s["max_delay"]/1000))

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(s["threads"])]
            for t in threads: t.start()
            for t in threads: t.join()

        self.log_signal.emit("\n🎉 Completed.")

    def _chapter_num(self, url):
        m = re.search(r"/chapter[-_]?(\d+)", url)
        if m: return int(m.group(1))
        m = re.search(r"(\d+)(?:/)?$", url.rstrip("/"))
        return int(m.group(1)) if m else None


# --- STYLING (QSS) ---
STYLE_SHEET = """
QWidget {
    background-color: #DFF3FA;          /* Slightly lighter for balance */
    color: #1E1E1E;
    font-size: 10pt;
    font-family: "Segoe UI", sans-serif;
}

/* Tabs */
QTabWidget::pane {
    border: 1px solid #3A9CCF;
    background-color: #E8F7FB;
}

QTabBar::tab {
    background: #58B1D8;
    border: 1px solid #3A9CCF;
    border-bottom-color: #E8F7FB;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 8px 15px;
    margin-right: 2px;
    color: #FFFFFF;
    font-weight: 500;
}

QTabBar::tab:selected {
    background: #2A90C4;
    border-color: #2A90C4;
    color: #FFFFFF;
}

QTabBar::tab:hover {
    background: #45A2CE;
}

/* Buttons */
QPushButton {
    background-color: #2A90C4;          /* Deep blue for strong contrast */
    border: 1px solid #1E6A93;
    border-radius: 5px;
    padding: 6px 12px;
    color: #FFFFFF;
    font-weight: bold;
}

QPushButton:hover {
    background-color: #247BAA;          /* Noticeably darker hover */
}

QPushButton:pressed {
    background-color: #1E6A93;          /* Even darker pressed */
    border: 1px solid #155471;
}

QPushButton:disabled {
    background-color: #A0C9DA;
    border: 1px solid #7FB3C6;
    color: #E0E0E0;
}

/* Group Boxes */
QGroupBox {
    border: 1px solid #3A9CCF;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
    color: #2B2B2B;
    background-color: #EAF8FB;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 5px;
    color: #2B2B2B;
}

/* Inputs */
QLineEdit, QTextEdit {
    border: 1px solid #3A9CCF;
    background-color: #FFFFFF;
    padding: 5px;
    border-radius: 4px;
    color: #222222;
}

QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #2A90C4;
    background-color: #F9FEFF;
}

/* Progress Bars */
QProgressBar {
    border: 1px solid #3A9CCF;
    border-radius: 5px;
    text-align: center;
    background-color: #E8F7FB;
    color: #1E1E1E;
}

QProgressBar::chunk {
    background-color: #2E7D32;          /* Rich green */
    border-radius: 5px;
}

/* Checkboxes & Radio Buttons (better visibility) */
QCheckBox, QRadioButton {
    spacing: 5px;
    color: #1E1E1E;
    font-weight: 500;
}

QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #2A90C4;
    background-color: #FFFFFF;
    border-radius: 3px;
}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: #2A90C4;
    border: 1px solid #1E6A93;
}

/* Scrollbars (visible) */
QScrollBar:vertical {
    border: none;
    background: #E6F4F8;
    width: 12px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #2A90C4;
    min-height: 20px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #247BAA;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    background: none;
    border: none;
}

"""

# --- MODIFIED: Pipeline Orchestrator Class for Concurrent Loop ---
class PipelineOrchestrator(QObject):
    """Manages the step-by-step processing for a batch of chapters, enabling concurrent Merge/ImageGen/Narration/TTS/Compose."""

    # ALL FIVE STEPS are sequential for a single chapter.
    STEP_SEQUENCE = [
        ('MergeAll', 'start_single_chapter_task'),
        ('ImageGen', 'start_task'),
        ('Narration', 'start_task'),
        ('TTS', 'start_task'),
        ('ManhwaBatch', 'start_task')
    ]

    # NEW: Define the linear chain for the second phase
    COMPLETION_CHAIN = ['Narration', 'TTS', 'ManhwaBatch']

    # Concurrent initiation ends after ImageGen.
    CONCURRENT_BREAK_STEP_INDEX = 1 # ImageGen is index 1

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.tabs = main_window.tab_widgets
        self.current_chapter_index = -1 # Index of the chapter currently running Merge/ImageGen
        self.all_chapter_paths = []
        self.is_running_batch = False
        self.current_step_index = -1 # Step index within STEP_SEQUENCE for Merge/ImageGen

        self.total_chapters_completed = 0
        self.chapters_running_completion_steps = set() # Tracks which chapters are in Narration->TTS->Compose

        self._connect_sequence_signals()

    def _connect_sequence_signals(self):
        """
        Connects the finish signals to manage the concurrent flow.
        """

        # --- MERGE/IMAGEGEN HANDOFFS (Sequential for one chapter) ---
        # 0. MergeAll finish -> Start ImageGen (Step 1 for current chapter)
        if hasattr(self.tabs.get('MergeAll'), 'operation_finished'):
            self.tabs['MergeAll'].operation_finished.connect(
                lambda: self._start_next_step()
            )

        # 1. ImageGen finish (CRITICAL CONCURRENT TRIGGER)
        if hasattr(self.tabs.get('ImageGen'), 'operation_finished'):
            # 1a. Start the next chapter's Merge (advance index + restart sequence from step 0)
            self.tabs['ImageGen'].operation_finished.connect(
                lambda: self._start_next_chapter_concurrent()
            )
            # 1b. Start the current chapter's remaining sequence (Narration -> TTS -> Compose)
            self.tabs['ImageGen'].operation_finished.connect(
                lambda: self._start_completion_steps_for_chapter(self.current_chapter_index)
            )

        # --- NARRATION/TTS/COMPOSE HANDOFFS (Sequential for the *same* chapter, concurrently across chapters) ---

        # 2. Narration finish -> Start TTS for the same chapter
        if hasattr(self.tabs.get('Narration'), 'operation_finished'):
            self.tabs['Narration'].operation_finished.connect(
                lambda: self._handle_completion_chain_handoff('Narration')
            )

        # 3. TTS finish -> Start Compose for the same chapter
        if hasattr(self.tabs.get('TTS'), 'operation_finished'):
            self.tabs['TTS'].operation_finished.connect(
                lambda: self._handle_completion_chain_handoff('TTS')
            )

        # 4. Compose finish -> Mark the chapter as fully complete.
        if hasattr(self.tabs.get('ManhwaBatch'), 'operation_finished'):
            self.tabs['ManhwaBatch'].operation_finished.connect(
                lambda: self._on_chapter_fully_complete()
            )

    # REMOVED: _advance_completion_step

    @Slot()
    def _on_chapter_fully_complete(self):
        """Called when the Compose step finishes for a chapter."""
        self.total_chapters_completed += 1

        # Remove the chapter from the set of actively running completion steps
        # NOTE: We can't safely know which index finished without changing the worker signals.
        # We'll rely on the total count for now.

        print(f"ORCHESTRATOR: A chapter fully completed. Total completed: {self.total_chapters_completed}/{len(self.all_chapter_paths)}")

        if self.total_chapters_completed == len(self.all_chapter_paths):
             self._show_final_completion()

    def _get_chapter_paths(self, root_dir):
        """Helper function to list chapters with images (used for initial input)."""
        chapter_paths = []
        image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
        if not os.path.isdir(root_dir): return []
        try:
            for item in natsorted(os.listdir(root_dir)):
                full_path = os.path.join(root_dir, item)
                if os.path.isdir(full_path):
                    if any(f.lower().endswith(image_exts) for f in os.listdir(full_path)):
                        chapter_paths.append(full_path)
            return chapter_paths
        except Exception as e:
            print(f"Error listing chapters: {e}")
            return []

    @Slot()
    def start_batch_loop(self):
        """Initializes the chapter list and starts the MergeAll step on the first chapter."""
        merge_tab = self.tabs.get('MergeAll')

        if not merge_tab.is_auto_run_checked():
            print("ORCHESTRATOR: Auto-Run not checked. Aborting loop start.")
            return

        root_dir = merge_tab.root_input_line.text().strip()

        if not os.path.isdir(root_dir):
            QMessageBox.critical(self.main_window, "Input Error", "Invalid Chapter Root Folder from Merge Tab.")
            return

        self.all_chapter_paths = self._get_chapter_paths(root_dir)

        if not self.all_chapter_paths:
            QMessageBox.warning(self.main_window, "Chapter Error", "No chapter folders found in the specified root.")
            return

        self.is_running_batch = True
        self.current_chapter_index = 0
        self.current_step_index = 0
        self.total_chapters_completed = 0
        self.chapters_running_completion_steps.clear()

        print(f"ORCHESTRATOR: Starting CONCURRENT loop for {len(self.all_chapter_paths)} chapters.")
        self._run_current_step() # Start MergeAll for Chapter 1
        current_tab._already_auto_started = False   # allow auto-start again
        getattr(current_tab, start_method)()



    def _run_current_step(self):
        """Starts the MergeAll or ImageGen step for the chapter at current_chapter_index."""

        if not self.is_running_batch: return

        if self.current_chapter_index >= len(self.all_chapter_paths):
            # All chapters have been initiated (MergeAll & ImageGen started)
            return

        step_name, start_method_name = self.STEP_SEQUENCE[self.current_step_index]
        current_tab = self.tabs.get(step_name)

        # This is the path to the current chapter's raw input (e.g., C:/.../ddd/Chapter-N)
        chapter_path_input = self.all_chapter_paths[self.current_chapter_index]

        # Get the pipeline output root from the Merge tab (e.g., C:/.../fff)
        output_base_text = self.tabs.get('MergeAll').out_base_line.text().strip()
        root_dir_for_workers = output_base_text

        print(f"\n--- Chapter {self.current_chapter_index + 1}/{len(self.all_chapter_paths)}: Starting step **{step_name}** ---")
        self.main_window.tabs.setCurrentWidget(current_tab)

        if step_name == 'MergeAll':
            current_tab._set_ui_state(False)
            if hasattr(current_tab, start_method_name):
                 # MergeAll is the only one that uses the raw input path
                 current_tab.start_single_chapter_task(chapter_path_input)
            else:
                 QMessageBox.critical(self.main_window, "Critical Error", "MergeAllTab is missing the required 'start_single_chapter_task' method.")
                 self.is_running_batch = False
                 return

        elif step_name == 'ImageGen' and hasattr(current_tab, start_method_name):
            # ImageGen runs on the pipeline output root (which contains the new chapter folder)
            self._update_worker_input_path(step_name, current_tab, root_dir_for_workers)

            start_method = getattr(current_tab, start_method_name)
            start_method()
        else:
            print(f"ORCHESTRATOR: WARNING - Step {step_name} is not expected here.")
            # Should not happen for MergeAll/ImageGen
            self.is_running_batch = False

    @Slot()
    def _start_next_step(self):
        """Advances the step index and runs the next step for the current chapter (Merge->ImageGen)."""
        if not self.is_running_batch: return

        self.current_step_index += 1

        if self.current_step_index <= self.CONCURRENT_BREAK_STEP_INDEX:
            # Continue the Merge/ImageGen chain for the current chapter
            self._run_current_step()
        else:
            # The Merge/ImageGen chain for the current chapter is finished.
            # No action needed here, as _start_next_chapter_concurrent will handle the handoff.
            pass


    def _update_worker_input_path(self, step_name, current_tab, root_dir_for_workers):
        """Helper to inject the root path into worker tabs."""
        # ALL completion steps must use the pipeline output root as their "root folder"
        if step_name in ['ImageGen', 'Narration'] and hasattr(current_tab, 'root_folder_display'):
            current_tab.root_folder_display.setText(root_dir_for_workers)
        elif step_name == 'TTS' and hasattr(current_tab, 'batch_root_line'):
            current_tab.batch_root_line.setText(root_dir_for_workers)
        elif step_name == 'ManhwaBatch' and hasattr(current_tab, 'root_entry'):
             current_tab.root_entry.setText(root_dir_for_workers)
             current_tab.root_path = Path(root_dir_for_workers)


    @Slot()
    def _start_next_chapter_concurrent(self):
        """Advances the chapter index and starts the MergeAll step for the next chapter concurrently."""

        # Advance index to the next chapter to be merged
        next_chapter_index = self.current_chapter_index + 1

        # Check if we are done with the Merge/ImageGen initiation phase
        if next_chapter_index < len(self.all_chapter_paths):
            print(f"\nORCHESTRATOR: ImageGen(Ch-{self.current_chapter_index + 1}) finished. Starting **MergeAll(Ch-{next_chapter_index + 1})** concurrently.")

            # Set the orchestrator's state to the NEW chapter index and step index to MergeAll
            self.current_chapter_index = next_chapter_index
            self.current_step_index = 0

            # Call _run_current_step(), which will now execute MergeAll for the new chapter index
            self._run_current_step()

        else:
             print("\nORCHESTRATOR: All chapters initiated (Merge/ImageGen phase complete).")


    @Slot(int)
    def _start_completion_steps_for_chapter(self, chapter_index_to_start):
        """
        Starts the Narration step (first of the completion chain) for the specified chapter.
        """
        if not self.is_running_batch: return

        step_name = 'Narration'
        current_tab = self.tabs.get(step_name)

        # Get the pipeline output root (where the single chapter subfolder resides)
        output_base_text = self.tabs.get('MergeAll').out_base_line.text().strip()

        print(f"ORCHESTRATOR: ImageGen(Ch-{chapter_index_to_start + 1}) finished. Initiating **{step_name}** for Ch-{chapter_index_to_start + 1} (Sequential Completion Chain).")

        # 1. Inject the Pipeline Output Root Folder (`fff`)
        self._update_worker_input_path(step_name, current_tab, output_base_text)

        # 2. Start the single-chapter task.
        current_tab.start_task()

        # 3. Switch the main UI tab to NARRATION (to show the progress of the earliest completion step)
        self.main_window.tabs.setCurrentWidget(current_tab)

        # --- ADDED DELAY: Give the UI and threads time to stabilize after starting the next worker ---
        QApplication.processEvents()
        time.sleep(2)
        print(f"ORCHESTRATOR: Delayed 2s after launching {step_name}.")
        # ---------------------------------------------------------------------------------------------

        # Add chapter to set of actively running completion steps (for tracking)
        self.chapters_running_completion_steps.add(chapter_index_to_start)


    @Slot(str)
    def _handle_completion_chain_handoff(self, finished_tab_name):
        """
        NEW METHOD: Central handler for the sequential flow of Narration -> TTS -> Compose.
        """
        if not self.is_running_batch: return

        try:
            current_step_index = self.COMPLETION_CHAIN.index(finished_tab_name)
        except ValueError:
            print(f"ORCHESTRATOR ERROR: Unknown tab name {finished_tab_name} finished.")
            return

        # ManhwaBatch finish is handled by the direct signal connection to _on_chapter_fully_complete
        if finished_tab_name == 'ManhwaBatch':
            return

        # Determine the next step
        next_step_index = current_step_index + 1
        next_tab_name = self.COMPLETION_CHAIN[next_step_index]
        next_tab = self.tabs.get(next_tab_name)

        print(f"ORCHESTRATOR: {finished_tab_name} finished. Starting next step: {next_tab_name}")

        # Re-inject path
        output_base_text = self.tabs.get('MergeAll').out_base_line.text().strip()
        self._update_worker_input_path(next_tab_name, next_tab, output_base_text)

        next_tab.start_task()
        self.main_window.tabs.setCurrentWidget(next_tab)

        QApplication.processEvents()
        time.sleep(5)
        print(f"ORCHESTRATOR: Delayed 5s after launching {next_tab_name}.")

    def _show_final_completion(self):
        """Final message."""
        self.is_running_batch = False
        self.main_window._show_final_completion()
        print("\n=======================================================")
        print("ORCHESTRATOR: ALL CHAPTERS COMPLETE.")
        print("=======================================================")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Manhwa Pipeline Tool")
        self.setFixedSize(550, 690)

        self.tabs = QTabWidget()
        self.tab_widgets = {}
        self.tab_names = {}
        self._setup_ui()

        self.active_tabs = [
            self.tabs.widget(i) for i in range(self.tabs.count())
        ]
        self.orchestrator = PipelineOrchestrator(self)
        self._setup_auto_sequence()

        # --- CRITICAL FIX: Setup the automatic path injection ---
        self._setup_path_injection()
        self.tabs.currentChanged.connect(self._auto_start_current_tab)

    @Slot(str)
    def _update_all_output_paths(self, new_path: str):
        """Updates the input path in all subsequent tabs using the Merge output path."""

        # Correct Story / ImageGen Tab (Uses high_level_root)
        if hasattr(self.tab_widgets['ImageGen'], 'root_folder_display'):
            self.tab_widgets['ImageGen'].root_folder_display.setText(new_path)

        # Narration Tab (Uses root_folder)
        if hasattr(self.tab_widgets['Narration'], 'root_folder_display'):
            self.tab_widgets['Narration'].root_folder_display.setText(new_path)

        # TTS Tab (Uses root_folder)
        if hasattr(self.tab_widgets['TTS'], 'batch_root_line'):
            self.tab_widgets['TTS'].batch_root_line.setText(new_path)

        # Compose / ManhwaBatch Tab (Uses root_entry)
        if hasattr(self.tab_widgets['ManhwaBatch'], 'root_entry'):
            self.tab_widgets['ManhwaBatch'].root_entry.setText(new_path)
            # Must also update the internal Path object for the worker logic
            self.tab_widgets['ManhwaBatch'].root_path = Path(new_path)

    def _auto_start_current_tab(self, index: int):
        """Start the tab automatically when the Orchestrator switches to it."""
        widget = self.tabs.widget(index)
        if not widget:
            return
        # only fire if this tab is actually wired into the pipeline
        if widget in (
            self.tab_widgets.get('MergeAll'),
            self.tab_widgets.get('Narration'),
            self.tab_widgets.get('TTS'),
            self.tab_widgets.get('ManhwaBatch')
        ):
            if hasattr(widget, 'start_task') and not getattr(widget, '_already_auto_started', False):
                widget._already_auto_started = True   # run once per visit
                widget.start_task()




    def _setup_path_injection(self):
        """Connects the Merge output field to the update slot."""
        merge_tab = self.tab_widgets.get('MergeAll')

        # Check if the output line edit exists on the Merge tab
        if merge_tab and hasattr(merge_tab, 'out_base_line'):
            merge_tab.out_base_line.textChanged.connect(self._update_all_output_paths)
            print("✅ Path injection connected: Merge output will update all subsequent tab inputs.")


    def _setup_ui(self):
        layout = QVBoxLayout(self);
        layout.addWidget(self.tabs)
        # === One-click chain button: TTS -> Compose (same folder) ===
        row = QHBoxLayout()
        self.btn_chain_tts_compose = QPushButton("▶ TTS → Compose (same folder)")
        self.btn_chain_tts_compose.setMinimumHeight(36)
        self.btn_chain_tts_compose.setStyleSheet("font-weight:600;")
        self.btn_chain_tts_compose.clicked.connect(self._run_tts_then_compose)
        row.addStretch(1)
        row.addWidget(self.btn_chain_tts_compose)
        layout.addLayout(row)



        # 1. Manhwa Image Download (launcher)
        self.tab_widgets['Downloader'] = DownloaderTab()
        self.tabs.addTab(ImageDownloaderTab(), "📥 Image Download")



        # 2. Merge All Tab
        self.tab_widgets['MergeAll'] = MergeAllTab()
        self.tabs.addTab(self.tab_widgets['MergeAll'], "⚙️ Merge")

        # 3. Narration Tab
        self.tab_widgets['Narration'] = NarrationGeminiTab()
        self.tabs.addTab(self.tab_widgets['Narration'], "🎙️ Narration")

        self.tab_widgets['Polish'] = PolishTab()
        self.tabs.addTab(PolishTab(), "Polish")

        # 3. TTS Tab
        self.tab_widgets['TTS'] = TtsTab()
        self.tabs.addTab(self.tab_widgets['TTS'], "🗣️ TTS")

        # 4. Manhwa Batch Tab
        self.tab_widgets['ManhwaBatch'] = ManhwaBatchTab()
        self.tabs.addTab(self.tab_widgets['ManhwaBatch'], "🎞️ Video Compose")

        for key, widget in self.tab_widgets.items():
            self.tab_names[widget] = key

        self.tabs.setMinimumHeight(40)

    def _setup_auto_sequence(self):
        """Connects signals for orchestration and path management."""

        merge_tab = self.tab_widgets.get('MergeAll')
        if merge_tab and hasattr(merge_tab, 'btn_night_start'):
            pass

        self.tabs.currentChanged.connect(self._handle_tab_change)

    # The Orchestrator calls this directly when Compose finishes.
    def _show_final_completion(self):
        QMessageBox.information(
            self,
            "Pipeline Complete",
            "The full sequential chapter pipeline has finished successfully!"
        )
        print("PIPELINE COMPLETE: The full sequential chapter pipeline has finished successfully!")
        self.orchestrator.is_running_batch = False # Ensure orchestrator status is reset

    def _handle_tab_change(self, index):
        """
        Handles cleanup on tab change but explicitly prevents auto-start
        when switching to the MergeAll tab. The user must click START.
        """
        pass
    def _run_tts_then_compose(self):
        """Run TTS first; when it finishes, auto-start Video Compose (same folder)."""
        tts_tab = self.tab_widgets.get('TTS')
        comp_tab = self.tab_widgets.get('ManhwaBatch')

        if not tts_tab or not comp_tab:
            QMessageBox.critical(self, "Missing tabs",
                                 "TTS or Video Compose tab not found.")
            return

        # 1) Read SAME folder from TTS tab (its Output/Root field)
        try:
            # your TTS tab exposes batch_root_line (already wired in file)
            root = tts_tab.out_root_edit.text().strip()

        except Exception:
            root = ""

        if not root:
            QMessageBox.warning(self, "Set Folder",
                                "Please set the Root/Chapters folder in the TTS tab first.")
            self.tabs.setCurrentWidget(tts_tab)
            return

        # 2) Put that SAME folder into Video Compose tab fields
        comp_tab.root_entry.setText(root)   # its input box
        try:
            from pathlib import Path
            comp_tab.root_path = Path(root) # its Path object used by start_task()
        except Exception:
            pass

        # 3) When TTS says “finished”, start Compose once
        def _start_compose_once():
            try:
                tts_tab.operation_finished.disconnect(_start_compose_once)
            except Exception:
                pass
            self.tabs.setCurrentWidget(comp_tab)
            comp_tab.start_task()  # starts batch compose (see start_task)

        # avoid double connect on repeated clicks
        try:
            tts_tab.operation_finished.disconnect(_start_compose_once)
        except Exception:
            pass
        tts_tab.operation_finished.connect(_start_compose_once)

        # 4) Switch to TTS and start it now
        self.tabs.setCurrentWidget(tts_tab)
        if hasattr(tts_tab, 'start_task'):
            tts_tab.start_task()
        else:
            QMessageBox.critical(self, "Cannot start TTS",
                                 "TTS tab has no start_task().")

    def closeEvent(self, event):
        """Called when the window is closed. Ensures all worker threads are gracefully stopped."""

        if hasattr(self, 'orchestrator') and self.orchestrator.is_running_batch:
            print("Stopping orchestrator batch...")
            self.orchestrator.is_running_batch = False

        for tab in self.active_tabs:
            if hasattr(tab, 'cleanup'):
                tab.cleanup()

        app = QApplication.instance()
        if app:
            for _ in range(5):
                app.processEvents()
                time.sleep(0.01)

        super().closeEvent(event)

def main():
    if not QApplication.instance():
        app = QApplication(sys.argv)
    else:
        app = QApplication.instance()

    app.setStyleSheet(STYLE_SHEET)

    if os.path.exists("logo.png"):
        app.setWindowIcon(QIcon("logo.png"))
    else:
        app.setWindowIcon(QIcon.fromTheme("applications-other"))

    w = MainWindow()
    w.show()

    ret = app.exec()

    app.quit()
    del app

    sys.exit(ret)

if __name__ == "__main__":
    main()
