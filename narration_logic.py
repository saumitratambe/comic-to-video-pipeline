import os
import time
import requests
import base64
import re
from collections import deque

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QLineEdit, QGroupBox,
    QProgressBar, QMessageBox, QFileDialog, QComboBox
)
from PySide6.QtCore import Signal, Slot, QThread

# ===================== CONFIG =====================
MODEL_NAME = "gemini-flash-lite-latest"
API_URL_TEMPLATE = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key="

MAX_CROP_RETRIES = 3                # per-line retries before marking the line as failed
MAX_KEY_FAILURES_BEFORE_DROP = 2    # drop a hard-failing key after N 401/403 events
CONSEC_FAILS_SWITCH_THRESHOLD = 10  # switch key after 10 consecutive failed lines (resets on any success)

# ===================== UTILS ======================
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split('([0-9]+)', s)]

def encode_image_to_base64(image_path):
    if not os.path.exists(image_path):
        return None, None, f"Error: File not found at path: {image_path}"
    try:
        ext = image_path.split('.')[-1].lower()
        mime_type = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'
        with open(image_path, "rb") as image_file:
            base64_data = base64.b64encode(image_file.read()).decode('utf-8')
        return base64_data, mime_type, None
    except Exception as e:
        return None, None, f"Error encoding image: {e}"

def extract_narration_text(raw_text):
    if raw_text is None:
        return "Error: Empty response received."
    cleaned_text = raw_text.strip()
    if cleaned_text.startswith('{') and cleaned_text.endswith('}'):
        try:
            match = re.search(r'"narration"\s*:\s*"(.*?)"', cleaned_text, re.DOTALL)
            if match:
                narration = match.group(1).strip()
                return narration.replace(r'\"', '"')
            return raw_text
        except Exception:
            return raw_text
    return raw_text

