# polish_logic.py
# ============================================================
# POLISH NARRATION — Chapter-level narration fixer & aligner
#
# INPUT:
#   ChapterXX/
#     ├─ Crop/           (Crop1.png ... CropN.png)
#     └─ Narrations/     (Crop1.txt ... CropN.txt)  ← input & output
#
# OUTPUT:
#   Narrations/*.txt  ← overwritten ONLY on full success
#
# BEHAVIOR:
#   • 1 Gemini call per chapter
#   • Retry max 3 times per chapter
#   • Skip chapter on 400/401/403/404
#   • Retry other errors
#   • No partial overwrite
# ============================================================

import os
import time
import base64
import re
import requests
from collections import deque

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QLineEdit,
    QProgressBar, QMessageBox, QFileDialog, QGroupBox
)
from PySide6.QtCore import QThread, Signal, Slot

# ================= CONFIG =================
MODEL_NAME = "gemini-flash-lite-latest"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key="
MAX_CHAPTER_ATTEMPTS = 3
SKIP_HTTP_CODES = {400, 401, 403, 404}

# ================= UTILITIES =================
def natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

def encode_image(path):
    ext = path.split(".")[-1].lower()
    mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"inlineData": {"mimeType": mime, "data": data}}

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip())

# ================= KEY MANAGER =================
class KeyManager:
    def __init__(self, keys):
        self.keys = deque(keys)

    def current(self):
        return self.keys[0]

    def rotate(self):
        if len(self.keys) > 1:
            self.keys.rotate(-1)

    def drop_current(self):
        if self.keys:
            self.keys.popleft()

    def has_keys(self):
        return bool(self.keys)

# ================= GEMINI CALL =================
def call_gemini(key, parts, system_prompt):
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    r = requests.post(API_URL + key, json=payload, timeout=120)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "error": r.text}
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return {"ok": True, "text": text.strip()}
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)}

# ================= PARSER =================
def parse_output(text, expected):
    blocks = {}
    current = None
    buffer = []

    for line in text.splitlines():
        m = re.match(r"^\s*Crop(\d+)\s*:\s*(.*)$", line, re.I)
        if m:
            if current:
                blocks[current] = " ".join(buffer).strip()
            current = int(m.group(1))
            buffer = [m.group(2)] if m.group(2) else []
        else:
            if current:
                buffer.append(line)

    if current:
        blocks[current] = " ".join(buffer).strip()

    # Allow slight mismatch but require at least expected crops
    if len(blocks) < expected:
        return None

    # Force exact ordering, ignore extras
    return [blocks.get(i, "").strip() for i in range(1, expected + 1)]


