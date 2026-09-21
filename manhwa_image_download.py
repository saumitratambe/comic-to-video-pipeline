# use pip install requests beautifulsoup4 lxml natsort

from __future__ import annotations
import os, re, sys, threading, time, queue, random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from natsort import natsorted
except Exception:
    natsorted = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "🕷️ Manhwa Saver"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LIGHT_BG       = "#ffffff"  # main background
LIGHT_PANEL    = "#f7f7fb"  # panel/cards
TEXT_PRIMARY   = "#0b1220"  # dark text
TEXT_SECONDARY = "#475569"  # secondary text
BORDER_COLOR   = "#d6d9e0"  # borders
WEB_GRAY       = "#e6e9f0"  # faint 'web' lines

SPIDEY_RED     = "#e11d48"  # primary action
SPIDEY_RED_DK  = "#be123c"
SPIDEY_BLUE    = "#1d4ed8"  # accents
SPIDEY_BLUE_LT = "#3b82f6"
SPIDEY_GOLD    = "#f59e0b"  # highlight for progress / focus
SUCCESS_GREEN  = "#16a34a"
WARN_AMBER     = "#f59e0b"
ERROR_RED      = "#dc2626"

# ---------------------------- HELPERS ----------------------------
def natural_sort(items):
    if natsorted:
        return natsorted(items)
    def keyfun(s):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]
    return sorted(items, key=keyfun)

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def guess_ext_from_ct(ct: str) -> str:
    if not ct:
        return ""
    if "/" in ct:
        ext = ct.split("/", 1)[1].split(";")[0].lower()
        if ext in {"jpeg","pjpeg"}: return ".jpg"
        if ext in {"png"}: return ".png"
        if ext in {"webp"}: return ".webp"
        if ext in {"gif"}: return ".gif"
    return ""

def pick_from_srcset(srcset: str) -> Optional[str]:
    try:
        parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
        return parts[-1] if parts else None
    except Exception:
        return None

def extract_image_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    containers = soup.select(".reader, .reading-content, .chapter-content, article, main, #content")
    scope = containers[0] if containers else soup.select_one("#readerarea") or soup
    urls = []
    for img in scope.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src") or img.get("src")
        if not src and img.get("srcset"):
            src = pick_from_srcset(img.get("srcset"))
        if not src:
            continue
        src = src.strip()
        if any(ph in src for ph in ("data:image/", "placeholder", "blank.")):
            continue
        urls.append(urljoin(base_url, src))
    # de-dup + natural sort
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            out.append(u); seen.add(u)
    return natural_sort(out)

def robots_allows(url: str) -> Tuple[bool, Optional[str]]:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        robots_url = f"{u.scheme}://{u.netloc}/robots.txt"
        r = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code != 200:
            return True, None
        blocks = re.findall(r"User-agent:\s*\*.*?(?=User-agent:|\Z)", r.text, flags=re.I|re.S)
        for blk in blocks:
            if re.search(r"^\s*Disallow:\s*/\s*$", blk, flags=re.I|re.M):
                return False, robots_url
        return True, None
    except Exception:
        return True, None

# ---------------------------- DATA ----------------------------
@dataclass
class DownloadTask:
    chapter_index: int
    url: str

@dataclass
class Settings:
    out_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), "Manhwa"))
    start_chapter: int = 1
    threads_per_chapter: int = 6
    request_timeout: int = 25
    max_retries: int = 4
    min_delay_ms: int = 80
    max_delay_ms: int = 180
    respect_robots: bool = False
    user_agent: str = USER_AGENT