def call_gemini_api(api_key, base64_image, mime_type, context, user_prompt, system_prompt_text):
    """Return dict: { ok, text?, status?, error? }"""
    api_url = API_URL_TEMPLATE + api_key
    time.sleep(1.0)  # gentle pacing

    contents = [{
        "role": "user",
        "parts": [
            {"text": f"Overall Story Context/Memory: {context}"},
            {"inlineData": {"mimeType": mime_type, "data": base64_image}},
            {"text": user_prompt}
        ]
    }]
    payload = {"contents": contents, "systemInstruction": {"parts": [{"text": system_prompt_text}]}}

    for attempt in range(3):
        try:
            resp = requests.post(api_url, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', None)
                if text:
                    return {"ok": True, "text": text.strip(), "status": 200}
                return {"ok": False, "error": "Could not extract narration text.", "status": 200}

            status = resp.status_code

            # explicit handling
            if status == 429:
                # Quota exceeded -> caller should switch key immediately
                return {"ok": False, "error": "Quota exceeded (429).", "status": 429}

            if 500 <= status < 600:
                # transient -> backoff and retry
                time.sleep(2 ** attempt)
                continue

            # 4xx (including 401/403) and others -> return to caller
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            return {"ok": False, "error": f"HTTP {status}: {detail}", "status": status}

        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": f"Network error after retries: {e}", "status": None}

    return {"ok": False, "error": "Unknown error after retries.", "status": None}

def call_gemini_api_multi(api_key, encoded_images, context, user_prompt, system_prompt_text):
    """
    Call Gemini once with ALL crops of a chapter.

    encoded_images: list of dicts:
        {"base64": <str>, "mime_type": <str>, "name": <str>}
    context: merged OCR text for the full chapter
    user_prompt: big meta prompt (manhwa name, chapter name, rules, etc.)
    """
    api_url = API_URL_TEMPLATE + api_key
    time.sleep(1.0)  # gentle pacing

    # Build the text part: instructions + OCR context
    meta_text = (
        f"{user_prompt}\n\n"
        "OCR OF THIS CHAPTER (for your understanding, do NOT copy it word by word):\n"
        f"\"\"\"{context}\"\"\""
    )

    parts = [{"text": meta_text}]

    # Attach all crops in nat-sorted order
    for idx, img in enumerate(encoded_images, start=1):
        parts.append({"text": f"[CROP {idx} IMAGE]"})
        parts.append({
            "inlineData": {
                "mimeType": img["mime_type"],
                "data": img["base64"],
            }
        })

    contents = [{
        "role": "user",
        "parts": parts,
    }]

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt_text}]}
    }

    for attempt in range(3):
        try:
            resp = requests.post(api_url, json=payload, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                text = (
                    data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", None)
                )
                if text:
                    return {"ok": True, "text": text.strip(), "status": 200}
                return {"ok": False, "error": "Could not extract narration text.", "status": 200}

            status = resp.status_code

            if status == 429:
                return {"ok": False, "error": "Quota exceeded (429).", "status": 429}

            if 500 <= status < 600:
                time.sleep(2 ** attempt)
                continue

            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            return {"ok": False, "error": f"HTTP {status}: {detail}", "status": status}

        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": f"Network error after retries: {e}", "status": None}

    return {"ok": False, "error": "Unknown error after retries.", "status": None}


def parse_multi_crop_output(raw_text, expected_count):
    """
    Parse Gemini reply in format:

    Crop1:
    text...
    Crop2:
    text...

    Returns: list of narrations [for Crop1, Crop2, ...].
    Ensures length == expected_count (fills missing with empty strings).
    """
    if not raw_text:
        return [""] * expected_count

    lines = raw_text.replace("\r", "").split("\n")
    crop_map = {}
    current_idx = None
    buffer = []

    crop_header_re = re.compile(r"^\s*Crop\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)

    for line in lines:
        m = crop_header_re.match(line)
        if m:
            # save previous
            if current_idx is not None:
                text = " ".join(part.strip() for part in buffer if part.strip())
                crop_map[current_idx] = text.strip()
            # start new
            current_idx = int(m.group(1))
            rest = m.group(2).strip()
            buffer = [rest] if rest else []
        else:
            if current_idx is not None:
                buffer.append(line)

    # flush last
    if current_idx is not None:
        text = " ".join(part.strip() for part in buffer if part.strip())
        crop_map[current_idx] = text.strip()

    narrations = []
    for i in range(1, expected_count + 1):
        narr = crop_map.get(i, "").strip()

        # Hard cap to 2 sentences if model overtalks
        if narr:
            sentences = re.split(r'(?<=[.!?])\s+', narr)
            narr = " ".join(sentences[:2]).strip()

        narrations.append(narr)

    return narrations


# ================== KEY MANAGER ====================
class KeyManager:
    def __init__(self, keys):
        cleaned = [k.strip() for k in keys if k and k.strip()]
        if not cleaned:
            raise ValueError("No valid API keys provided.")
        self._queue = deque(cleaned)
        self._hard_fail_counts = {k: 0 for k in cleaned}

    def current(self):
        return self._queue[0]

    def rotate(self):
        if len(self._queue) > 1:
            self._queue.rotate(-1)

    def mark_hard_failure(self, key):
        self._hard_fail_counts[key] = self._hard_fail_counts.get(key, 0) + 1
        if self._hard_fail_counts[key] >= MAX_KEY_FAILURES_BEFORE_DROP and key in self._queue:
            self._queue.remove(key)

    def any_keys_left(self):
        return len(self._queue) > 0

# ================== WORKER THREAD ==================
class NarrationWorker(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(int)
    status_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, key_manager, root_folder, target_language, manhwa_name=""):
            super().__init__()
            self.key_manager = key_manager
            self.root_folder = root_folder
            self.target_language = target_language
            self.manhwa_name = manhwa_name.strip() if manhwa_name else ""
            self._is_running = True
            self.system_prompt_text = self._system_prompt()

            # ---------- attempt bookkeeping ----------
            self._attempts = {}          # key: ("chapter", "crop") or ("chapter", "multi")
            def _get_attempts(self, chapter, mode="multi", crop=None):
                k = (chapter, mode) if mode=="multi" else (chapter, crop)
                return self._attempts.setdefault(k, 0)
            def _inc_attempts(self, chapter, mode="multi", crop=None):
                k = (chapter, mode) if mode=="multi" else (chapter, crop)
                self._attempts[k] = self._attempts.get(k, 0) + 1

    # ------------------------------------------------------------------
    #  ADD THESE TWO LINES INSIDE NarrationWorker
    # ------------------------------------------------------------------
    def _get_attempts(self, chapter, mode="multi", crop=None):
        k = (chapter, mode) if mode == "multi" else (chapter, crop)
        return self._attempts.get(k, 0)

    def _inc_attempts(self, chapter, mode="multi", crop=None):
        k = (chapter, mode) if mode == "multi" else (chapter, crop)
        self._attempts[k] = self._attempts.get(k, 0) + 1

    def _rotate_key(self, chapter_name, reason=""):
        old_key = self.key_manager.current()
        self.key_manager.rotate()
        new_key = self.key_manager.current()
        self.log_signal.emit(
            f"[{chapter_name}] 🔁 Key rotated ({reason}): {old_key[:10]}… → {new_key[:10]}…"
        )
    def _note_line_failed_and_maybe_switch(self, chapter_name):
        """
        Count one more consecutive failure; if we hit the threshold
        we rotate the key and reset the counter.
        """
        self.consecutive_failed_lines = getattr(self, 'consecutive_failed_lines', 0) + 1
        if self.consecutive_failed_lines >= CONSEC_FAILS_SWITCH_THRESHOLD:
            self._rotate_key(chapter_name,
                           reason=f"{CONSEC_FAILS_SWITCH_THRESHOLD} consecutive failed lines")
            self.consecutive_failed_lines = 0


    def _system_prompt(self):
        return (
            "You are a manhwa explainer creating narration for a YouTube explanation video.\n"
            "You ALWAYS write in very simple, daily English, even if any other language is requested.\n"
            "It Should like a story use should make a story from given ocr(ignore images ocr)\n"
            "Whole Chapter Will be a story Explained In manner that it be Synced with Images\n"
            "You ALWAYS use third-person narration only (no 'I', 'me', 'my').\n"
            "All crops belong to the same chapter and must sound like one continuous story from start to end.\n"
            "Use smooth hooks so that Crop1 leads into Crop2, Crop2 into Crop3, and so on.\n"
            "Do NOT mention panels, scenes, speech bubbles, text boxes, or camera angles.\n"
            "If a crop looks very tall or very busy, summarize that crop inside its own narration. Do not push its content into the next crop.\n"
            "Your answer must exactly follow the format:\n"
            "Crop1:\\n<text>\\nCrop2:\\n<text>\\n... without any extra text before or after.\n"
        )

    def _system_prompt_single(self):
        return (
            "You are a manhwa explainer creating narration for a YouTube explanation video.\n"
            "You ALWAYS write in very simple, daily English, even if any other language is requested.\n"
            "It Should like a story use should make a story from given ocr(ignore images ocr)\n"
            "Whole Chapter Will be a story Explained In manner that it be Synced with Images\n"
            "You ALWAYS use third-person narration only (no 'I', 'me', 'my').\n"
            "All crops belong to the same chapter and must sound like one continuous story from start to end.\n"
            "Do NOT mention panels, scenes, speech bubbles, text boxes, or camera angles.\n"
            "If a crop looks very tall or very busy, summarize that crop inside its own narration. Do not push its content into the next crop.\n"
            "Output ONLY the plain narration text (max 2 sentences), no labels, no numbers.\n"
        )



    def run(self):
        self._process_root()
        self.finished_signal.emit()

    def stop(self):
        self._is_running = False

    def _process_root(self):
        try:
            chapters = sorted(
                [d for d in os.listdir(self.root_folder) if os.path.isdir(os.path.join(self.root_folder, d))],
                key=natural_sort_key
            )
        except Exception as e:
            self.log_signal.emit(f"FATAL ERROR: Failed to list chapters: {e}")
            return

        if not chapters:
            self.log_signal.emit("INFO: No chapter directories found.")
            return

        # Compute total crops for progress
        total_crops = 0
        chapter_crop_counts = {}
        for ch in chapters:
            crop_dir = os.path.join(self.root_folder, ch, "Waste")
            if os.path.exists(crop_dir):
                cnt = len([f for f in os.listdir(crop_dir)
                           if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff'))])
                chapter_crop_counts[ch] = cnt
                total_crops += cnt

        if total_crops == 0:
            self.log_signal.emit("INFO: No crop images found.")
            return

        self.log_signal.emit(f"INFO: Found {len(chapters)} chapters and {total_crops} total panels. Starting…")

        processed_so_far = 0
        for chapter_name in chapters:
            if not self._is_running:
                self.log_signal.emit("PROCESS STOPPED by user.")
                return
            self.status_signal.emit(f"Chapter: {chapter_name}")
            self._process_chapter(chapter_name, processed_so_far, total_crops)
            processed_so_far += chapter_crop_counts.get(chapter_name, 0)

        self.status_signal.emit("All chapters complete.")

    def _process_chapter(self, chapter_name, base_count, total_crops_all):
        chapter_path = os.path.join(self.root_folder, chapter_name)
        ocr_file = os.path.join(chapter_path, "ExtractText", "combined_ocr_merged.txt")
        crop_dir = os.path.join(chapter_path, "Waste")
        narr_dir = os.path.join(chapter_path, "Narrations")
        os.makedirs(narr_dir, exist_ok=True)

        # OCR
        if not os.path.exists(ocr_file):
            self.log_signal.emit(f"[{chapter_name}] ⚠️ Missing OCR file. Skipping chapter.")
            return
        try:
            with open(ocr_file, "r", encoding="utf-8") as fr:
                ocr_text = fr.read().strip()
        except Exception as e:
            self.log_signal.emit(f"[{chapter_name}] ⚠️ OCR read error: {e}")
            return

        # crops
        if not os.path.exists(crop_dir):
            self.log_signal.emit(f"[{chapter_name}] ⚠️ Missing Crop folder. Skipping.")
            return
        try:
            crops = sorted(
                [
                    f for f in os.listdir(crop_dir)
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff'))
                ],
                key=natural_sort_key
            )
        except Exception as e:
            self.log_signal.emit(f"[{chapter_name}] ⚠️ Error listing crops: {e}")
            return
        if not crops:
            self.log_signal.emit(f"[{chapter_name}] INFO: No crops found.")
            return

        chapter_crop_count = len(crops)

        # Encode all crops up front
        encoded_images = []
        for crop_name in crops:
            crop_path = os.path.join(crop_dir, crop_name)
            base64_img, mime_type, enc_err = encode_image_to_base64(crop_path)
            if enc_err:
                self.log_signal.emit(
                    f"[{chapter_name}] Encoding error for {crop_name}: {enc_err}. Skipping entire chapter."
                )
                return
            encoded_images.append({
                "base64": base64_img,
                "mime_type": mime_type,
                "name": crop_name,
            })

        # Build the big chapter prompt
        manhwa_name = self.manhwa_name if self.manhwa_name else "Unknown Manhwa"
        total_crops = len(encoded_images)

        user_prompt = (
            f"Manhwa name: {manhwa_name}\n"
            f"This is Chapter folder: {chapter_name}\n"
            f"Total No. of Crops are {total_crops}.\n\n"
            "Start narrating in natural-sorted order from Crop1 to Crop"
            f"{total_crops}.\n"
            "You are an explainer. I want to make an explain video. Search and use chapter understanding from OCR and your own knowledge.\n\n"
            "Narration rules:\n"
            "- Always narrate in 3rd person.\n"
            "- Always use very easy English (simple daily use words Delhi English Style).\n"
            "- All the crops that are uploaded are from the same chapter.\n"
            "- Narration should be in a flow. It should sound like a continuous story with connecting hooks between each crop.\n"
            "- If Crop X has big height or a lot of story, SUMMARIZE and write it inside Crop X only. Do NOT extend it into next crop.\n"
            "- While doing narration, look at each image and make sure the narration matches that crop.\n"
            "- If narration for Crop1 is big, complete it inside Crop1 narration. Do not bleed it into Crop2.\n"
            "- Do not mention any voices, text boxes, speech bubbles, or panel words.\n"
            "- Do NOT use phrases like 'this panel shows', 'this scene shifts', 'in this panel', etc.\n"
            "- Use strictly 1-3 sentences per crop, in simple Delhi English.\n\n"
            "Output format (strict):\n"
            "Crop1:\n"
            "<narration for crop 1>\n"
            "Crop2:\n"
            "<narration for crop 2>\n"
            "...\n"
            f"Crop{total_crops}:\n"
            f"<narration for crop {total_crops}>\n"
        )

        self.log_signal.emit(f"[{chapter_name}] Calling Gemini once with {total_crops} crops for this chapter.")

        # FIRST TRY: FULL CHAPTER MODE
        narrations = self._try_chapter_with_key_rotation_on_demand(
            chapter_name,
            encoded_images,
            ocr_text,
            user_prompt,
            expected_count=total_crops
        )

        if narrations:
            # SUCCESS — SAVE MULTI-CROP RESULT
            for idx, img_info in enumerate(encoded_images):
                crop_name = img_info["name"]
                narration_text = narrations[idx] or ""
                out_path = os.path.join(
                    narr_dir,
                    os.path.splitext(crop_name)[0] + ".txt"
                )
                try:
                    with open(out_path, "w", encoding="utf-8") as fw:
                        fw.write(narration_text.strip())
                    self.log_signal.emit(
                        f"[{chapter_name}] 🟢 Saved narration for {crop_name} (Crop{idx+1}) using multi-crop mode."
                    )
                except Exception as e:
                    self.log_signal.emit(
                        f"[{chapter_name}] ⚠️ Save error for {crop_name}: {e}"
                    )
        else:
            # FAILED — FALLBACK TO OLD CROP-WISE MODE
            self.log_signal.emit(
                f"[{chapter_name}] ❌ Full chapter narration failed. Switching to crop-by-crop mode..."
            )

            for idx, img_info in enumerate(encoded_images):
                if not self._is_running:
                    self.log_signal.emit(f"[{chapter_name}] ❌ Process stopped early.")
                    break

                line_idx = idx + 1
                crop_name = img_info["name"]
                base64_img = img_info["base64"]
                mime_type = img_info["mime_type"]

                single_prompt = (
                    f"Manhwa Name: {manhwa_name}\n"
                    f"You are narrating only Crop{line_idx}.\n"
                    "Rules:\n"
                    "- Simple English.\n"
                    "- Third person only.\n"
                    "- Story must match the crop image.\n"
                    "- Max 2 sentences.\n"
                    "- No 'panel shows', no speech bubble mention.\n"
                    "- Summarize if crop is long.\n"
                )

                success = self._try_line_with_key_rotation_on_demand(
                    chapter_name,
                    line_idx,
                    crop_name,
                    base64_img,
                    mime_type,
                    ocr_text,
                    single_prompt,
                    narr_dir
                )

                if not success:
                    self._note_line_failed_and_maybe_switch(chapter_name)
                else:
                    self.consecutive_failed_lines = 0

        # UPDATE PROGRESS BAR
        if total_crops_all:
            progress_value = int(((base_count + chapter_crop_count) / total_crops_all) * 100)
            self.progress_signal.emit(progress_value)

        self.log_signal.emit(f"[{chapter_name}] ✅ Chapter finished.")

    def _drop_key_and_rotate(self, chapter_name, current_key, reason="hard failure"):
        self.key_manager.mark_hard_failure(current_key)
        if not self.key_manager.any_keys_left():
            self.log_signal.emit(f"[{chapter_name}] ❌ All keys dropped. Aborting.")
            self._is_running = False
            return False
        self._rotate_key(chapter_name, reason=reason)
        return True

    def _try_chapter_with_key_rotation_on_demand(
            self, chapter_name, encoded_images, ocr_text,
            user_prompt, expected_count):
        """
        Returns list[ narrations ] on success,
        None when *real* attempts exhausted → caller must switch to single mode.
        """
        while self._get_attempts(chapter_name, "multi") < 3 and self._is_running:
            if not self.key_manager.any_keys_left():
                self.log_signal.emit(f"[{chapter_name}] ❌ No keys left.")
                return None

            key = self.key_manager.current()
            res = call_gemini_api_multi(
                key, encoded_images, ocr_text,
                user_prompt, self.system_prompt_text)

            if res["ok"]:
                narrations = parse_multi_crop_output(res["text"], expected_count)
                if len(narrations) == expected_count:
                    self.log_signal.emit(
                        f"[{chapter_name}] ✅ Multi-crop success.")
                    return narrations
                # parse error counts as real fail
                self._inc_attempts(chapter_name, "multi")
                self.log_signal.emit(
                    f"[{chapter_name}] ⚠️ Parse error  (attempt "
                    f"{self._get_attempts(chapter_name, 'multi')}/3)")
                continue

            # ---------- handle failures ----------
            status = res.get("status")
            err  = res.get("error", "unknown")

            if status == 429:          # quota → rotate key, **do not** count attempt
                self._rotate_key(chapter_name, reason="429 quota")
                continue

            if status in (401, 403, 400):   # bad key → drop + rotate, **do** count attempt
                self._inc_attempts(chapter_name, "multi")
                if not self._drop_key_and_rotate(chapter_name, key, reason=f"HTTP {status}"):
                    return None
                continue

            # any other error counts as real attempt
            self._inc_attempts(chapter_name, "multi")
            att = self._get_attempts(chapter_name, "multi")
            self.log_signal.emit(
                f"[{chapter_name}] API error (attempt {att}/3): {err}")

        # ---------- exhausted ----------
        self.log_signal.emit(
            f"[{chapter_name}] ❌ Multi-mode failed 3 real attempts → fallback to single.")
        return None

    def _try_line_with_key_rotation_on_demand(
            self, chapter_name, line_idx, crop_name,
            base64_img, mime_type, ocr_text, user_prompt, narr_dir):
        """
        Returns True  → file saved
                False → 3 real attempts exhausted → skip crop
        """
        while self._get_attempts(chapter_name, "single", line_idx) < 3 and self._is_running:
            if not self.key_manager.any_keys_left():
                self.log_signal.emit(f"[{chapter_name}] ❌ No keys left.")
                return False

            key = self.key_manager.current()
            res = call_gemini_api(
                key, base64_img, mime_type, ocr_text,
                user_prompt, self._system_prompt_single())

            if res["ok"]:
                text = extract_narration_text(res["text"])
                if text.startswith("Error"):
                    # parse error counts as real fail
                    self._inc_attempts(chapter_name, "single", line_idx)
                    self.log_signal.emit(
                        f"[{chapter_name} / line {line_idx}] Parse error  (attempt "
                        f"{self._get_attempts(chapter_name, 'single', line_idx)}/3)")
                    continue

                # success → save & exit
                out_path = os.path.join(narr_dir, os.path.splitext(crop_name)[0] + ".txt")
                try:
                    with open(out_path, "w", encoding="utf-8") as fw:
                        fw.write(text.strip())
                    self.log_signal.emit(
                        f"[{chapter_name} / line {line_idx}] ✅ Saved narration.")
                    return True
                except Exception as e:
                    # save error counts as real fail
                    self._inc_attempts(chapter_name, "single", line_idx)
                    self.log_signal.emit(
                        f"[{chapter_name} / line {line_idx}] Save error  (attempt "
                        f"{self._get_attempts(chapter_name, 'single', line_idx)}/3): {e}")
                    continue

            # ---------- handle failures ----------
            status = res.get("status")
            err  = res.get("error", "unknown")

            if status == 429:          # quota → rotate key, **do not** count attempt
                self._rotate_key(chapter_name, reason="429 quota")
                continue

            if status in (401, 403, 400):   # bad key → drop + rotate, **do** count attempt
                self._inc_attempts(chapter_name, "single", line_idx)
                if not self._drop_key_and_rotate(chapter_name, key, reason=f"HTTP {status}"):
                    return False
                continue

            # any other error counts as real attempt
            self._inc_attempts(chapter_name, "single", line_idx)
            att = self._get_attempts(chapter_name, "single", line_idx)
            self.log_signal.emit(
                f"[{chapter_name} / line {line_idx}] API error (attempt {att}/3): {err}")

        # ---------- exhausted → skip ----------
        self.log_signal.emit(
            f"[{chapter_name} / line {line_idx}] ❌ Skipping crop after 3 real attempts.")
        # write empty file so downstream knows it was processed
        out_path = os.path.join(narr_dir, os.path.splitext(crop_name)[0] + ".txt")
        try:
            with open(out_path, "w", encoding="utf-8") as fw:
                fw.write("")
        except Exception as e:
            self.log_signal.emit(
                f"[{chapter_name} / line {line_idx}] ⚠️ Could not write skip-placeholder: {e}")
        return False

# =================== MAIN TAB (UI) =================
class NarrationGeminiTab(QWidget):
    operation_finished = Signal()

    SUPPORTED_LANGUAGES = ["English", "Khmer (Cambodian)", "English (English)", "Korean", "Japanese", "Spanish"]

    def __init__(self):
        super().__init__()
        self.worker = None
        self.root_folder = ""
        self.setLayout(QVBoxLayout())
        self._setup_ui()
        self._update_buttons(is_running=False)
    # ------------------------------------------------------------------
    #  ADD THESE TWO LINES INSIDE NarrationWorker
    # ------------------------------------------------------------------

    def start_task(self):
    
        self._start()

    def _setup_ui(self):
        # Config
        cfg = QGroupBox("Configuration")
        cfg_v = QVBoxLayout(cfg)

        # multi-key input
        api_v = QVBoxLayout()
        api_v.addWidget(QLabel("Gemini API Keys (one per line or comma-separated):"))
        self.api_keys_text = QTextEdit()
        self.api_keys_text.setPlaceholderText("key_1\nkey_2\nkey_3 ...")
        self.api_keys_text.textChanged.connect(lambda: self._update_buttons(is_running=False))
        api_v.addWidget(self.api_keys_text)
        cfg_v.addLayout(api_v)

        # root folder
        f_h = QHBoxLayout()
        f_h.addWidget(QLabel("Root Folder:"))
        self.root_folder_display = QLineEdit("Select the folder containing ChapterXX directories.")
        self.root_folder_display.setReadOnly(True)
        f_h.addWidget(self.root_folder_display)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse_folder)
        f_h.addWidget(self.browse_button)
        cfg_v.addLayout(f_h)
        # Manhwa name
        m_h = QHBoxLayout()
        m_h.addWidget(QLabel("Manhwa Name (for prompt):"))
        self.manhwa_name_line = QLineEdit()
        self.manhwa_name_line.setPlaceholderText("Enter manhwa / webtoon name (optional)")
        m_h.addWidget(self.manhwa_name_line)
        cfg_v.addLayout(m_h)


        # language
        l_h = QHBoxLayout()
        l_h.addWidget(QLabel("Target Narration Language:"))
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(self.SUPPORTED_LANGUAGES)
        self.lang_combo.setCurrentIndex(0)
        l_h.addWidget(self.lang_combo)
        cfg_v.addLayout(l_h)

        self.layout().addWidget(cfg)

        # Controls
        ctr = QGroupBox("Controls")
        ctr_h = QHBoxLayout(ctr)
        self.start_button = QPushButton("✨ START BATCH NARRATION ✨")
        self.start_button.clicked.connect(self._start)
        ctr_h.addWidget(self.start_button)
        self.stop_button = QPushButton("🛑 STOP PROCESS")
        self.stop_button.clicked.connect(self._stop)
        ctr_h.addWidget(self.stop_button)
        self.layout().addWidget(ctr)

        # Status & Log
        stat = QGroupBox("Process Status")
        stat_v = QVBoxLayout(stat)
        self.status_label = QLabel("Ready.")
        stat_v.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        stat_v.addWidget(self.progress_bar)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        stat_v.addWidget(self.log_output)
        self.layout().addWidget(stat)

    # ------------- handlers -------------
    @Slot()
    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Root Manhwa Folder")
        if folder:
            self.root_folder = folder
            self.root_folder_display.setText(folder)
            self.status_label.setText(f"Root selected: {os.path.basename(folder)}")
            self._update_buttons(is_running=False)

    def _update_buttons(self, is_running):
        has_keys = bool(self._collect_keys())
        self.start_button.setEnabled(not is_running and bool(self.root_folder) and has_keys)
        self.stop_button.setEnabled(is_running)
        self.browse_button.setEnabled(not is_running)
        self.api_keys_text.setEnabled(not is_running)
        self.lang_combo.setEnabled(not is_running)

    def _collect_keys(self):
        raw = self.api_keys_text.toPlainText()
        if not raw:
            return []
        parts = []
        for line in raw.splitlines():
            parts.extend([p.strip() for p in line.split(",") if p.strip()])
        return parts

    @Slot(str)
    def _on_log(self, msg):
        self.log_output.append(msg)

    @Slot(int)
    def _on_progress(self, v):
        self.progress_bar.setValue(v)

    @Slot(str)
    def _on_status(self, s):
        self.status_label.setText(s)

    @Slot()
    def _on_finished(self):
        self.status_label.setText("Batch process completed or stopped.")
        self.log_output.append("--- Batch Narration Process Finished ---")
        self.worker = None
        self._update_buttons(is_running=False)
        self.progress_bar.setValue(100)
        self.operation_finished.emit()

    @Slot()
    def _start(self):
        keys = self._collect_keys()
        lang = self.lang_combo.currentText()
        if not keys:
            QMessageBox.warning(self, "Input Error", "Enter at least one Gemini API key.")
            return
        if not self.root_folder or not os.path.exists(self.root_folder):
            QMessageBox.warning(self, "Input Error", "Select a valid root folder.")
            return

        self.log_output.clear()
        self.progress_bar.setValue(0)
        self.log_output.append(f"--- Starting Batch Narration (Language: {lang}) ---")
        self.status_label.setText(f"Starting…")

        self._update_buttons(is_running=True)

        try:
            km = KeyManager(keys)
        except ValueError as e:
            QMessageBox.critical(self, "Key Error", str(e))
            self._update_buttons(is_running=False)
            return

        self.worker = NarrationWorker(
            km,
            self.root_folder,
            lang,
            manhwa_name=self.manhwa_name_line.text().strip()
        )

        self.worker.log_signal.connect(self._on_log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.status_signal.connect(self._on_status)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    @Slot()
    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.status_label.setText("Stopping… waiting for current API call to finish.")
            self._update_buttons(is_running=True)

    def cleanup(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(5000)
            if self.worker.isRunning():
                self.worker.terminate()