# ================= WORKER =================
class PolishWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal()

    def __init__(self, root, keys):
        super().__init__()
        self.root = root
        self.keys = KeyManager(keys)
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        chapters = sorted(
            [d for d in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, d))],
            key=natural_sort_key
        )

        total = len(chapters)
        done = 0

        for ch in chapters:
            if not self._running:
                break

            self.log.emit(f"[{ch}] ▶ Starting polish")
            ok = self._process_chapter(ch)

            if ok:
                self.log.emit(f"[{ch}] ✅ Polished successfully")
            else:
                self.log.emit(f"[{ch}] ⏭ Skipped (original preserved)")

            done += 1
            self.progress.emit(int(done / total * 100))
            time.sleep(8)  # global pacing between chapters


        self.finished.emit()

    def _process_chapter(self, ch):
        ch_path = os.path.join(self.root, ch)
        crop_dir = os.path.join(ch_path, "Crop")
        narr_dir = os.path.join(ch_path, "Narrations")

        if not os.path.isdir(crop_dir) or not os.path.isdir(narr_dir):
            self.log.emit(f"[{ch}] ❌ Missing Crop or Narrations folder")
            return False

        crops = sorted(
            [f for f in os.listdir(crop_dir) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))],
            key=natural_sort_key
        )
        txts = sorted(
            [f for f in os.listdir(narr_dir) if f.lower().endswith(".txt")],
            key=natural_sort_key
        )

        if len(crops) != len(txts) or not crops:
            self.log.emit(f"[{ch}] ❌ Count mismatch or empty")
            return False

        narrations = [read_text(os.path.join(narr_dir, t)) for t in txts]

        parts = [
            {
                "text": (
                    "You are a professional narration editor AND visual aligner.\n\n"

                    "VERY IMPORTANT:\n"
                    "This task is NOT simple rephrasing.\n"
                    "You MUST use the IMAGES to decide narration placement.\n\n"

                    "PROCESS YOU MUST FOLLOW INTERNALLY:\n"
                    "1. Look at Crop1.png and understand what is happening visually.\n"
                    "2. Decide which part of the story fits Crop1.\n"
                    "3. Rewrite ONLY that part for Crop1.\n"
                    "4. Then repeat for Crop2, Crop3, and so on.\n\n"

                    "ALIGNMENT RULES (STRICT):\n"
                    "- If a sentence currently belongs to the wrong crop, MOVE it.\n"
                    "- If narration describes an event not visible yet, push it to a later crop.\n"
                    "- If a crop has little visual information, continue the story naturally.\n"
                    "- NEVER label a crop as empty or transitional.\n\n"

                    "STORY RULES:\n"
                    "- All crops together form ONE continuous story.\n"
                    "- Do NOT restart narration at each crop.\n"
                    "- Do NOT invent new events.\n"
                    "- Do NOT remove important story points.\n\n"

                    "LANGUAGE RULES:\n"
                    "- Simple daily English.\n"
                    "- Third person only.\n"
                    "- 1–3 sentences per crop.\n"
                    "- No ALL CAPS.\n"
                    "- No symbols like *, &, #, @.\n"
                    "- No meta commentary.\n\n"

                    "OUTPUT FORMAT (STRICT):\n"
                    "Crop1:\n<aligned story>\n\n"
                    "Crop2:\n<aligned story>\n\n"
                    "...\n"
                )
            },
            {"text": "\nRAW CHAPTER STORY (may be misaligned):\n"},
            {
                "text": "\n".join(
                    f"Crop{i+1}:\n{narrations[i]}\n"
                    for i in range(len(narrations))
                )
            },
        ]



        for idx, c in enumerate(crops, start=1):
            parts.append({"text": f"This is the visual content of Crop{idx}."})
            parts.append(encode_image(os.path.join(crop_dir, c)))



        attempts = 0

        while attempts < MAX_CHAPTER_ATTEMPTS and self.keys.has_keys():
            res = call_gemini(self.keys.current(), parts, "You are a professional narration editor.")
            self.log.emit(f"[{ch}] DEBUG raw response keys: {list(res.keys())}")
            self.log.emit(f"[{ch}] DEBUG status: {res.get('status')}")
            self.log.emit(f"[{ch}] DEBUG has_text: {'text' in res}")
            self.log.emit(f"[{ch}] DEBUG text preview:\n{res.get('text', '')[:800]}")

            if not res.get("ok"):
                status = res.get("status")

                if status in SKIP_HTTP_CODES:
                    self.log.emit(f"[{ch}] ❌ HTTP {status} → skipping chapter")
                    return False

                if status == 429:
                    self.log.emit(f"[{ch}] ⏳ Rate limit hit. Cooling down 20 seconds...")
                    time.sleep(20)
                    self.keys.rotate()

                elif status in (401, 403):
                    self.keys.drop_current()

                attempts += 1
                self.log.emit(f"[{ch}] 🔁 Retry {attempts}/{MAX_CHAPTER_ATTEMPTS}")
                continue


            # ---------- SUCCESS RESPONSE ----------
            self.log.emit(f"[{ch}] 🔍 Gemini output preview:\n{res.get('text', '')[:800]}")

            parsed = parse_output(res["text"], len(crops))

            if not parsed:
                attempts += 1
                self.log.emit(f"[{ch}] ⚠ Parse failed (attempt {attempts}/{MAX_CHAPTER_ATTEMPTS})")
                continue


            # ---------- FULL SUCCESS ----------
            for i, t in enumerate(txts):
                write_text(os.path.join(narr_dir, t), parsed[i])

            return True



# ================= UI TAB =================
class PolishTab(QWidget):
    operation_finished = Signal()

    def __init__(self):
        super().__init__()
        self.worker = None
        self.root = ""
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        cfg = QGroupBox("Configuration")
        c = QVBoxLayout(cfg)

        self.key_box = QTextEdit()
        self.key_box.setPlaceholderText("Gemini API keys (one per line or comma)")
        c.addWidget(self.key_box)

        f = QHBoxLayout()
        self.root_line = QLineEdit()
        self.root_line.setReadOnly(True)
        f.addWidget(self.root_line)
        btn = QPushButton("Browse")
        btn.clicked.connect(self._browse)
        f.addWidget(btn)
        c.addLayout(f)

        layout.addWidget(cfg)

        btns = QHBoxLayout()
        self.start = QPushButton("START POLISH")
        self.stop = QPushButton("STOP")
        self.start.clicked.connect(self._start)
        self.stop.clicked.connect(self._stop)
        btns.addWidget(self.start)
        btns.addWidget(self.stop)
        layout.addLayout(btns)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Root Folder")
        if d:
            self.root = d
            self.root_line.setText(d)

    def _collect_keys(self):
        raw = self.key_box.toPlainText()
        return [k.strip() for k in re.split(r"[,\n]", raw) if k.strip()]

    def _start(self):
        keys = self._collect_keys()
        if not keys or not self.root:
            QMessageBox.warning(self, "Missing input", "Provide keys and root folder")
            return

        self.log.clear()
        self.worker = PolishWorker(self.root, keys)
        self.worker.log.connect(self.log.append)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self._done)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()

    def _done(self):
        self.log.append("=== POLISH COMPLETE ===")
        self.operation_finished.emit()
