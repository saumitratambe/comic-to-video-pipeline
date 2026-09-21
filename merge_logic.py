import os
import sys
import re
import traceback
import threading
import time
import requests
import base64
import shutil
from natsort import natsorted
from PIL import Image
try:
    from natsort import natsorted
except Exception:
    natsorted = None
import re, math




# Global chunk sequence for chapter-level numbering
# === Part 2 helpers ===
def _count_words_simple(s: str) -> int:
    return len([w for w in (s or "").strip().split() if w.strip()])

def _get_chapter_dir_from_any_path(path_str: str) -> str:
    # path like .../<Chapter>/Extracttext/chunk
    import os
    p = os.path.abspath(path_str)
    # chunk dir
    p = os.path.dirname(p)
    # Extracttext
    p = os.path.dirname(p)
    # Chapter
    return os.path.dirname(p)

def _gemini_key_pool():
    import os
    keys = os.getenv("GEMINI_KEYS", "").strip()
    if keys:
        return [k.strip() for k in keys.split(",") if k.strip()]
    single = os.getenv("GEMINI_API_KEY", "").strip()
    return [single] if single else []

def _gemini_generate_line(mode: str, chunk_png_path: str, prev_text: str, curr_text: str, next_text: str) -> str:
    """
    mode: 'create' for 0 words (12-20 words), 'expand' for 1-19 words (~25-45 words)
    """
    keys = _gemini_key_pool()
    if not keys:
        # fallback neutral
        return curr_text if curr_text else "A brief silent beat; the scene holds."
    model = "gemini-flash-lite-latest"
    b64 = ""
    try:
        with open(chunk_png_path, "rb") as f:
            import base64
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        pass

    if mode == "create":
        instruction = ("Write ONE OCR-like line (12–20 words), descriptive, no quotes, no new names. "
                       "Use ONLY what the image shows and fit between previous and next text.")
    else:
        instruction = ("Expand the OCR into ONE clearer OCR-like line (~25–45 words), keep meaning, "
                       "reuse phrases, no quotes, no new names, fit between neighbors.")

    payload = {
      "contents": [{
        "parts": [
          {"text": instruction + "\n\nPREVIOUS:\n" + (prev_text or "") + "\n\nCURRENT:\n" + (curr_text or "") + "\n\nNEXT:\n" + (next_text or "")},
          {"inline_data": {"mime_type":"image/png","data": b64}}
        ]
      }]
    }

    import requests, time
    last_err = None
    for i in range(len(keys)+1):
        key = keys[i % len(keys)]
        if not key:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        try:
            r = requests.post(url, json=payload, timeout=45)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()
            out = (data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")).strip()
            out = " ".join(out.split())
            if not out:
                continue
            return out
        except Exception as e:
            last_err = e
            time.sleep(1)
            continue
    # fallback
    return curr_text if curr_text else "A brief silent beat; the scene holds."

_GLOBAL_CHUNK_SEQ = 1

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QLineEdit, QProgressBar, QGroupBox, QSpinBox
    , QFileDialog, QCheckBox, QApplication, QMenu
)
# --- MODIFICATION: Added QAction and Qt for custom context menu functionality ---
from PySide6.QtGui import QAction
from PySide6.QtCore import Signal, QThread, QObject, Slot, Qt

try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import numpy as np
    import cv2
    import pytesseract
except ImportError:
    np, cv2, pytesseract = None, None, None

TESSERACT_EXEC_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if pytesseract and os.path.exists(TESSERACT_EXEC_PATH):
    try:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXEC_PATH
    except Exception as e:
        print(f"Warning: Could not set pytesseract command path: {e}")
if Image:
    Image.MAX_IMAGE_PIXELS = None

GLOBAL_DEFAULT_CHUNK_HEIGHT = 3000
GLOBAL_DEFAULT_LANG_CODE = "eng"
GLOBAL_REMOVE_TEXT_CONFIDENCE_THRESHOLD = 60
GLOBAL_REMOVE_TEXT_DILATION_KERNEL_SIZE = (10, 10)
GLOBAL_REMOVE_TEXT_INPAINT_RADIUS = 5
GLOBAL_CROP_CLEANUP_SIZE_LIMIT = 300 * 1024
FOLDER_NAME_COMBINE = "Combine"
FOLDER_NAME_OCR = "ExtractText"
FOLDER_NAME_REMOVE_TEXT = "RemoveText"
FOLDER_NAME_CROP = "Crop"

# ==== Big-crop auto-split helpers ====
LARGE_CROP_BYTES = 1_600_000      # 1.6 MB threshold
TARGET_PART_BYTES = 1_050_000     # aim ~1.0–1.2 MB each
MAX_EXTRA_PARTS = 3               # 2–3 parts total