class ChapterDownloader:
    def __init__(self, s: Settings, logfun, stop_event: threading.Event):
        self.s = s
        self.log = logfun
        self.stop = stop_event
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.s.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

    def backoff(self, attempt: int):
        time.sleep(min(1.5 ** attempt + random.random(), 10))

    def _download_one(self, url: str, dest: str) -> bool:
        tmp = dest + ".part"
        for attempt in range(self.s.max_retries + 1):
            if self.stop.is_set():
                return False
            try:
                with self.session.get(url, stream=True, timeout=self.s.request_timeout) as r:
                    st = r.status_code
                    if st >= 500:
                        self.log(f"  {st} — server trouble, retrying 🕸️")
                        self.backoff(attempt); continue
                    if st != 200:
                        self.log(f"  HTTP {st}: {url}")
                        return False
                    ctype = r.headers.get("Content-Type", "")
                    if not ctype.startswith("image/"):
                        self.log(f"  Skipped (not image): {url} [{ctype}]")
                        return False
                    ensure_dir(os.path.dirname(dest))
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 15):
                            if self.stop.is_set():
                                return False
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp, dest)
                    return True
            except requests.RequestException as e:
                self.log(f"  NetErr {type(e).__name__}: {e} — crawling back up the wall…")
                self.backoff(attempt)
            except Exception as e:
                self.log(f"  Unexpected: {e}")
                self.backoff(attempt)
        return False

    def download_chapter(self, chapter_folder: str, page_url: str) -> Tuple[int, int]:
        allow, robots_url = robots_allows(page_url)
        if not allow and self.s.respect_robots:
            self.log(f"[WARN] robots.txt disallows '/': {robots_url}")
        try:
            r = self.session.get(page_url, timeout=self.s.request_timeout)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            self.log(f"[ERR] Fetch fail: {page_url} -> {e}")
            return 0, 0

        imgs = extract_image_urls(html, page_url)
        if not imgs:
            self.log(f"[WARN] No images found at: {page_url}")
            return 0, 0

        total, ok = len(imgs), 0
        self.log(f"  Found {total} images 🕸️")
        q = queue.Queue()
        for idx, u in enumerate(imgs, start=1):
            q.put((idx, u))

        def worker():
            nonlocal ok
            while not q.empty() and not self.stop.is_set():
                idx, u = q.get()
                try:
                    base = f"{idx:03d}"
                    url_ext = os.path.splitext(urlparse(u).path)[1].lower()
                    ext = url_ext if (url_ext and len(url_ext) <= 5) else ".jpg"
                    dest = os.path.join(chapter_folder, base + ext)
                    if os.path.exists(dest):
                        self.log(f"  ⏭️ Already saved: {os.path.basename(dest)}")
                        q.task_done(); continue
                    success = self._download_one(u, dest)
                    if not success:
                        try:
                            hr = self.session.head(u, timeout=10, allow_redirects=True)
                            ext2 = guess_ext_from_ct(hr.headers.get("Content-Type","")) or ext
                            dest2 = os.path.join(chapter_folder, base + ext2)
                            if not os.path.exists(dest2):
                                success = self._download_one(u, dest2)
                        except Exception:
                            pass
                    if success:
                        ok += 1
                    time.sleep(random.uniform(self.s.min_delay_ms/1000.0, self.s.max_delay_ms/1000.0))
                finally:
                    q.task_done()

        ensure_dir(chapter_folder)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, self.s.threads_per_chapter))]
        for t in threads: t.start()
        q.join()
        return ok, total

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x780")
        self.minsize(960, 660)

        self.settings = Settings()
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None

        self._apply_light_spidey_theme()
        self._build_ui()

    # ----- Theme -----
    def _apply_light_spidey_theme(self):
        self.configure(bg=LIGHT_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Base
        style.configure(".", background=LIGHT_BG, foreground=TEXT_PRIMARY, fieldbackground=LIGHT_PANEL)

        # Labels & Frames
        style.configure("TLabel", background=LIGHT_BG, foreground=TEXT_PRIMARY)
        style.configure("TLabelframe", background=LIGHT_BG, bordercolor=BORDER_COLOR)
        style.configure("TLabelframe.Label", background=LIGHT_BG, foreground=SPIDEY_BLUE)

        # Entry/Spinbox/Text
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER_COLOR)
        style.configure("TSpinbox", fieldbackground="#ffffff", arrowsize=14)

        # Buttons
        style.configure("Accent.TButton",
                        background=SPIDEY_RED,
                        foreground="#ffffff",
                        padding=10,
                        relief="flat")
        style.map("Accent.TButton",
                  background=[("active", SPIDEY_RED_DK), ("!disabled", SPIDEY_RED)],
                  foreground=[("disabled", "#ffffff")])

        style.configure("Blue.TButton",
                        background=SPIDEY_BLUE,
                        foreground="#ffffff",
                        padding=9,
                        relief="flat")
        style.map("Blue.TButton",
                  background=[("active", SPIDEY_BLUE_LT), ("!disabled", SPIDEY_BLUE)],
                  foreground=[("disabled", "#ffffff")])

        # Progressbar
        style.configure("Horizontal.TProgressbar",
                        troughcolor=WEB_GRAY,
                        background=SPIDEY_GOLD,
                        bordercolor=BORDER_COLOR)

    # subtle web-mesh background using Canvas lines
    def _draw_web(self, canvas: tk.Canvas):
        w = canvas.winfo_width() or 1000
        h = canvas.winfo_height() or 120
        canvas.delete("web")
        step = 24
        # horizontal lines
        for y in range(0, h, step):
            canvas.create_line(0, y, w, y, fill=WEB_GRAY, tags="web")
        # diagonal lines
        for x in range(0, w, step):
            canvas.create_line(x, 0, x-80, h, fill=WEB_GRAY, tags="web")
        
        ring_gap = 60
        for r in range(ring_gap, max(w, h), ring_gap):
            canvas.create_oval(-r, -r, r*1.4, r*1.2, outline=WEB_GRAY, width=1, tags="web")

    def _build_ui(self):
        # Header with web mesh
        header = tk.Frame(self, bg=LIGHT_PANEL, highlightbackground=BORDER_COLOR, highlightthickness=1)
        header.pack(fill=tk.X, padx=10, pady=(12, 6))
        canvas = tk.Canvas(header, height=120, bg=LIGHT_PANEL, highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        canvas.bind("<Configure>", lambda e: self._draw_web(canvas))

        # Header content (on top of canvas)
        title = tk.Label(canvas,
                         text="🕸️ MANHWA SAVER",
                         bg=LIGHT_PANEL, fg=SPIDEY_BLUE,
                         font=("Segoe UI", 18, "bold"))
        subtitle = tk.Label(canvas,
                            text="Paste your chapter links (LAST → FIRST processing). Concurrency • Retries • Skip-if-Exists",
                            bg=LIGHT_PANEL, fg=TEXT_SECONDARY,
                            font=("Segoe UI", 10))
        # place near left
        canvas.create_window(24, 28, anchor="nw", window=title)
        canvas.create_window(24, 62, anchor="nw", window=subtitle)

        # Output settings
        outf = ttk.Frame(self)
        outf.pack(fill=tk.X, padx=12, pady=(6, 6))
        ttk.Label(outf, text="Output Folder:").grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar(value=self.settings.out_dir)
        ttk.Entry(outf, textvariable=self.out_var, width=70).grid(row=0, column=1, sticky="we", padx=8)
        ttk.Button(outf, text="📂 Browse…", style="Blue.TButton", command=self.choose_out_dir).grid(row=0, column=2)
        outf.columnconfigure(1, weight=1)

        # Options frame
        opt = ttk.Labelframe(self, text="🕷️ Options")
        opt.pack(fill=tk.X, padx=12, pady=(4, 6))

        self.start_ch_var = tk.IntVar(value=self.settings.start_chapter)
        self.threads_var  = tk.IntVar(value=self.settings.threads_per_chapter)
        self.timeout_var  = tk.IntVar(value=self.settings.request_timeout)
        self.retries_var  = tk.IntVar(value=self.settings.max_retries)
        self.min_delay_var= tk.IntVar(value=self.settings.min_delay_ms)
        self.max_delay_var= tk.IntVar(value=self.settings.max_delay_ms)
        self.robot_var    = tk.BooleanVar(value=self.settings.respect_robots)

        def add(label, widget, col):
            ttk.Label(opt, text=label).grid(row=0, column=col, sticky="w", padx=(10 if col==0 else 6, 6), pady=8)
            widget.grid(row=0, column=col+1, sticky="w", padx=(0, 14), pady=8)

        add("Start #", ttk.Spinbox(opt, from_=1, to=999999, width=8, textvariable=self.start_ch_var), 0)
        add("Threads/ch", ttk.Spinbox(opt, from_=1, to=32, width=8, textvariable=self.threads_var), 2)
        add("Timeout(s)", ttk.Spinbox(opt, from_=5, to=120, width=8, textvariable=self.timeout_var), 4)
        add("Retries", ttk.Spinbox(opt, from_=0, to=10, width=8, textvariable=self.retries_var), 6)
        add("DelayMin(ms)", ttk.Spinbox(opt, from_=0, to=2000, increment=10, width=12, textvariable=self.min_delay_var), 8)
        add("DelayMax(ms)", ttk.Spinbox(opt, from_=0, to=3000, increment=10, width=12, textvariable=self.max_delay_var), 10)

        cbrow = ttk.Frame(opt)
        cbrow.grid(row=1, column=0, columnspan=12, sticky="w", padx=10, pady=(0,10))
        ttk.Checkbutton(cbrow, text="🤖 Warn if robots.txt blocks", variable=self.robot_var).pack(side=tk.LEFT, padx=(0, 16))

        # URLs input
        urls_box = ttk.Labelframe(self, text="📜 Chapter URLs (one per line) — If Chapter 0 exists, In Start Dropdown select Zero")
        urls_box.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 6))
        self.txt_urls = tk.Text(urls_box, height=12, wrap="word", bg="#ffffff", fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY, relief="flat")
        self.txt_urls.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Controls
        ctl = ttk.Frame(self)
        ctl.pack(fill=tk.X, padx=12, pady=(4, 6))
        self.pb = ttk.Progressbar(ctl, mode="determinate")
        self.pb.grid(row=0, column=0, sticky="we", padx=(0, 8))
        ttk.Button(ctl, text="🕸️ Start", style="Accent.TButton", command=self.on_start).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(ctl, text="⏹ Stop", style="Blue.TButton", command=self.on_stop).grid(row=0, column=2)
        ctl.columnconfigure(0, weight=1)

        # Log
        logf = ttk.Labelframe(self, text="🧾 Log")
        logf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))
        self.txt_log = tk.Text(logf, height=10, wrap="none", bg=LIGHT_PANEL, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY, relief="flat")
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Status
        self.status_var = tk.StringVar(value="Ready.")
        status = tk.Label(self, textvariable=self.status_var, anchor="w", bg=LIGHT_BG, fg=TEXT_SECONDARY)
        status.pack(fill=tk.X, padx=12, pady=(0, 12))

    # ----- Logger -----
    def log(self, msg: str):
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)

    # ----- Actions -----
    def choose_out_dir(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.out_var.get() or os.getcwd())
        if d:
            self.out_var.set(d)

    def open_output(self):
        path = self.out_var.get()
        if not os.path.isdir(path):
            messagebox.showinfo(APP_NAME, "Output folder does not exist yet.")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore
        elif sys.platform == "darwin":
            os.system(f"open '{path}'")
        else:
            os.system(f"xdg-open '{path}'")

    def on_stop(self):
        self.stop_event.set()
        self.status_var.set("Stopping…")
        self.log("[SYS] Stop requested. Waiting for active downloads to finish…")

    def on_start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(APP_NAME, "A job is already running.")
            return

        urls = [u.strip() for u in self.txt_urls.get("1.0", tk.END).splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_NAME, "Paste at least one chapter URL.")
            return

        # ALWAYS reverse (start from last pasted link)
        urls = list(reversed(urls))

        # Validate delay values
        try:
            a, b = int(self.min_delay_var.get()), int(self.max_delay_var.get())
            if a > b:
                self.min_delay_var.set(b)
                self.max_delay_var.set(a)
        except Exception:
            pass

        # Apply settings (no JSON persistence)
        s = self.settings
        s.out_dir              = self.out_var.get().strip() or s.out_dir
        s.start_chapter        = int(self.start_ch_var.get())
        s.threads_per_chapter  = int(self.threads_var.get())
        s.request_timeout      = int(self.timeout_var.get())
        s.max_retries          = int(self.retries_var.get())
        s.min_delay_ms         = int(self.min_delay_var.get())
        s.max_delay_ms         = int(self.max_delay_var.get())
        s.respect_robots       = bool(self.robot_var.get())

        ensure_dir(s.out_dir)
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._run_job, args=(urls,), daemon=True)
        self.worker_thread.start()

    def _run_job(self, urls: List[str]):
        self.log("🕷️ Swinging into action…")
        self.status_var.set("Running…")

        tasks = [DownloadTask(idx, u) for idx, u in enumerate(urls, start=self.settings.start_chapter)]
        self.pb.configure(maximum=len(tasks), value=0)

        dl = ChapterDownloader(self.settings, self.log, self.stop_event)

        for t in tasks:
            if self.stop_event.is_set():
                break
            chapter_dir = os.path.join(self.settings.out_dir, str(t.chapter_index))
            self.log(f"[CH{t.chapter_index}] {t.url}")
            ok, total = dl.download_chapter(chapter_dir, t.url)
            if ok == total and total > 0:
                self.log(f"[CH{t.chapter_index}] ✅ Saved {ok}/{total} images → {chapter_dir}")
            elif total > 0:
                self.log(f"[CH{t.chapter_index}] ⚠️ Saved {ok}/{total} images → {chapter_dir}")
            else:
                self.log(f"[CH{t.chapter_index}] ❌ No images saved → {chapter_dir}")
            self.pb.step(1)

        if self.stop_event.is_set():
            self.log("🕸️ Stopped by user.")
            self.status_var.set("Stopped")
        else:
            self.log("✅ All chapters downloaded. Great power, great responsibility.")
            self.status_var.set("Done.")

