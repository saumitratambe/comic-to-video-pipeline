# tts_logic.py — Multi-Chapter TTS with skip-if-exists and 96 kbps mono
# Structure:
# <OutputRoot>/
#   Chapter A/
#     Narration/*.txt   (input)
#     Voice/            (output, auto-created)
#   Chapter B/
#     Narration/*.txt
#     Voice/
#
# Providers:
#   • Murf: multi-key (multiline) + fixed voice dropdown (Ryan default)
#            -> Insert your HTTP call where marked; result re-encoded to 96 kbps mono.
#   • Edge: edge-tts -> temp save, then re-encoded to 96 kbps mono.
#   • Piper: generates WAV -> converted to 96 kbps mono MP3.
#
# Sorting: natsort (Crop1, Crop2, …, Crop10)
# Extras: Skip if target MP3 already exists.

import os, sys, json, asyncio, subprocess, tempfile, uuid, time
from pathlib import Path
from typing import List, Optional, Tuple
from PySide6 import QtWidgets, QtCore
from natsort import natsorted

# ---------- Config ----------
DEFAULT_AUDIO_KBPS = 96  # unified default bitrate (mono) for all providers

# ---------- Murf Voice Options ----------
MURF_VOICE_OPTIONS = {
    "Natalie (Female, US)": "en-US-natalie",
    "Ryan (Male, US)": "en-US-ryan",   # DEFAULT
    "Stavros (Male, GR/EL)": "el-GR-stavros",
    "Theo (Male, UK)": "en-UK-theo",
    "Paul (Male, US)": "en-US-paul",
}

# ---------- Optional imports ----------
EDGE_AVAILABLE = True
try:
    import edge_tts
except Exception:
    EDGE_AVAILABLE = False

# ---------- Helpers ----------
def list_text_files(folder: Path) -> List[Path]:
    files = [p for p in folder.glob("*.txt") if p.is_file()]
    return natsorted(files, key=lambda p: p.stem.lower())