def _robust_cv2_read(path):
    try:
        with open(os.path.normpath(path), "rb") as f:
            import numpy as np, cv2
            data = np.frombuffer(f.read(), dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None

def _find_seam_rows(img):
    """Find horizontal separator rows (very white/black) to cut on panel gaps."""
    import numpy as np, cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    white_rows = (np.mean(gray > 240, axis=1) > 0.98)
    black_rows = (np.mean(gray <  15, axis=1) > 0.98)
    sep = (white_rows | black_rows).astype(np.uint8)

    seams, in_run, start = [], False, 0
    for y, v in enumerate(sep):
        if v and not in_run:
            in_run, start = True, y
        elif not v and in_run:
            in_run = False
            seams.append((start + y) // 2)
    if in_run:
        seams.append((start + len(sep) - 1) // 2)

    if seams:
        H = img.shape[0]
        lo, hi = int(H*0.02), int(H*0.98)
        seams = [s for s in seams if lo <= s <= hi]
    return sorted(seams)

def _save_png(img_cv, out_path):
    import cv2, os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ok, buf = cv2.imencode(".png", img_cv, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise RuntimeError("PNG encode failed")
    with open(out_path, "wb") as f:
        f.write(buf.tobytes())

def _split_one_large_crop(path_in, out_dir, logger=print):
    """
    If file size ≥ 1.6MB -> split into 2–3 vertical parts.
    Prefer seams (panel gaps). Else split evenly by height.
    Returns list of new part paths. Deletes original if split.
    """
    import os, math
    size = os.path.getsize(path_in)
    if size < LARGE_CROP_BYTES:
        return []

    img = _robust_cv2_read(path_in)
    if img is None:
        logger(f"⚠️ Cannot read large crop: {os.path.basename(path_in)}"); 
        return []

    H = img.shape[0]
    est_parts = max(2, min(MAX_EXTRA_PARTS, round(size / TARGET_PART_BYTES)))
    seams = _find_seam_rows(img)

    # choose cut rows
    if seams:
        targets = [int(H * k / est_parts) for k in range(1, est_parts)]
        cuts = sorted({min(seams, key=lambda s: abs(s - t)) for t in targets})
    else:
        step = H // est_parts
        cuts = [step, step*2][:est_parts-1]

    bounds = [0] + cuts + [H]
    parts = []
    base = os.path.splitext(os.path.basename(path_in))[0]
    for i in range(len(bounds)-1):
        y0, y1 = bounds[i], bounds[i+1]
        if y1 - y0 <= 10:  # skip tiny slivers
            continue
        part_img = img[y0:y1, :, :]
        out_path = os.path.join(out_dir, f"{base}_part{i+1}.png")
        try:
            _save_png(part_img, out_path)
            parts.append(out_path)
        except Exception as e:
            logger(f"❌ Split save failed: {os.path.basename(out_path)} -> {e}")

    if len(parts) >= 2:
        try: os.remove(path_in)
        except Exception: pass

    return parts

def _split_large_crops_in_folder(crop_dir, logger=print):
    """
    Walk Crop/ and split any image ≥ 1.6MB into 2–3 parts.
    Then rename sequentially: Crop1.png, Crop2.png, …
    """
    import os
    exts = (".png",".jpg",".jpeg",".webp",".bmp",".tiff")
    files = [f for f in os.listdir(crop_dir) if f.lower().endswith(exts)]
    if not files:
        return 0, 0
    changed = 0
    for name in files:
        p = os.path.join(crop_dir, name)
        try:
            os.path.getsize(p)
        except Exception:
            continue
        parts = _split_one_large_crop(p, crop_dir, logger=logger)
        if parts:
            changed += 1

    # keep nice order after new parts are added
    try:
        _rename_files_sequential(crop_dir, log=lambda m, c="gray": logger(m))
    except Exception:
        pass
    return changed, len(files)

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split('([0-9]+)', s)]

def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def safe_write_text(p, t):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
    except Exception:
        pass

def list_image_files_for_combine(p):
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
    try:
        return natsorted([f for f in os.listdir(p) if f.lower().endswith(exts)])
    except FileNotFoundError:
        return []

def combine_images_from_folder(src_folder_path, out_base, log):
    if not Image: return []
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
    files = natsorted([f for f in os.listdir(src_folder_path) if f.lower().endswith(exts)])
    total_files = len(files)
    if total_files == 0:
        log(f"⚠️ No image files found in {os.path.basename(src_folder_path)}", "orange")
        return []
    os.makedirs(out_base, exist_ok=True)
    target_width = 0
    try:
        with Image.open(os.path.join(src_folder_path, files[0])) as first_img:
            target_width = first_img.width
    except Exception as e:
        log(f"⚠️ Could not read first image to determine width: {e}", "red")
        return []
    CHUNK_LIMIT = 655000
    combined_img = Image.new('RGB', (target_width, CHUNK_LIMIT), (255, 255, 255))
    y_offset = 0
    chunk_num = 1
    out_files = []
    for f in files:
        fp = os.path.join(src_folder_path, f)
        try:
            img = Image.open(fp).convert('RGB')
            if img.width != target_width:
                ratio = target_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
            if y_offset + img.height > CHUNK_LIMIT:
                out_file_chunk = os.path.join(out_base, f"combined_part{chunk_num}.png")
                combined_img.crop((0, 0, target_width, y_offset)).save(out_file_chunk, "PNG", compress_level=9)
                # --- FIX 2a: Normalize output path
                out_files.append(os.path.normpath(os.path.abspath(out_file_chunk)))
                chunk_num += 1
                y_offset = 0
                combined_img = Image.new('RGB', (target_width, CHUNK_LIMIT), (255, 255, 255))
            combined_img.paste(img, (0, y_offset))
            y_offset += img.height
        except Exception as e:
            log(f"❌ Failed to process and paste {os.path.basename(fp)}: {e}", "red")
            continue
    if y_offset > 0:
        out_file_final_chunk = os.path.join(out_base, f"combined_part{chunk_num}.png")
        combined_img.crop((0, 0, target_width, y_offset)).save(out_file_final_chunk, "PNG", compress_level=9)
        # --- FIX 2b: Normalize output path
        out_files.append(os.path.normpath(os.path.abspath(out_file_final_chunk)))
    return out_files

def ocr_combined_folder(combined_folder, out_text_folder, chunk_height, lang, log):
    if not pytesseract: return []
    images = [os.path.join(combined_folder, f) for f in list_image_files_for_combine(combined_folder)]
    if not images: return []
    ensure_dir(out_text_folder)
    merged_texts = []
    stop_flag = getattr(threading.current_thread(), 'stop_flag', threading.Event())
    for img_path in images:
        if stop_flag.is_set(): break
        txt = _ocr_image_in_chunks(img_path, chunk_height=chunk_height, lang=lang, log=log)
        merged_texts.append(txt)
    written = []
    if not stop_flag.is_set():
        merged_path = os.path.join(out_text_folder, "combined_ocr_merged.txt")
        safe_write_text(merged_path, "\n\n".join(merged_texts))
        written.append(merged_path)
    return written



def _ocr_image_in_chunks(image_path, chunk_height, lang, log):
    if not pytesseract or not Image: return ""
    try:
        img = Image.open(image_path)
    except Exception:
        return ""
    w, h = img.size
    texts = []
    stop_flag = getattr(threading.current_thread(), 'stop_flag', threading.Event())
    for top in range(0, h, chunk_height):
        if stop_flag.is_set(): break
        bottom = min(top + chunk_height, h)
        chunk = img.crop((0, top, w, bottom))
        try:
            txt = pytesseract.image_to_string(chunk, lang=lang)
            texts.append(txt.strip())
        except Exception:
            texts.append("")
    return "\n".join([t for t in texts if t])




def remove_text_in_folder(input_paths, out_folder, log):
    ensure_dir(out_folder)
    results = []
    stop_flag = getattr(threading.current_thread(), 'stop_flag', None)
    for p in input_paths:
        if stop_flag and stop_flag.is_set(): break
        base = os.path.splitext(os.path.basename(p))[0]
        outp = os.path.join(out_folder, base + "_clean.png")
        ok = _remove_text_from_image(p, outp, log=log)
        if ok:
            results.append(outp)
    return results

def _remove_text_from_image(path_in, path_out, log):
    if not cv2 or not pytesseract or not np: return False

    # --- FIX 3a MODIFIED: Robustly read file into memory for cv2
    normalized_path = os.path.normpath(path_in)
    img = None
    try:
        # Read file as binary data
        with open(normalized_path, 'rb') as f:
            data = f.read()
        # Decode the binary data using cv2
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        log(f"⚠️ Failed reading image for RemoveText ({e}): {os.path.basename(path_in)}", "red")

    if img is None:
        log(f"⚠️ Cannot read image (via imdecode): {path_in}"); return False

    H, W = img.shape[:2]
    final_img = img.copy()
    chunk_height = GLOBAL_DEFAULT_CHUNK_HEIGHT
    for top in range(0, H, chunk_height):
        bottom = min(top + chunk_height, H)
        chunk = img[top:bottom, :].copy()
        gray = cv2.cvtColor(chunk, cv2.COLOR_BGR2GRAY)
        try:
            data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
        except Exception:
            continue
        mask = np.zeros(gray.shape, dtype=np.uint8)
        for j in range(len(data.get('level', []))):
            try: conf = int(float(data['conf'][j]))
            except Exception: conf = -1
            text = (data['text'][j] or "").strip()
            if conf >= GLOBAL_REMOVE_TEXT_CONFIDENCE_THRESHOLD and text and any(c.isalnum() for c in text):
                x, y, w_box, h_box = (data['left'][j], data['top'][j], data['width'][j], data['height'][j])
                x_end = min(x + w_box, chunk.shape[1])
                y_end = min(y + h_box, chunk.shape[0])
                if x < x_end and y < y_end:
                    mask[y:y_end, x:x_end] = 255
        if mask.sum() == 0: continue
        kernel = np.ones(GLOBAL_REMOVE_TEXT_DILATION_KERNEL_SIZE, np.uint8)
        mask = cv2.dilate(mask, kernel, iterations=1)
        try:
            inpainted = cv2.inpaint(chunk, mask, GLOBAL_REMOVE_TEXT_INPAINT_RADIUS, cv2.INPAINT_NS)
            final_img[top:bottom, :] = inpainted
        except Exception:
            final_img[top:bottom, :] = chunk
    ensure_dir(os.path.dirname(path_out) or ".")
    try:
        cv2.imwrite(path_out, final_img)
        return True
    except Exception:
        return False

def _detect_and_save_panels(src_folder_path, out_base, log):
    if not cv2 or not np or not Image: return 0
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
    try:
        files_to_crop = natsorted([f for f in os.listdir(src_folder_path) if f.lower().endswith(exts)])
    except FileNotFoundError:
        log(f"❌ Input folder not found: {src_folder_path}", "red")
        return 0
    if not files_to_crop:
        log(f"⚠️ No image files found in {os.path.basename(src_folder_path)}", "orange")
        return 0
    os.makedirs(out_base, exist_ok=True)
    total_panels_saved = 0
    stop_flag = getattr(threading.current_thread(), 'stop_flag', None)
    for fname in files_to_crop:
        if stop_flag and stop_flag.is_set(): break
        img_path = os.path.join(src_folder_path, fname)
        try:
            panels = _detect_panels_from_path(img_path, log)
            if not panels:
                log(f"⚠️ No panels detected in {fname}.", "orange")
                continue
            base_name = os.path.splitext(fname)[0]
            for panel_idx, panel in enumerate(panels, start=1):
                panel_file = os.path.join(out_base, f"{base_name}_p{panel_idx}.png")
                panel.save(panel_file, "PNG", compress_level=9)
                total_panels_saved += 1
            log(f"✂️ Extracted {len(panels)} panels from {fname}", "gray")
        except Exception as e:
            log(f"❌ Panel processing failed for {fname}: {e}", "red")
            continue

    if not stop_flag or not stop_flag.is_set():
        if total_panels_saved > 0:
            deleted_count = _delete_small_files_auto(out_base, size_limit=GLOBAL_CROP_CLEANUP_SIZE_LIMIT, log=log)
            if deleted_count > 0:
                log(f"🗑️ Deleted {deleted_count} small panels (< {GLOBAL_CROP_CLEANUP_SIZE_LIMIT//1024}KB)", "blue")
            renamed_count = _rename_files_sequential(out_base, log=log)
            if renamed_count > 0:
                log(f"📝 Renamed {renamed_count} files sequentially (Crop1.png, Crop2.png, ...)", "blue")

    return total_panels_saved

def _detect_panels_from_path(image_path, log):
    if not cv2 or not np or not Image: return []

    # --- FIX 3b MODIFIED: Robustly read file into memory for cv2
    normalized_path = os.path.normpath(image_path)
    img_cv = None
    try:
        # Read file as binary data
        with open(normalized_path, 'rb') as f:
            data = f.read()
        # Decode the binary data using cv2
        img_cv = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        log(f"⚠️ Failed reading image for Crop ({e}): {os.path.basename(image_path)}", "red")

    if img_cv is None:
        log(f"⚠️ Could not read image for panel detection: {os.path.basename(image_path)}", "orange")
        return []
    H, _, _ = img_cv.shape
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    white_rows = np.mean(gray > 240, axis=1) > 0.98
    black_rows = np.mean(gray < 15, axis=1) > 0.98
    sep = np.logical_or(white_rows, black_rows)
    panels = []
    in_panel = False
    top = 0
    min_panel_height = 50
    min_pixels = 30000
    for y in range(H):
        if not in_panel and not sep[y]:
            top = y
            in_panel = True
        elif in_panel and sep[y]:
            bottom = y
            in_panel = False
            if (bottom - top) >= min_panel_height:
                panel_img_cv = img_cv[top:bottom, :]
                if panel_img_cv.size >= min_pixels:
                    panels.append(Image.fromarray(cv2.cvtColor(panel_img_cv, cv2.COLOR_BGR2RGB)))
    if in_panel and (H - top) >= min_panel_height and (H - top) * img_cv.shape[1] >= min_pixels:
        panels.append(Image.fromarray(cv2.cvtColor(img_cv[top:H, :], cv2.COLOR_BGR2RGB)))
    return panels

def _delete_small_files_auto(folder, size_limit, log):
    if not os.path.exists(folder): return 0
    deleted_count = 0
    for filename in os.listdir(folder):
        filepath = os.path.join(folder, filename)
        if os.path.isfile(filepath):
            try:
                if os.path.getsize(filepath) < size_limit:
                    os.remove(filepath)
                    deleted_count += 1
            except Exception as e:
                log(f"⚠️ Failed to delete {filename}: {e}", "orange")
    return deleted_count

def _rename_files_sequential(folder, log):
    """
    Safely rename ALL images in 'folder' to Crop1.png, Crop2.png, ...
    Uses a 2-phase rename to avoid collisions and leftovers.
    """
    if not os.path.exists(folder): 
        return 0

    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
    try:
        files = [f for f in os.listdir(folder) if f.lower().endswith(exts)]
    except Exception as e:
        log(f"⚠️ Rename: cannot list folder: {e}", "orange")
        return 0

    # Natural order (so numbering is stable)
    try:
        files = natsorted(files)
    except Exception:
        import re
        files = sorted(files, key=lambda s: [int(t) if t.isdigit() else t.lower()
                                             for t in re.split(r'(\d+)', str(s))])

    if not files:
        return 0

    # PHASE 0: clean old leftover temps that match our pattern
    for f in list(files):
        if f.startswith("__TMP_REN__"):
            try:
                os.remove(os.path.join(folder, f))
            except Exception:
                pass

    # PHASE 1: rename every file to a collision-free temp name
    tmp_names = []
    for i, name in enumerate(files):
        src = os.path.join(folder, name)
        tmp = os.path.join(folder, f"__TMP_REN__{i:04d}.png")
        # make sure we don't collide with an existing file
        k = 0
        while os.path.exists(tmp):
            k += 1
            tmp = os.path.join(folder, f"__TMP_REN__{i:04d}_{k}.png")
        try:
            os.replace(src, tmp)  # replace avoids collisions
            tmp_names.append(tmp)
        except Exception as e:
            log(f"⚠️ Rename phase1 failed for {name}: {e}", "orange")

    # PHASE 2: rename temps to final Crop#.png in order
    renamed_count = 0
    for i, tmp in enumerate(tmp_names):
        final = os.path.join(folder, f"Crop{i+1}.png")
        try:
            # if a final already exists (from older run), overwrite it
            os.replace(tmp, final)
            renamed_count += 1
        except Exception as e:
            log(f"⚠️ Rename phase2 failed for {os.path.basename(tmp)} -> {os.path.basename(final)}: {e}", "orange")

    return renamed_count


def _build_collages_grid(crop_dir, out_dir, rows, cols, cell_w, pad, logger=print):
    """
    Make paged collages: each page lays out (rows x cols) crops in a grid.
    - If cell_w == 0, uses the first crop's width.
    - Heights are proportional per image; each row is centered using that row's max height.
    """
    os.makedirs(out_dir, exist_ok=True)
    exts = ('.png','.jpg','.jpeg','.webp','.bmp','.tiff')
    files = [f for f in os.listdir(crop_dir) if f.lower().endswith(exts)]
    if not files:
        logger("⚠️ Collage: no crops found."); return []

    try:
        files = natsorted(files)
    except Exception:
        files.sort(key=lambda s: [int(t) if t.isdigit() else s.lower()
                                  for s in re.split(r'(\d+)', str(s))])

    page_size = max(1, rows*cols)
    pages = [files[i:i+page_size] for i in range(0, len(files), page_size)]
    out_paths = []

    if cell_w <= 0:
        with Image.open(os.path.join(crop_dir, pages[0][0])) as im0:
            cell_w = im0.size[0]

    for p_idx, page_files in enumerate(pages, start=1):
        scaled_sizes = []
        for name in page_files:
            try:
                with Image.open(os.path.join(crop_dir, name)) as im:
                    w, h = im.size
                scale = cell_w / max(1, w)
                scaled_sizes.append((name, cell_w, max(1, int(round(h*scale)))))
            except Exception as e:
                logger(f"❌ Collage: failed to read {name}: {e}")

        if not scaled_sizes:
            continue

        row_heights = []
        for r in range(rows):
            row_items = scaled_sizes[r*cols:(r+1)*cols]
            row_heights.append(max([hh for _,_,hh in row_items], default=0))

        canvas_w = pad + (cols*cell_w) + ((cols-1)*pad) + pad
        canvas_h = pad + sum(row_heights) + (max(0, rows-1)*pad) + pad
        canvas = Image.new("RGB", (canvas_w, canvas_h), (0,0,0))

        y = pad
        for r in range(rows):
            row_items = scaled_sizes[r*cols:(r+1)*cols]
            if not row_items:
                continue
            x = pad
            row_h = row_heights[r]
            for name, w_scaled, h_scaled in row_items:
                try:
                    with Image.open(os.path.join(crop_dir, name)) as im:
                        im = im.resize((w_scaled, h_scaled), Image.LANCZOS).convert("RGB")
                    y_off = y + (row_h - h_scaled)//2
                    canvas.paste(im, (x, y_off))
                except Exception as e:
                    logger(f"❌ Collage: failed placing {name}: {e}")
                x += w_scaled + pad
            y += row_h + pad

        out_name = f"Collage_{p_idx}.png"
        out_path = os.path.join(out_dir, out_name)
        try:
            canvas.save(out_path, "PNG", optimize=True, compress_level=9)
            out_paths.append(out_path)
        except Exception as e:
            logger(f"❌ Collage: failed saving {out_name}: {e}")

    logger(f"🧩 Collage: created {len(out_paths)} page(s).")
    return out_paths



# --- NEW CLASS: ContextMenuLineEdit for path inputs ---
class ContextMenuLineEdit(QLineEdit):
    """
    A QLineEdit that ensures a 'Copy Path' action is available
    in the context menu.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Enable custom context menu policy
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def show_context_menu(self, pos):
        # Get the standard context menu (includes cut, copy, paste, select all)
        menu = self.createStandardContextMenu()

        # Add a separator before the custom action
        menu.addSeparator()

        # Add the custom 'Copy Path' action
        copy_path_action = QAction("Copy Path", self)
        copy_path_action.triggered.connect(self._copy_text_to_clipboard)
        menu.addAction(copy_path_action)

    def _copy_text_to_clipboard(self):
        """Copies the current text of the QLineEdit to the system clipboard."""
        text_to_copy = self.text()
        if text_to_copy:
            clipboard = QApplication.clipboard()
            clipboard.setText(text_to_copy)

# -----------------------------------------------------------------------------

class NestedOutputWorker(QThread):
    log = Signal(str, str)
    folder_finished = Signal(str)

    def __init__(self, input_folder, output_base, chapter_name, step_name, **kwargs):
        super().__init__()
        # --- FIX: Added check to ensure chapter_name is always set ---
        self.chapter_name = chapter_name if chapter_name else os.path.basename(input_folder)
        # -------------------------------------------------------------
        self.input_folder = input_folder
        self.output_base = output_base
        self.stop_flag = threading.Event()
        self.folder_name = chapter_name
        self.step_name = step_name
        self.output_folder = os.path.join(self.output_base, self.folder_name, self.step_name)

    def stop(self): self.stop_flag.set()

    def _emit(self, msg, color="black"): self.log.emit(f"[{self.folder_name}] [{self.step_name}] {msg}", color)

class CombineWorker(NestedOutputWorker):
    def __init__(self, input_folder, output_base, chapter_name, **kwargs):
        super().__init__(input_folder, output_base, chapter_name, FOLDER_NAME_COMBINE, **kwargs)
    def run(self):
        _log = lambda msg, color="gray": self._emit(msg, color)
        _log(f"🚀 Starting combination. Output: **{os.path.basename(self.output_folder)}**", "blue")
        # Ensure input folder is normalized before use
        normalized_input = os.path.normpath(self.input_folder)
        results = combine_images_from_folder(normalized_input, self.output_folder, _log)
        if not self.stop_flag.is_set() and results:
            _log(f"🎉 Combine finished: Saved {len(results)} image chunk(s) to '{os.path.basename(self.output_folder)}'.", "green")
        elif not results:
            _log(f"❌ Combine failed or found no input files in '{os.path.basename(normalized_input)}'.", "red")
        self.folder_finished.emit(self.folder_name)

class OcrWorker(NestedOutputWorker):
    def __init__(self, input_folder, output_base, chapter_name, **kwargs):
        super().__init__(input_folder, output_base, chapter_name, FOLDER_NAME_OCR, **kwargs)
        self.chunk_height = kwargs.get('chunk_height')
        self.lang_code = kwargs.get('lang_code')
    def run(self):
        _log = lambda msg, color="gray": self._emit(msg, color)
        if not pytesseract:
            _log("❌ Tesseract Python library not found. Skipping.", "red")
            self.folder_finished.emit(self.folder_name)
            return
        _log(f"🚀 Starting OCR. Output: **{os.path.basename(self.output_folder)}**", "blue")
        # Ensure input folder is normalized before use
        normalized_input = os.path.normpath(self.input_folder)
        results = ocr_combined_folder(
            combined_folder=normalized_input,
            out_text_folder=self.output_folder,
            chunk_height=self.chunk_height,
            lang=self.lang_code,
            log=_log
        )
        if not self.stop_flag.is_set() and results:
            _log(f"🎉 OCR finished: Merged text saved to 'combined_ocr_merged.txt'.", "green")
        elif not results:
            _log(f"❌ OCR failed or found no image chunks in '{os.path.basename(normalized_input)}'.", "red")
        self.folder_finished.emit(self.folder_name)

class RemoveTextWorker(NestedOutputWorker):
    def __init__(self, input_folder, output_base, chapter_name, **kwargs):
        super().__init__(input_folder, output_base, chapter_name, FOLDER_NAME_REMOVE_TEXT, **kwargs)
    def run(self):
        _log = lambda msg, color="gray": self._emit(msg, color)
        if not cv2 or not pytesseract:
            _log("❌ OpenCV or Tesseract not found. Skipping.", "red")
            self.folder_finished.emit(self.folder_name)
            return
        # Ensure input folder is normalized before getting file list
        normalized_input = os.path.normpath(self.input_folder)
        input_image_paths = [os.path.join(normalized_input, f) for f in list_image_files_for_combine(normalized_input)]
        if not input_image_paths:
            _log(f"⚠️ No combined images found in {os.path.basename(self.input_folder)}. Skipping.", "orange")
            self.folder_finished.emit(self.folder_name)
            return
        _log(f"🚀 Starting text removal on {len(input_image_paths)} image(s). Output: **{os.path.basename(self.output_folder)}**", "blue")
        results = remove_text_in_folder(input_image_paths, self.output_folder, _log)
        if not self.stop_flag.is_set() and results:
            _log(f"🎉 Text Removal finished: Saved {len(results)} cleaned image(s) to '{os.path.basename(self.output_folder)}'.", "green")
        elif not results:
            _log(f"❌ Remove Text failed or produced no output.", "red")
        self.folder_finished.emit(self.folder_name)

class CropWorker(NestedOutputWorker):
    def __init__(self, input_folder, output_base, chapter_name, **kwargs):
        super().__init__(input_folder, output_base, chapter_name, FOLDER_NAME_CROP, **kwargs)
    def run(self):
        _log = lambda msg, color="gray": self._emit(msg, color)
        if not cv2 or not np:
            _log("❌ OpenCV or NumPy is required for cropping. Skipping.", "red")
            self.folder_finished.emit(self.folder_name)
            return
        # Ensure input folder is normalized before checking for files
        normalized_input = os.path.normpath(self.input_folder)
        if not list_image_files_for_combine(normalized_input):
            _log(f"⚠️ No input files found in {os.path.basename(self.input_folder)}. Skipping.", "orange")
            self.folder_finished.emit(self.folder_name)
            return
        ensure_dir(self.output_folder)
        _log(f"🚀 Starting Panel Cropping. Output: **{os.path.basename(self.output_folder)}**", "blue")
        panel_count = _detect_and_save_panels(normalized_input, self.output_folder, _log)
        if not self.stop_flag.is_set() and panel_count > 0:
            _log(f"🎉 Cropping finished: Saved {panel_count} total panels to '{os.path.basename(self.output_folder)}'.", "green")
        elif panel_count == 0:
            _log(f"⚠️ Cropping finished, but no valid panels were saved.", "orange")
        self.folder_finished.emit(self.folder_name)


class PipelineSignals(QObject):
    log = Signal(str, str)
    chapter_finished = Signal(str, bool)

class PipelineWorker(QThread):

    DEFAULT_TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    DEFAULT_CHUNK_HEIGHT = GLOBAL_DEFAULT_CHUNK_HEIGHT
    DEFAULT_LANG_CODE = GLOBAL_DEFAULT_LANG_CODE

    PIPELINE_STEPS = {
        'Combine': CombineWorker, 'OCR': OcrWorker, 'RemoveText': RemoveTextWorker,
        'Crop': CropWorker
    }

    STEP_FOLDER_MAP = {
        'Combine': FOLDER_NAME_COMBINE, 'OCR': FOLDER_NAME_OCR,
        'RemoveText': FOLDER_NAME_REMOVE_TEXT, 'Crop': FOLDER_NAME_CROP,
    }

    def __init__(self, chapter_path, output_root, api_keys=None, steps_to_run=None, delete_map=None, collage_enabled=False, collage_cfg=None):
        super().__init__()
        # Ensure chapter_path and output_root are normalized for consistent path construction
        self.chapter_path = os.path.normpath(chapter_path)
        self.output_root = os.path.normpath(output_root)
        self.api_keys = api_keys or {}
        self.signals = PipelineSignals()
        self.stop_flag = threading.Event()
        self.folder_name = os.path.basename(chapter_path)
        self.steps_to_run = steps_to_run if steps_to_run is not None else list(self.PIPELINE_STEPS.keys())
        self.delete_map = delete_map if delete_map is not None else {}
        self.collage_enabled = collage_enabled
        self.collage_cfg = collage_cfg or {}


    def stop(self):
        self.stop_flag.set()

    def _emit(self, msg, color="black"):
        self.signals.log.emit(f"[{self.folder_name}] {msg}", color)

    def _check_output_exists(self, step_name):
        """Checks if the output folder for a given step already exists and contains files."""
        step_folder_name = self.STEP_FOLDER_MAP[step_name]
        output_path = os.path.join(self.output_root, self.folder_name, step_folder_name)

        if not os.path.isdir(output_path):
            return False, output_path

        if step_name == 'OCR':
            # Check for the specific combined OCR file
            if os.path.exists(os.path.join(output_path, "combined_ocr_merged.txt")):
                return True, output_path

        # For image steps (Combine, RemoveText, Crop), check for image files
        image_files = list_image_files_for_combine(output_path)
        if image_files:
            return True, output_path

        return False, output_path

    def _run_sub_worker(self, step_name, previous_output_path):

        # --- CRITICAL FIX 1: CHECK IF OUTPUT ALREADY EXISTS ---
        output_exists, intended_output_path = self._check_output_exists(step_name)
        if output_exists:
            self._emit(f"⏭️ Skipping **{step_name}**: Output folder already exists and contains files.", "darkgreen")
            return True, intended_output_path
        # -----------------------------------------------------

        WorkerClass = self.PIPELINE_STEPS.get(step_name)

        if not WorkerClass:
            self._emit(f"❌ Skipping {step_name}: Worker class missing/disabled.", "red")
            return True, None

        # Build input paths based on the step's dependency
        if step_name == 'Combine':
            input_folder = self.chapter_path
        elif step_name == 'OCR':
            input_folder = os.path.join(self.output_root, self.folder_name, self.STEP_FOLDER_MAP['Combine'])
        elif step_name == 'RemoveText':
            input_folder = os.path.join(self.output_root, self.folder_name, self.STEP_FOLDER_MAP['Combine'])
        elif step_name == 'Crop':
            # Use RemoveText output if it ran, otherwise fallback to Combine output
            remove_text_path = os.path.join(self.output_root, self.folder_name, self.STEP_FOLDER_MAP['RemoveText'])
            combine_path = os.path.join(self.output_root, self.folder_name, self.STEP_FOLDER_MAP['Combine'])

            # Use os.path.isdir for robust directory check
            if os.path.isdir(remove_text_path) and list_image_files_for_combine(remove_text_path):
                input_folder = remove_text_path
            else:
                input_folder = combine_path
        else:
            self._emit(f"❌ Internal Error: Unknown step name {step_name}", "red")
            return False, None

        # Normalize the input folder path for robust checking
        input_folder = os.path.normpath(input_folder)

        # Check if the required input is present (if not 'Combine', which uses chapter_path)
        if step_name != 'Combine' and (not os.path.isdir(input_folder) or not (list_image_files_for_combine(input_folder) or (step_name == 'OCR' and os.path.isdir(input_folder)))):
             # OCR only needs the directory to exist if Combine ran, the OCR worker will check for image files inside it.
             # But if the input folder is genuinely empty or missing, skip.
             if step_name == 'OCR' and not os.path.isdir(input_folder):
                 self._emit(f"⚠️ Skipping {step_name}: Required input folder missing: {os.path.basename(input_folder)}", "orange")
             elif step_name != 'OCR':
                 self._emit(f"⚠️ Skipping {step_name}: Required input folder missing or empty: {os.path.basename(input_folder)}", "orange")

             step_folder_name = self.STEP_FOLDER_MAP[step_name]
             # If skipped due to missing input, the intended output path remains the same as if it ran.
             return True, intended_output_path

        self._emit(f"➡️ Starting **{step_name}** (Input: {os.path.basename(input_folder)})", "blue")

        worker_args = {
            'input_folder': input_folder, 'output_base': self.output_root,
            'chapter_name': self.folder_name
        }

        if step_name == 'OCR':
            worker_args.update({
                'tesseract_cmd': self.DEFAULT_TESSERACT_PATH,
                'chunk_height': self.DEFAULT_CHUNK_HEIGHT,
                'lang_code': self.DEFAULT_LANG_CODE
            })

        worker = WorkerClass(**worker_args)
        worker.log.connect(self.signals.log.emit)
        worker.start()

        while worker.isRunning():
            if self.stop_flag.is_set():
                worker.stop()
            QThread.msleep(100)
            QApplication.processEvents()

        worker.wait()

        if self.stop_flag.is_set():
            self._emit(f"🛑 {step_name} cancelled.", "orange")
            return False, None

        # The intended_output_path was determined earlier.
        return True, intended_output_path

    def run(self):
        self._emit(f"🚀 Starting master pipeline for chapter: **{self.folder_name}**", "darkblue")
        overall_success = True
        previous_output_path = self.chapter_path

        try:
            for step_name in self.steps_to_run:
                if self.stop_flag.is_set():
                    overall_success = False; break

                # Note: previous_output_path is technically unused by _run_sub_worker
                # but kept for API compatibility. The worker determines its input internally.
                success, current_output_path = self._run_sub_worker(step_name, previous_output_path)

                if not success and not self.stop_flag.is_set():
                    overall_success = False
                    self._emit(f"❌ **{step_name}** FAILED. Stopping pipeline for this chapter.", "red"); break

                # The output path from a skipped step must be passed along, which _run_sub_worker now does.
                if success: previous_output_path = current_output_path


                # --- Post-Crop: split oversized crops (>=1.6MB) into 2–3 parts ---
                if success and step_name == 'Crop':
                    try:
                        crop_dir = os.path.join(self.output_root, self.folder_name, FOLDER_NAME_CROP)
                        changed, total = _split_large_crops_in_folder(crop_dir, logger=self._emit)
                        _ = _rename_files_sequential(crop_dir, log=lambda m, c="gray": self._emit(m))

                        # --- Rename crops sequentially after splitting ---
                        try:
                            count = _rename_files_sequential(crop_dir, log=lambda m, c="gray": self._emit(m))
                            self._emit(f"🔤 Renamed crops sequentially to maintain order (Crop1.png, Crop2.png...).", "blue")
                        except Exception as e:
                            self._emit(f"⚠️ Rename after split failed: {e}", "orange")

                        if changed > 0:
                            self._emit(f"🔪 Split {changed} oversized crop(s) into parts for better balance.", "blue")
                    except Exception as e:
                        self._emit(f"⚠️ Post-Crop split skipped: {e}", "orange")


                # >>> RUN COLLAGE RIGHT AFTER CROP <<<
                if success and step_name == 'Crop' and self.collage_enabled:
                    try:
                        crop_dir = os.path.join(self.output_root, self.folder_name, FOLDER_NAME_CROP)
                        collage_dir = os.path.join(self.output_root, self.folder_name, "Collage")
                        rows = int(self.collage_cfg.get('rows', 2))
                        cols = int(self.collage_cfg.get('cols', 5))
                        cell_w = int(self.collage_cfg.get('cell_w', 0))
                        pad = int(self.collage_cfg.get('pad', 10))
                        self._emit(f"▶ Collage: building {rows}×{cols} pages from '{os.path.basename(crop_dir)}' …", "blue")
                        pages = _build_collages_grid(crop_dir, collage_dir, rows, cols, cell_w, pad, logger=self._emit)
                        if pages:
                            self._emit(f"✅ Collage: {len(pages)} page(s) written to 'Collage'.", "green")
                        else:
                            self._emit("⚠️ Collage produced no pages.", "orange")
                    except Exception as e:
                        self._emit(f"❌ Collage failed: {e}", "red")


            if self.stop_flag.is_set():
                self._emit("🛑 Master pipeline cancelled by user.", "orange")
            elif overall_success:
                self._emit("🎉 Master pipeline finished successfully!", "green")

        except Exception as e:
            overall_success = False
            self._emit(f"❌ CRITICAL MASTER ERROR: {e}", "red")
            self._emit(traceback.format_exc(), "red")

        finally:
            # >>> RUN WASTE AFTER COLLAGE <<<
            try:
                crop_dir    = os.path.join(self.output_root, self.folder_name, FOLDER_NAME_CROP)
                combine_dir = os.path.join(self.output_root, self.folder_name, FOLDER_NAME_COMBINE)
                waste_dir   = os.path.join(self.output_root, self.folder_name, "Waste")

                crop_files   = list_image_files_for_combine(crop_dir)
                n_strips     = len(crop_files)
                combine_files = list_image_files_for_combine(combine_dir)

                # --- ALWAYS create / clean Waste folder ---
                if os.path.isdir(waste_dir):
                    shutil.rmtree(waste_dir, ignore_errors=True)
                os.makedirs(waste_dir, exist_ok=True)

                if n_strips == 0:
                    self._emit("⚠️ Waste: No crops found. Waste folder created but nothing sliced.", "orange")
                elif not combine_files:
                    self._emit("⚠️ Waste: No combined image found. Waste folder created but nothing sliced.", "orange")
                else:
                    img_path = os.path.join(combine_dir, combine_files[0])
                    slice_vertical = False
                    with Image.open(img_path) as im:
                        w, h = im.size
                        if slice_vertical:
                            slice_w = w // n_strips
                            for i in range(n_strips):
                                left  = i * slice_w
                                right = (i + 1) * slice_w if i < n_strips - 1 else w
                                im.crop((left, 0, right, h)).save(
                                    os.path.join(waste_dir, f"Crop{i+1}.png"), "PNG")
                        else:
                            slice_h = h // n_strips
                            for i in range(n_strips):
                                top    = i * slice_h
                                bottom = (i + 1) * slice_h if i < n_strips - 1 else h
                                im.crop((0, top, w, bottom)).save(
                                    os.path.join(waste_dir, f"Crop{i+1}.png"), "PNG")

                    self._emit(f"🔪 Waste: sliced into {n_strips} parts → Waste folder.", "blue")

            except Exception as e:
                self._emit(f"❌ Waste failed: {e}", "red")
                overall_success = False      # <-- let scheduler know

            # old cleanup (optional) – keep only if you still want it
            if overall_success and not self.stop_flag.is_set():
                chapter_base_path = os.path.join(self.output_root, self.folder_name)
                cleanup_keys = ['Combine', 'OCR', 'RemoveText']
                for key in cleanup_keys:
                    if self.delete_map.get(key):
                        folder_name = self.STEP_FOLDER_MAP[key]
                        full_path = os.path.join(chapter_base_path, folder_name)
                        if os.path.isdir(full_path):
                            try:
                                shutil.rmtree(full_path)
                                self._emit(f"🗑️ Deleted intermediate folder: {folder_name}", "darkgreen")
                            except Exception as e:
                                self._emit(f"⚠️ Failed to delete folder {folder_name}: {e}", "red")
                                overall_success = False

            # tell scheduler we are done (success or failure)
            self.signals.chapter_finished.emit(self.folder_name, overall_success)


class MergeAllTab(QWidget):
    operation_finished = Signal()

    def __init__(self):
        super().__init__()
        self.workers = []
        self.active_worker = None
        self.total_chapters = 0
        self._all_input_paths = []
        self._launch_index = 0
        self._output_base = ""
        self.natsorted = natsorted
        self._setup_ui()

    def is_auto_run_checked(self):
        """Returns the state of the auto-run checkbox."""
        return self.cb_auto_run_sequence.isChecked()

    @Slot()
    def start_task(self):
        """Standardized method for MainWindow to trigger the operation."""
        # This will be the entry point for the full batch run
        self._on_start_pipeline()

    # --- NEW METHOD: Starts the pipeline for a single, specified chapter ---
    def start_single_chapter_task(self, chapter_path):
        """
        Custom method to start the pipeline ONLY for the given chapter_path,
        used by the PipelineOrchestrator in Main.py.
        """
        output_base = self.out_base_line.text().strip()
        chapter_name = os.path.basename(chapter_path)

        self.n_log.clear() # Clear log for new chapter

        selected_steps = []
        if self.cb_combine.isChecked(): selected_steps.append('Combine')
        if self.cb_ocr.isChecked(): selected_steps.append('OCR')
        if self.cb_remove_text.isChecked(): selected_steps.append('RemoveText')
        if self.cb_crop.isChecked(): selected_steps.append('Crop')

        if not selected_steps:
            self._append_log("❌ Error: No pipeline steps selected.", "red"); return

        delete_map = {
            'Combine': self.cb_delete_combine.isChecked(),
            'RemoveText': self.cb_delete_remove_text.isChecked(),
        }

        if not output_base:
            self._append_log("⚠️ Error: Please enter a Pipeline Output Root Folder.", "red"); return

        try:
            normalized_output_base = os.path.normpath(output_base)
            os.makedirs(normalized_output_base, exist_ok=True)
            self._output_base = normalized_output_base
        except Exception as e:
            self._append_log(f"❌ Critical Error: Could not create output directory '{output_base}': {e}", "red"); return

        self._set_ui_state(True)
        self.workers = []
        self.total_chapters = 1 # Set to 1 for single run progress display
        self._launch_index = 0 # Not used for single run, but kept for state consistency
        self.progress.setRange(0, 1); self.progress.setValue(0)

        step_names_str = " -> ".join(selected_steps)
        self._append_log(f"🚀 Starting SINGLE chapter pipeline for **{chapter_name}**. Steps: **{step_names_str}**", "darkblue")

        # Create and start the worker directly for the single chapter
        api_keys = {} # API keys are not used here
        collage_cfg = {
            'rows': self.spin_collage_rows.value(),
            'cols': self.spin_collage_cols.value(),
            'cell_w': self.spin_collage_cellw.value(),
            'pad': self.spin_collage_pad.value(),
        }
        worker = PipelineWorker(
            chapter_path, normalized_output_base, api_keys,
            steps_to_run=selected_steps, delete_map=delete_map,
            collage_enabled=self.cb_collage.isChecked(), collage_cfg=collage_cfg
        )

        worker.signals.log.connect(self._append_log)
        worker.signals.chapter_finished.connect(self._on_single_chapter_finished)
        self.active_worker = worker
        self.workers.append(worker)
        worker.start()

    # --- NEW SLOT: Handles the finish of the single chapter and signals the orchestrator ---
    @Slot(str, bool)
    def _on_single_chapter_finished(self, folder_name, success):
        self.progress.setValue(self.progress.value() + 1)
        self.active_worker = None
        self._append_log(f"✅ Chapter **'{folder_name}'** pipeline COMPLETED." if success else f"❌ Chapter **'{folder_name}'** pipeline FAILED/STOPPED.", "green" if success else "red")

        # Signal the orchestrator (in Main.py) that this step is done
        if success and self.is_auto_run_checked():
            self.operation_finished.emit() # This signal drives the Orchestrator loop

        self._set_ui_state(False)
        self.workers = []
        self.total_chapters = 0

    @Slot()
    def _on_batch_finished(self, final_batch_success):
        self.progress.setFormat("Batch Progress: %p% (%v / %m chapters)")
        if self.total_chapters > 0: self.progress.setValue(self.total_chapters)

        self._append_log("🎉 Master batch process COMPLETED.", "darkgreen")

        # NOTE: The original auto-run sequence logic is now handled by the Orchestrator
        # that calls start_single_chapter_task(). We keep the original emit here in case
        # the user runs the "full batch" mode, but the new orchestrator won't use it.
        if final_batch_success and self.cb_auto_run_sequence.isChecked():
            self._append_log("⚙️ Merge All batch success. Emitting signal to start next step (TTS/Video)...", "darkblue")
            # We don't emit operation_finished here anymore, as the orchestrator handles the full flow.
            # We will rely on the orchestrator's start_batch_loop being called by the main start button.
            pass

        self.workers = []; self.active_worker = None; self.total_chapters = 0
        self._launch_index = 0; self._set_ui_state(False)

    def _browse_root_folder(self):
        root_dir = QFileDialog.getExistingDirectory(self, "Select the ROOT Folder Containing ALL Chapters")
        if root_dir: self.root_input_line.setText(root_dir)

    def _browse_output_folder(self):
        out_dir = QFileDialog.getExistingDirectory(self, "Select the Output Root Folder")
        if out_dir: self.out_base_line.setText(out_dir)

    def _append_log(self, text, color="black"):
        color_map = {
            "darkblue": "#007ACC", "red": "#CC0000", "orange": "#FF9900",
            "green": "#4CAF50", "darkgreen": "#006600", "blue": "#3498DB",
            "gray": "#AAAAAA"
        }
        html_color = color_map.get(color.lower(), color.lower())
        self.n_log.append(f'<span style="color:{html_color};">{text}</span>')

    def _get_subfolders_with_images(self, root_dir):
        chapter_paths = []
        image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff')
        if not os.path.isdir(root_dir): return []
        for item in self.natsorted(os.listdir(root_dir)):
            full_path = os.path.join(root_dir, item)
            if os.path.isdir(full_path):
                if any(f.lower().endswith(image_exts) for f in os.listdir(full_path)):
                    chapter_paths.append(full_path)
        return chapter_paths

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 1. Root & Output Selection
        g_io = QGroupBox("1. Batch Folders & Output")
        v_io = QVBoxLayout(g_io)
        v_io.addWidget(QLabel("Chapter Root Folder (Input for Combine):"))
        h_root = QHBoxLayout()
        # --- MODIFICATION: Used ContextMenuLineEdit ---
        self.root_input_line = ContextMenuLineEdit()
        btn_nr = QPushButton("Browse Root Folder (chapters)")
        btn_nr.clicked.connect(self._browse_root_folder)
        h_root.addWidget(self.root_input_line); h_root.addWidget(btn_nr)
        v_io.addLayout(h_root)
        v_io.addWidget(QLabel("Pipeline Output Root Folder:"))
        h_out = QHBoxLayout()
        # --- MODIFICATION: Used ContextMenuLineEdit ---
        self.out_base_line = ContextMenuLineEdit()
        btn_no = QPushButton("Select Output Root")
        btn_no.clicked.connect(self._browse_output_folder)
        h_out.addWidget(self.out_base_line); h_out.addWidget(btn_no)
        v_io.addLayout(h_out)
        layout.addWidget(g_io)

        # 2. Pipeline Step Selection
        g_steps = QGroupBox("2. Pipeline Steps")
        h_steps = QHBoxLayout(g_steps)
        self.cb_combine = QCheckBox("📦 Combine"); self.cb_ocr = QCheckBox("🔍 OCR")
        self.cb_remove_text = QCheckBox("🧼 Remove Text"); self.cb_crop = QCheckBox("✂️ Crop"); self.cb_collage = QCheckBox("🧩 Collage")
        self.cb_combine.setChecked(True); self.cb_ocr.setChecked(False); self.cb_remove_text.setChecked(False)
        self.cb_crop.setChecked(True)
        self.cb_collage.setChecked(True)

        h_steps.addWidget(self.cb_combine); h_steps.addWidget(self.cb_ocr); h_steps.addWidget(self.cb_remove_text)
        h_steps.addWidget(self.cb_crop); h_steps.addStretch(1); h_steps.addWidget(self.cb_collage)
        layout.addWidget(g_steps)
        # --- Collage options (rows / cols / cell width / pad) ---
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Rows"))
        self.spin_collage_rows = QSpinBox(); self.spin_collage_rows.setRange(1, 10); self.spin_collage_rows.setValue(2)
        opts.addWidget(self.spin_collage_rows)

        opts.addWidget(QLabel("Cols"))
        self.spin_collage_cols = QSpinBox(); self.spin_collage_cols.setRange(1, 10); self.spin_collage_cols.setValue(5)
        opts.addWidget(self.spin_collage_cols)

        opts.addWidget(QLabel("Cell W"))
        self.spin_collage_cellw = QSpinBox(); self.spin_collage_cellw.setRange(0, 4096); self.spin_collage_cellw.setValue(0)  # 0 = auto (use first crop width)
        opts.addWidget(self.spin_collage_cellw)

        opts.addWidget(QLabel("Pad"))
        self.spin_collage_pad = QSpinBox(); self.spin_collage_pad.setRange(0, 200); self.spin_collage_pad.setValue(10)
        opts.addWidget(self.spin_collage_pad)

        h_steps.addLayout(opts)



        # 3. Post-Processing / Cleanup
        g_cleanup = QGroupBox("3. Cleanup: Delete Intermediate Folders on Success")
        v_cleanup = QVBoxLayout(g_cleanup)
        h_cleanup_folders = QHBoxLayout()
        global FOLDER_NAME_COMBINE, FOLDER_NAME_OCR, FOLDER_NAME_REMOVE_TEXT
        self.cb_delete_combine = QCheckBox(f"Delete '{FOLDER_NAME_COMBINE}'")
        self.cb_delete_remove_text = QCheckBox(f"Delete '{FOLDER_NAME_REMOVE_TEXT}'")

        # --- START MODIFICATION FOR CLEANUP CHECKBOXES ---
        self.cb_delete_combine.setChecked(True)
        self.cb_delete_remove_text.setChecked(True)
        # --- END MODIFICATION FOR CLEANUP CHECKBOXES ---

        h_cleanup_folders.addWidget(self.cb_delete_combine); h_cleanup_folders.addWidget(self.cb_delete_remove_text)
        h_cleanup_folders.addStretch(1); v_cleanup.addLayout(h_cleanup_folders); layout.addWidget(g_cleanup)

        # 4. Control & Progress
        h_controls = QHBoxLayout()
        # --- IMPORTANT CHANGE: This checkbox now controls the Orchestrator ---
        self.cb_auto_run_sequence = QCheckBox("Auto-Run Sequential Chapter Loop")

        # --- START MODIFICATION FOR AUTO-RUN CHECKBOX ---
        self.cb_auto_run_sequence.setChecked(False)
        # --- END MODIFICATION FOR AUTO-RUN CHECKBOX ---

        h_controls.addWidget(self.cb_auto_run_sequence); h_controls.addStretch(1)
        self.btn_night_start = QPushButton("▶️ Start")
        self.btn_night_stop = QPushButton("🛑 Stop All")
        self.btn_night_stop.setEnabled(False)
        self.btn_night_start.clicked.connect(self._on_start_pipeline)
        self.btn_night_stop.clicked.connect(self._on_stop_all)
        h_controls.addWidget(self.btn_night_start); h_controls.addWidget(self.btn_night_stop)
        layout.addLayout(h_controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100); self.progress.setValue(0)
        self.progress.setFormat("Batch Progress: %p% (%v / %m chapters)")
        layout.addWidget(self.progress)

        # 5. Log
        self.n_log = QTextEdit(); self.n_log.setReadOnly(True); self.n_log.setFixedHeight(250)
        layout.addWidget(QLabel("Master Process Log:"))
        layout.addWidget(self.n_log)
        layout.addStretch()

    def _set_ui_state(self, is_running):
        self.btn_night_start.setEnabled(not is_running); self.btn_night_stop.setEnabled(is_running)
        self.root_input_line.setEnabled(not is_running); self.out_base_line.setEnabled(not is_running)
        self.cb_auto_run_sequence.setEnabled(not is_running)
        self.cb_combine.setEnabled(not is_running); self.cb_ocr.setEnabled(not is_running)
        self.cb_remove_text.setEnabled(not is_running); self.cb_crop.setEnabled(not is_running)
        self.cb_delete_combine.setEnabled(not is_running)
        self.cb_delete_remove_text.setEnabled(not is_running)

    def _launch_next_worker(self, steps_to_run, delete_map):
        if self._launch_index < self.total_chapters:
            chapter_path = self._all_input_paths[self._launch_index]
            api_keys = {}
            collage_cfg = {
                'rows': self.spin_collage_rows.value(),
                'cols': self.spin_collage_cols.value(),
                'cell_w': self.spin_collage_cellw.value(),
                'pad': self.spin_collage_pad.value(),
            }
            worker = PipelineWorker(
                chapter_path, self._output_base, api_keys,
                steps_to_run=steps_to_run, delete_map=delete_map,
                collage_enabled=self.cb_collage.isChecked(), collage_cfg=collage_cfg
            )

            worker.signals.log.connect(self._append_log)
            worker.signals.chapter_finished.connect(self._on_chapter_finished)
            self.active_worker = worker
            self.workers.append(worker)
            worker.start()
            self._launch_index += 1
            return True
        return False

    # --- MODIFIED: This is now the entry point for the full batch run only ---
    def _on_start_pipeline(self):

            # 1. Get the main application window (which holds the orchestrator)
            main_window = None
            for w in QApplication.instance().topLevelWidgets():
                if w.windowTitle() == "Manhwa Pipeline Tool" and hasattr(w, 'orchestrator'):
                    main_window = w
                    break

            # 2. If the sequential loop is checked AND the orchestrator is found, delegate
            if self.cb_auto_run_sequence.isChecked() and main_window:
                self._set_ui_state(True) # Lock UI
                self._append_log("⚙️ Auto-Run checked. Initiating **Sequential Chapter Loop** (Merge -> Compose for Chapter 1).", "darkblue")
                main_window.orchestrator.start_batch_loop()
            else:
                # 3. Fallback to Legacy Full Batch if unchecked or orchestrator is missing
                self._append_log("⚙️ Running **Legacy Full Batch Merge** (All Chapters Merge First).", "darkblue")
                self._run_legacy_full_batch()

    # --- NEW: Logic to run the original full batch mode ---
    def _run_legacy_full_batch(self):
        root_dir = self.root_input_line.text().strip(); output_base = self.out_base_line.text().strip()
        self.n_log.clear()
        selected_steps = []
        if self.cb_combine.isChecked(): selected_steps.append('Combine')
        if self.cb_ocr.isChecked(): selected_steps.append('OCR')
        if self.cb_remove_text.isChecked(): selected_steps.append('RemoveText')
        if self.cb_crop.isChecked(): selected_steps.append('Crop')
        if not selected_steps:
            self._append_log("❌ Error: No pipeline steps selected.", "red"); return

        delete_map = {
            'Combine': self.cb_delete_combine.isChecked(),
            'RemoveText': self.cb_delete_remove_text.isChecked(),
        }

        if not os.path.isdir(root_dir):
            self._append_log("⚠️ Error: Invalid or non-existent Chapter Root Folder.", "red"); return
        if not output_base:
            self._append_log("⚠️ Error: Please enter a Pipeline Output Root Folder.", "red"); return

        try:
            normalized_output_base = os.path.normpath(output_base)
            os.makedirs(normalized_output_base, exist_ok=True)
            self._output_base = normalized_output_base
            self._append_log(f"✅ Output Base Path accepted: {self._output_base}", "blue")
        except Exception as e:
            self._append_log(f"❌ Critical Error: Could not create output directory '{output_base}': {e}", "red"); return

        self._all_input_paths = self._get_subfolders_with_images(root_dir)
        if not self._all_input_paths:
            self._append_log("⚠️ Error: No image-containing chapter subfolders found.", "red"); return

        self._set_ui_state(True); self.workers = []; self.active_worker = None
        self.total_chapters = len(self._all_input_paths); self._launch_index = 0
        self.progress.setRange(0, self.total_chapters); self.progress.setValue(0)

        step_names_str = " -> ".join(selected_steps)
        self._append_log(f"🚀 Starting legacy FULL BATCH pipeline for {self.total_chapters} chapters. Steps: **{step_names_str}**", "darkblue")
        if any(delete_map.values()):
            self._append_log("⚠️ **WARNING**: Cleanup is active. Selected intermediate folders will be deleted upon success.", "orange")

        self._launch_next_worker(steps_to_run=selected_steps, delete_map=delete_map)

        def _build_collages_grid(crop_dir, out_dir, rows, cols, cell_w, pad, logger=print):
            """
            Make paged collages: each page lays out (rows x cols) crops in a grid.
            - If cell_w == 0, uses the first crop's width.
            - Heights are proportional per image; each row is centered using that row's max height.
            """
            os.makedirs(out_dir, exist_ok=True)
            exts = ('.png','.jpg','.jpeg','.webp','.bmp','.tiff')
            files = [f for f in os.listdir(crop_dir) if f.lower().endswith(exts)]
            if not files:
                logger("⚠️ Collage: no crops found."); return []

            try:
                files = natsorted(files)
            except Exception:
                files.sort(key=lambda s: [int(t) if t.isdigit() else t.lower()
                                        for t in re.split(r'(\d+)', str(s))])

            page_size = max(1, rows*cols)
            pages = [files[i:i+page_size] for i in range(0, len(files), page_size)]
            out_paths = []

            # Determine default cell width if auto
            if cell_w <= 0:
                with Image.open(os.path.join(crop_dir, pages[0][0])) as im0:
                    cell_w = im0.size[0]

            for p_idx, page_files in enumerate(pages, start=1):
                # Preload scaled heights for the page at width = cell_w
                scaled_sizes = []
                for name in page_files:
                    try:
                        with Image.open(os.path.join(crop_dir, name)) as im:
                            w, h = im.size
                        scale = cell_w / max(1, w)
                        scaled_sizes.append((name, cell_w, max(1, int(round(h*scale)))))
                    except Exception as e:
                        logger(f"❌ Collage: failed to read {name}: {e}")

                if not scaled_sizes:
                    continue

                # Compute per-row max heights
                row_heights = []
                for r in range(rows):
                    row_items = scaled_sizes[r*cols:(r+1)*cols]
                    if not row_items:
                        row_heights.append(0)
                    else:
                        row_heights.append(max(hh for _, _, hh in row_items))

                canvas_w = pad + (cols*cell_w) + ((cols-1)*pad) + pad
                canvas_h = pad + sum(row_heights) + (max(0, rows-1)*pad) + pad
                canvas = Image.new("RGB", (canvas_w, canvas_h), (0,0,0))

                y = pad
                for r in range(rows):
                    row_items = scaled_sizes[r*cols:(r+1)*cols]
                    if not row_items:
                        continue
                    x = pad
                    row_h = row_heights[r]
                    for name, w_scaled, h_scaled in row_items:
                        try:
                            with Image.open(os.path.join(crop_dir, name)) as im:
                                im = im.resize((w_scaled, h_scaled), Image.LANCZOS).convert("RGB")
                            y_off = y + (row_h - h_scaled)//2
                            canvas.paste(im, (x, y_off))
                        except Exception as e:
                            logger(f"❌ Collage: failed placing {name}: {e}")
                        x += w_scaled + pad
                    y += row_h + pad

                out_name = f"Collage_{p_idx}.png"
                out_path = os.path.join(out_dir, out_name)
                try:
                    canvas.save(out_path, "PNG", optimize=True, compress_level=9)
                    out_paths.append(out_path)
                except Exception as e:
                    logger(f"❌ Collage: failed saving {out_name}: {e}")

            logger(f"🧩 Collage: created {len(out_paths)} page(s).")
            return out_paths


    def _on_chapter_finished(self, folder_name, success):
        self.progress.setValue(self.progress.value() + 1); self.active_worker = None
        self._append_log(f"✅ Chapter **'{folder_name}'** pipeline COMPLETED." if success else f"❌ Chapter **'{folder_name}'** pipeline FAILED/STOPPED.", "green" if success else "red")

        if self._launch_index < self.total_chapters:
            self._append_log(f"▶️ Launching next chapter: {os.path.basename(self._all_input_paths[self._launch_index])}", "gray")
            last_worker = self.workers[-1]
            self._launch_next_worker(steps_to_run=last_worker.steps_to_run, delete_map=last_worker.delete_map)
        else:
            self._on_batch_finished(success)

    def _on_stop_all(self):
        if self.active_worker:
            self._append_log("🛑 Requesting current chapter job to stop...", "orange")
            self.active_worker.stop()
            self._launch_index = self.total_chapters
            self.active_worker = None
            self._set_ui_state(False)

    def cleanup(self):
        """
        FIXED: Robust cleanup for the MergeAllTab to ensure the active worker is
        stopped and given time to exit before destruction.
        """
        if self.active_worker and self.active_worker.isRunning():
            self.active_worker.stop()

            # Wait gracefully (e.g., 2000ms)
            if not self.active_worker.wait(2000):
                self.active_worker.terminate()
                self.active_worker.wait() # Wait for termination

        # Clean up all worker references
        self.active_worker = None
        self.workers = []


def postprocess_chunks_and_write_combined(extracttext_chunk_dir: str, log):
    """
    Applies 3 conditions per chunk (>=20 keep, 1-19 expand, 0 create) using image+neighbors,
    overwrites each n.txt, then concatenates 1..N into 'combined_ocr_merged.txt' in chapter root.
    """
    import os, re, glob
    if not os.path.isdir(extracttext_chunk_dir):
        log(f"[OCR] chunk dir missing: {extracttext_chunk_dir}")
        return None

    # list all n.png sorted by integer
    pngs = []
    for p in glob.glob(os.path.join(extracttext_chunk_dir, "*.png")):
        name = os.path.splitext(os.path.basename(p))[0]
        if name.isdigit():
            pngs.append((int(name), p))
    pngs.sort(key=lambda x: x[0])
    if not pngs:
        log("[OCR] no chunk pngs found")
        return None

    # helper to read txt by id
    def read_txt_by_id(n):
        fp = os.path.join(extracttext_chunk_dir, f"{n}.txt")
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except:
            return ""

    # pass 1: ensure every chunk has final text according to rules
    total = len(pngs)
    for idx, (n, png_path) in enumerate(pngs):
        prev_text = read_txt_by_id(n-1) if n>1 else ""
        next_text = read_txt_by_id(n+1) if n< pngs[-1][0] else ""
        curr_text = read_txt_by_id(n)

        wc = _count_words_simple(curr_text)
        if wc >= 20:
            final = curr_text
        elif wc == 0:
            final = _gemini_generate_line("create", png_path, prev_text, curr_text, next_text)
        else:
            final = _gemini_generate_line("expand", png_path, prev_text, curr_text, next_text)

        try:
            with open(os.path.join(extracttext_chunk_dir, f"{n}.txt"), "w", encoding="utf-8") as f:
                f.write(final)
        except Exception as e:
            log(f"[OCR] write chunk {n}.txt failed: {e}")

    # pass 2: build combined_ocr_merged.txt in chapter root
    # collect in numeric order again after updates
    lines = []
    for n,_ in pngs:
        t = read_txt_by_id(n)
        if t:
            lines.append(t)

    chapter_dir = _get_chapter_dir_from_any_path(extracttext_chunk_dir)
    out_path = os.path.join(chapter_dir, "combined_ocr_merged.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(lines))
        log(f"[OCR] combined_ocr_merged.txt written: {out_path}")
        return out_path
    except Exception as e:
        log(f"[OCR] failed to write combined_ocr_merged.txt: {e}")
        return None