# ---------------------------- CORE (headless) ----------------------------
# Small, GUI-free core so PySide6 tab can run downloads and receive logs.

import threading

class DownloaderCore:
    """
    Minimal downloader that reuses helpers from this file.
    Designed for embedding in PySide6 tab: pass a log callback and a stop_event.
    """
    def __init__(self, settings: Settings, log_cb=None, stop_event: Optional[threading.Event]=None):
        self.settings = settings
        self.log = (log_cb or (lambda m: None))
        self.stop_event = (stop_event or threading.Event())
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _download_one(self, url: str, dest: str) -> bool:
        try:
            r = self.session.get(url, stream=True, timeout=self.settings.request_timeout)
            if r.status_code != 200:
                return False
            ensure_dir(os.path.dirname(dest))
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if self.stop_event.is_set():
                        return False
                    if chunk:
                        f.write(chunk)
            return True
        except Exception:
            return False

    def download_urls(self, urls: List[str]) -> None:
        """urls = list of chapter URLs (last pasted first or normal order—your choice)."""
        s = self.settings
        ok_all = 0
        for i, chapter_url in enumerate(urls, start=1):
            if self.stop_event.is_set():
                self.log("🛑 Stopped.")
                break

            # robots.txt courtesy (optional)
            if s.respect_robots:
                allowed, robots_url = robots_allows(chapter_url)
                if not allowed:
                    self.log(f"⚠️ Robots blocks '{chapter_url}'. See: {robots_url or 'robots.txt'}")
                    continue

            # output folder: <out_dir>/Chapter-<N>
            chapter_idx = s.start_chapter + (i - 1)
            chapter_folder = os.path.join(s.out_dir, f"Chapter-{chapter_idx}")
            ensure_dir(chapter_folder)

            self.log(f"\n🔗 Chapter {chapter_idx}: {chapter_url}")
            try:
                # force list view (critical for Manhuaus/Madara)
                if "?style=list" not in url:
                    url = url.rstrip("/") + "/?style=list"

                self.session.headers.update({"Referer": url})
                html = self.session.get(url, timeout=s["timeout"]).text



            except Exception as e:
                self.log(f"  [ERR] Failed to open: {e}")
                continue

            imgs = extract_image_urls(html, chapter_url)
            if not imgs:
                self.log("  [WARN] No images found.")
                continue

            self.log(f"  Found {len(imgs)} images")

            # threaded download like the Tk app (smaller & safe)
            q = queue.Queue()
            for idx, u in enumerate(imgs, start=1):
                q.put((idx, u))

            def worker():
                while not q.empty() and not self.stop_event.is_set():
                    idx, u = q.get()
                    base = f"{idx:03d}"
                    url_ext = os.path.splitext(urlparse(u).path)[1].lower()
                    ext = url_ext if (url_ext and len(url_ext) <= 5) else ".jpg"
                    dest = os.path.join(chapter_folder, base + ext)
                    if os.path.exists(dest):
                        self.log(f"  ⏭️ {os.path.basename(dest)}")
                        q.task_done(); continue
                    if not self._download_one(u, dest):
                        # try to infer ext from content-type
                        try:
                            hr = self.session.head(u, timeout=10, allow_redirects=True)
                            ext2 = guess_ext_from_ct(hr.headers.get("Content-Type","")) or ext
                            dest2 = os.path.join(chapter_folder, base + ext2)
                            if dest2 != dest:
                                self._download_one(u, dest2)
                        except Exception:
                            pass
                    q.task_done()
                    if s.min_delay_ms or s.max_delay_ms:
                        time.sleep(random.uniform(s.min_delay_ms, s.max_delay_ms)/1000.0)

            threads = []
            tpch = max(1, min(8, s.threads_per_chapter))
            for _ in range(tpch):
                t = threading.Thread(target=worker, daemon=True); t.start(); threads.append(t)
            for t in threads:
                t.join()

            ok_all += 1
            self.log(f"  ✅ Saved to: {chapter_folder}")

        if not self.stop_event.is_set():
            self.log("\n🎉 Done.")


# ---------------------------- MAIN ----------------------------
def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()