def ffmpeg_to_mp3(src_audio: Path, dst_mp3: Path, kbps: int = DEFAULT_AUDIO_KBPS, mono: bool = True):
    """
    Re-encode any input to MP3 at kbps (default 96) and mono.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_audio),
        "-ac", "1" if mono else "2",
        "-b:a", f"{kbps}k",
        str(dst_mp3),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def find_piper_root() -> Path:
    return Path(__file__).resolve().parent / "piper"

def find_piper_exe() -> Optional[Path]:
    root = find_piper_root()
    if not root.exists():
        return None
    for dirpath, _, files in os.walk(root):
        for cand in ("piper.exe", "piper"):
            p = Path(dirpath) / cand
            if p.is_file():
                return p
    return None

def iter_piper_voices():
    root = find_piper_root()
    if not root.exists():
        return
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(".onnx"):
                continue
            onnx = Path(dirpath) / f
            base = onnx.with_suffix("").name
            meta_path = onnx.with_suffix(".json")
            entries = []
            meta = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            if isinstance(meta.get("speaker_id_map"), dict):
                for spk_name, spk_id in meta["speaker_id_map"].items():
                    entries.append({"onnx": onnx, "label": f"{base} · {spk_name} (id {spk_id})", "speaker_id": int(spk_id)})
            elif isinstance(meta.get("speakers"), list) and meta["speakers"]:
                for idx, spk_name in enumerate(meta["speakers"]):
                    entries.append({"onnx": onnx, "label": f"{base} · {spk_name} (id {idx})", "speaker_id": int(idx)})
            else:
                entries.append({"onnx": onnx, "label": f"{base} (id 0)", "speaker_id": 0})
            for e in entries:
                yield e

def find_chapters(out_root: Path) -> List[Path]:
    """Return natsorted list of subfolders (chapters) inside out_root."""
    subs = [p for p in out_root.iterdir() if p.is_dir()]
    return natsorted(subs, key=lambda p: p.name.lower())

def gather_all_txts(chapters: List[Path]) -> Tuple[int, List[Tuple[Path, List[Path], Path]]]:
    """
    Returns (total_txt_count, plan) where plan is a list of:
      (chapter_dir, narration_txts[], voice_dir)
    Only includes chapters that contain Narration and at least one .txt file.
    """
    total = 0
    plan = []
    for ch in chapters:
        narration = ch / "Narrations"
        if narration.exists():
            txts = list_text_files(narration)
            voice = ch / "Voice"
            if txts:
                total += len(txts)
                plan.append((ch, txts, voice))
    return total, plan

# ---------- Worker ----------
class TTSWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int)
    finished = QtCore.Signal()

    def __init__(
        self,
        out_root: Path,
        provider: str,
        murf_keys: List[str],
        murf_voice: str,
        edge_voice: str,
        piper_sel: dict,
        parent=None
    ):
        super().__init__(parent)
        self.out_root = out_root
        self.provider = provider
        # Murf multi-key
        self.murf_keys: List[str] = [k.strip() for k in (murf_keys or []) if k.strip()]
        self._murf_key_idx: int = 0
        self.murf_voice = murf_voice or ""
        # Edge
        self.edge_voice = edge_voice or ""
        # Piper
        self.piper_sel = piper_sel or {}

        self._stop = False
        self._current_proc = None

    # ---------- Control ----------
    def stop(self):
        self._stop = True
        try:
            if self._current_proc and self._current_proc.poll() is None:
                self._current_proc.terminate()
        except Exception:
            pass

    # ---------- Murf helpers ----------
    def _next_murf_key(self) -> str:
        if not self.murf_keys:
            return ""
        key = self.murf_keys[self._murf_key_idx]
        self._murf_key_idx = (self._murf_key_idx + 1) % len(self.murf_keys)
        return key

    def _murf_retry_delays(self):
        return [0.5, 1.0, 2.0]

    # ---------- Providers ----------
    def _synth_with_murf(self, text: str, out_mp3: Path):
        """
        Insert your real Murf HTTP call here, using self._next_murf_key() to rotate keys.
        We ALWAYS re-encode the result to 96 kbps mono for consistency.
        """
        if not self.murf_keys:
            raise RuntimeError("Murf: At least one API key is required.")
        if not self.murf_voice.strip():
            raise RuntimeError("Murf: Voice ID is required.")

        # ==== YOUR MURF HTTP CALL HERE ====
        # import requests
        # url = "https://api.murf.ai/v1/speech"  # Example only
        # payload = {"voiceId": self.murf_voice, "text": text, "format": "mp3"}
        # last_err = None
        # for i, delay in enumerate([0.0] + self._murf_retry_delays()):
        #     if self._stop:
        #         return
        #     if delay: time.sleep(delay)
        #     key = self._next_murf_key()
        #     headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        #     try:
        #         r = requests.post(url, headers=headers, json=payload, timeout=60)
        #         if r.status_code == 200:
        #             tmp_in = Path(tempfile.gettempdir()) / f"murf_{uuid.uuid4().hex}.mp3"
        #             tmp_in.write_bytes(r.content)
        #             ffmpeg_to_mp3(tmp_in, out_mp3)  # => 96 kbps mono
        #             try: tmp_in.unlink()
        #             except: pass
        #             return
        #         if r.status_code in (429, 500, 502, 503, 504):
        #             last_err = RuntimeError(f"Murf status {r.status_code}")
        #             continue
        #         r.raise_for_status()
        #     except Exception as ex:
        #         last_err = ex
        #         continue
        # raise last_err or RuntimeError("Murf: synthesis failed.")
        # ===================================

        # If you didn't paste the real call yet:
        raise NotImplementedError("Insert Murf HTTP call in _synth_with_murf() (multi-key rotation + 96 kbps re-encode).")

    async def _synth_with_edge_async(self, text: str, out_mp3: Path):
        if not EDGE_AVAILABLE:
            raise RuntimeError("edge-tts not installed. pip install edge-tts")
        if not self.edge_voice:
            raise RuntimeError("Edge voice is empty (e.g., en-US-GuyNeural).")

        # Save whatever edge-tts gives, then normalize to 96 kbps mono MP3
        tmp_out = Path(tempfile.gettempdir()) / f"edge_{uuid.uuid4().hex}.mp3"
        comm = edge_tts.Communicate(text, self.edge_voice)
        await comm.save(str(tmp_out))
        ffmpeg_to_mp3(tmp_out, out_mp3)  # => 96 kbps mono
        try:
            tmp_out.unlink()
        except Exception:
            pass

    def _synth_with_piper(self, text: str, out_mp3: Path):
        exe = self.piper_sel.get("exe")
        onnx = self.piper_sel.get("onnx")
        speaker_id = int(self.piper_sel.get("speaker_id", 0))
        if not exe or not Path(exe).is_file():
            raise RuntimeError("Piper: executable not found in ./piper")
        if not onnx or not Path(onnx).is_file():
            raise RuntimeError("Piper: voice model (.onnx) not found in ./piper")

        # Generate WAV with Piper, then convert to MP3 96 kbps mono
        tmp_wav = Path(tempfile.gettempdir()) / f"piper_{uuid.uuid4().hex}.wav"
        cmd = [str(exe), "-m", str(onnx), "--speaker", str(speaker_id), "-f", str(tmp_wav)]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._current_proc = proc
            if proc.stdin:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            while True:
                if self._stop and proc and proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except Exception: pass
                    return
                code = proc.poll()
                if code is not None:
                    if code != 0:
                        err = (proc.stderr.read() if proc.stderr else b"")[:4096].decode("utf-8","ignore")
                        raise RuntimeError(f"Piper failed (exit {code}). {err}")
                    break
                time.sleep(0.02)
            if self._stop:
                return
            ffmpeg_to_mp3(tmp_wav, out_mp3)  # => 96 kbps mono
        finally:
            self._current_proc = None
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            try:
                if tmp_wav.exists():
                    tmp_wav.unlink()
            except Exception:
                pass

    # ---------- Thread entry ----------
    def run(self):
        try:
            if not self.out_root.exists():
                self.log.emit(f"Output root not found: {self.out_root}")
                self.progress.emit(100)
                self.finished.emit()
                return

            chapters = find_chapters(self.out_root)
            total_txt, plan = gather_all_txts(chapters)

            if total_txt == 0:
                self.log.emit("No chapters with Narration found under the selected Output Root.")
                self.progress.emit(100)
                self.finished.emit()
                return

            done = 0
            for ch_dir, txts, voice_dir in plan:
                if self._stop:
                    break
                chapter_name = ch_dir.name
                voice_dir.mkdir(parents=True, exist_ok=True)
                self.log.emit(f"=== Chapter: {chapter_name} — {len(txts)} files ===")

                for txt in txts:
                    if self._stop:
                        break
                    out_mp3 = voice_dir / f"{txt.stem}.mp3"

                    # --- SKIP IF ALREADY EXISTS ---
                    if out_mp3.exists():
                        self.log.emit(f"[SKIP] Already exists: {chapter_name}/Voice/{out_mp3.name}")
                        done += 1
                        self.progress.emit(int(done / total_txt * 100))
                        continue
                    # ------------------------------

                    text = txt.read_text(encoding="utf-8", errors="ignore").strip()
                    if not text:
                        self.log.emit(f"[SKIP] Empty: {chapter_name}/Narrations/{txt.name}")
                        done += 1
                        self.progress.emit(int(done / total_txt * 100))
                        continue

                    self.log.emit(f"Synthesizing → {chapter_name}/Voice/{out_mp3.name} ({self.provider})")

                    if self.provider == "murf":
                        self._synth_with_murf(text, out_mp3)
                    elif self.provider == "edge":
                        asyncio.run(self._synth_with_edge_async(text, out_mp3))
                    elif self.provider == "piper":
                        self._synth_with_piper(text, out_mp3)
                    else:
                        raise RuntimeError(f"Unknown provider: {self.provider}")

                    done += 1
                    self.progress.emit(int(done / total_txt * 100))

                self.log.emit(f"=== Finished: {chapter_name} ===")

            if not self._stop:
                self.log.emit("All chapters done.")
        except Exception as e:
            self.log.emit(f"ERROR: {e}")
        finally:
            self.finished.emit()

# ---------- TTS TAB (exported) ----------
class TtsTab(QtWidgets.QWidget):
    operation_finished = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TtsTab")

        # Output Root Folder ONLY
        self.out_root_edit = QtWidgets.QLineEdit()
        btn_browse_out = QtWidgets.QPushButton("Browse…")
        btn_browse_out.clicked.connect(self._choose_out_root)

        # Provider dropdown
        self.provider_combo = QtWidgets.QComboBox()
        self.provider_combo.addItems(["Murf", "Edge", "Piper"])
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        # Murf (multi-key + voice dropdown)
        self.murf_keys_edit = QtWidgets.QTextEdit()
        self.murf_keys_edit.setPlaceholderText("Enter multiple keys:\nkey1\nkey2\nkey3\n...\n(Comma-separated also works)")
        self.murf_keys_edit.setFixedHeight(140)

        self.murf_voice_combo = QtWidgets.QComboBox()
        for label, value in MURF_VOICE_OPTIONS.items():
            self.murf_voice_combo.addItem(label, value)
        self.murf_voice_combo.setCurrentText("Ryan (Male, US)")

        # Edge
        self.edge_voice_edit = QtWidgets.QLineEdit()
        self.edge_voice_edit.setPlaceholderText("e.g., en-US-GuyNeural")

        # Piper (auto)
        self.piper_voice_combo = QtWidgets.QComboBox()

        # Controls
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_stop  = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        # Log + progress
        self.log = QtWidgets.QTextEdit(); self.log.setReadOnly(True)
        self.prog = QtWidgets.QProgressBar()

        # Layout
        form = QtWidgets.QFormLayout()

        row_out = QtWidgets.QHBoxLayout()
        row_out.addWidget(self.out_root_edit, 1)
        row_out.addWidget(btn_browse_out)
        w_out = QtWidgets.QWidget(); w_out.setLayout(row_out)

        form.addRow("Output Root Folder", w_out)
        form.addRow("Provider", self.provider_combo)
        form.addRow("Murf API Keys", self.murf_keys_edit)
        form.addRow("Murf Voice", self.murf_voice_combo)
        form.addRow("Edge Voice", self.edge_voice_edit)
        form.addRow("Piper Voice", self.piper_voice_combo)

        row_btn = QtWidgets.QHBoxLayout()
        row_btn.addWidget(self.btn_start)
        row_btn.addWidget(self.btn_stop)
        w_btn = QtWidgets.QWidget(); w_btn.setLayout(row_btn)

        v = QtWidgets.QVBoxLayout(self)
        v.addLayout(form)
        v.addWidget(w_btn)
        v.addWidget(self.prog)
        v.addWidget(self.log, 1)

        # Signals
        self.btn_start.clicked.connect(self.start_task)
        self.btn_stop.clicked.connect(self._stop)

        # State
        self._worker: Optional[TTSWorker] = None
        self._on_provider_changed()

    # Public hook (also used when running solo)
    def start_task(self):
        if not self.btn_start.isEnabled():
            return
        self._start()

    # Internals
    def _choose_out_root(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select OUTPUT ROOT folder (contains many chapter folders)")
        if d:
            self.out_root_edit.setText(d)

    def _on_provider_changed(self):
        name = self.provider_combo.currentText().strip().lower()
        is_murf  = (name == "murf")
        is_edge  = (name == "edge")
        is_piper = (name == "piper")

        self.murf_keys_edit.setVisible(is_murf)
        self.murf_voice_combo.setVisible(is_murf)
        self.edge_voice_edit.setVisible(is_edge)
        self.piper_voice_combo.setVisible(is_piper)
        if is_piper:
            self._load_piper_voices()

    def _load_piper_voices(self):
        self.piper_voice_combo.clear()
        exe = find_piper_exe()
        voices = list(iter_piper_voices())
        if not exe or not voices:
            self.piper_voice_combo.addItem("No voices found in ./piper", {"exe": None, "onnx": None, "speaker_id": 0})
            return
        for v in natsorted(list(voices), key=lambda x: x["label"].lower()):
            self.piper_voice_combo.addItem(
                v["label"],
                {"exe": str(exe), "onnx": str(v["onnx"]), "speaker_id": int(v["speaker_id"])}
            )

    def _append_log(self, s: str):
        self.log.append(s)
        self.log.ensureCursorVisible()

    def _start(self):
        out_root = Path(self.out_root_edit.text().strip() or "").resolve()
        if not out_root.exists():
            QtWidgets.QMessageBox.warning(self, "Output Root", "Please select a valid OUTPUT ROOT folder.")
            return

        provider = self.provider_combo.currentText().strip().lower()

        murf_keys: List[str] = []
        murf_voice = ""
        edge_voice = ""
        piper_sel  = {}

        if provider == "murf":
            raw = self.murf_keys_edit.toPlainText().strip()
            murf_keys = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
            murf_voice = self.murf_voice_combo.currentData()
            if not murf_keys:
                QtWidgets.QMessageBox.warning(self, "Murf", "Enter at least one API key (one per line or comma-separated).")
                return
            if not murf_voice:
                QtWidgets.QMessageBox.warning(self, "Murf", "Please select a Murf voice.")
                return
        elif provider == "edge":
            if not EDGE_AVAILABLE:
                QtWidgets.QMessageBox.warning(self, "Edge", "edge-tts is not installed (pip install edge-tts).")
                return
            edge_voice = self.edge_voice_edit.text().strip()
            if not edge_voice:
                QtWidgets.QMessageBox.warning(self, "Edge", "Please enter an Edge voice (e.g., en-US-GuyNeural).")
                return
        elif provider == "piper":
            piper_sel = self.piper_voice_combo.currentData() or {}
            if not piper_sel.get("exe") or not piper_sel.get("onnx"):
                QtWidgets.QMessageBox.warning(self, "Piper", "No valid Piper voice found in the ./piper folder.")
                return

        self._worker = TTSWorker(out_root, provider, murf_keys, murf_voice, edge_voice, piper_sel)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self.prog.setValue)
        self._worker.finished.connect(self._on_finished)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.prog.setValue(0)
        self.log.clear()
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._worker = None
        self.operation_finished.emit()

# ---------- Standalone run ----------
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    w = TtsTab()
    w.setWindowTitle("TTS — Multi-Chapter (96 kbps mono, skip if exists)")
    w.resize(860, 620)
    w.show()
    sys.exit(app.exec())
