"""
Live Screen Translator
======================

Pick an area of your screen once; the app keeps reading the text inside it
(Windows built-in OCR) and translates it whenever it changes.

Hotkeys (global):
    Ctrl+Shift+T   select a new area
    Ctrl+Shift+S   pause / resume
    Ctrl+Shift+M   toggle compact mode (translation text only)

Requires Windows 10/11 and Python 3.9+.  See README.md for setup.
"""
from __future__ import annotations

import asyncio
import ctypes
import difflib
import inspect
import json
import os
import queue
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional, Tuple

from PIL import ImageChops, ImageEnhance, ImageGrab, ImageOps, ImageStat, ImageTk
import winocr
from deep_translator import GoogleTranslator

try:
    import keyboard  # optional: global hotkeys
except ImportError:  # pragma: no cover
    keyboard = None

try:
    import argostranslate.package as argos_package      # optional: offline translation
    import argostranslate.translate as argos_translate
except ImportError:  # pragma: no cover
    argos_package = argos_translate = None

# Use real pixels on high-DPI displays so the selection matches the capture.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
APP_NAME = "Live Screen Translator"
HOTKEY_SELECT = "ctrl+shift+t"
HOTKEY_TOGGLE = "ctrl+shift+s"
HOTKEY_COMPACT = "ctrl+shift+m"

ACCENT = "#00d26a"      # frame / selection colour
WARN = "#d93025"        # overlap warning colour
COMPACT_BG = "#14161a"

Box = Tuple[int, int, int, int]

ENGINE_ONLINE = "Online (Google)"
ENGINE_OFFLINE = "Offline (Argos)"
ENGINES = (ENGINE_ONLINE, ENGINE_OFFLINE)

# Display name -> Windows OCR language tag. Only languages that are actually
# installed in Windows are shown in the dropdown (see installed_ocr_languages).
OCR_LANGS = {
    "English": "en",
    "Chinese (Simplified)": "zh-Hans-CN",
    "Chinese (Traditional)": "zh-Hant-TW",
    "Japanese": "ja",
    "Korean": "ko",
    "Malay": "ms",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Portuguese": "pt",
    "Russian": "ru",
    "Arabic": "ar",
    "Thai": "th",
    "Vietnamese": "vi",
}

# Display name -> Google language code, e.g. "Spanish" -> "es"
TARGET_LANGS = {
    name.title(): code
    for name, code in GoogleTranslator().get_supported_languages(as_dict=True).items()
}


# --------------------------------------------------------------------------
# Settings (saved between runs)
# --------------------------------------------------------------------------
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "LiveScreenTranslator" / "settings.json"

DEFAULT_SETTINGS = {
    "src": "English",
    "dst": "English",
    "engine": ENGINE_ONLINE,
    "interval": 1.0,
    "enhance": True,
    "history": False,
    "font_size": 14,
    "opacity": 0.92,
    "full_geometry": "",
    "compact_geometry": "",
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# OCR language discovery
# --------------------------------------------------------------------------
def installed_ocr_languages() -> dict:
    """Return the subset of OCR_LANGS that Windows can actually recognise."""
    try:
        try:
            from winrt.windows.media.ocr import OcrEngine
        except ImportError:
            from winsdk.windows.media.ocr import OcrEngine
        tags = [lang.language_tag.lower() for lang in OcrEngine.available_recognizer_languages]
    except Exception:
        return dict(OCR_LANGS)

    def covered(tag: str) -> bool:
        tag = tag.lower()
        return any(t == tag or t.startswith(tag + "-") for t in tags)

    found = {name: tag for name, tag in OCR_LANGS.items() if covered(tag)}
    known = [t.lower() for t in found.values()]
    for t in tags:  # installed languages that aren't in our friendly list
        if not any(t == k or t.startswith(k + "-") for k in known):
            found[t] = t
    return found or dict(OCR_LANGS)


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------
_cache: dict = {}


def _gtx(text: str, target: str) -> str:
    """Google's lightweight web endpoint (unofficial, no API key)."""
    url = ("https://translate.googleapis.com/translate_a/single?"
           + urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": target, "dt": "t"}))
    body = urllib.parse.urlencode({"q": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(part[0] for part in data[0] if part and part[0])


def _online_translate(text: str, target: str) -> str:
    try:
        return _gtx(text, target)
    except Exception as first:
        try:
            return GoogleTranslator(source="auto", target=target).translate(text)
        except Exception as second:
            raise RuntimeError(f"Translation failed ({first}; fallback: {second})") from second


# ---- Offline engine (Argos Translate) -------------------------------------
_argos_cache: dict = {}
_ARGOS_ALIASES = {"iw": "he"}   # Google code -> Argos code


def argos_code(code: str) -> str:
    """Map an OCR tag or Google code (zh-Hans-CN, zh-TW, en-US...) to an Argos code."""
    c = code.lower()
    if c in ("zh-tw", "zh-hant", "zh-hant-tw"):
        return "zt"                                 # Traditional Chinese
    if c.startswith("zh"):
        return "zh"
    c = c.split("-")[0]
    return _ARGOS_ALIASES.get(c, c)


def _require_argos() -> None:
    if argos_translate is None or argos_package is None:
        raise RuntimeError("Offline mode needs Argos Translate. Run:  pip install argostranslate")


def _argos_translation(src: str, dst: str):
    """Return an installed Argos translation object for src -> dst, or None."""
    key = (src, dst)
    if key not in _argos_cache:
        langs = argos_translate.get_installed_languages()
        a = next((lang for lang in langs if lang.code == src), None)
        b = next((lang for lang in langs if lang.code == dst), None)
        translation = a.get_translation(b) if a and b else None
        if translation is None:
            return None                              # don't cache misses
        _argos_cache[key] = translation
    return _argos_cache[key]


def _offline_translate(text: str, src_tag: str, dst_code: str) -> str:
    _require_argos()
    src, dst = argos_code(src_tag), argos_code(dst_code)
    if src == dst:
        return text
    translation = _argos_translation(src, dst)
    if translation is None:
        raise RuntimeError(
            f"No offline model installed for {src} -> {dst}. "
            "Click 'Get offline pack' (internet needed once).")
    return translation.translate(text)


def install_offline_pack(src_tag: str, dst_code: str, report: Callable[[str], None]) -> None:
    """Download + install the Argos model(s) for src -> dst (pivots through English)."""
    _require_argos()
    src, dst = argos_code(src_tag), argos_code(dst_code)
    if src == dst:
        return
    pairs = [(src, dst)] if "en" in (src, dst) else [(src, "en"), ("en", dst)]
    report("Fetching package list...")
    argos_package.update_package_index()
    available = argos_package.get_available_packages()
    installed = {(p.from_code, p.to_code) for p in argos_package.get_installed_packages()}
    for a, b in pairs:
        if (a, b) in installed:
            continue
        pkg = next((p for p in available if p.from_code == a and p.to_code == b), None)
        if pkg is None:
            raise RuntimeError(f"No offline model exists for {a} -> {b}")
        report(f"Downloading {a} -> {b} (about 100 MB)...")
        argos_package.install_from_path(pkg.download())
    _argos_cache.clear()


def translate(text: str, target: str, source_tag: str = "en", engine: str = ENGINE_ONLINE) -> str:
    """Translate `text` into `target` (Google-style code). Cached.

    `source_tag` is the OCR language tag; it is only used by the offline engine,
    which (unlike Google) needs to be told the source language.
    """
    key = (engine, source_tag, target, text)
    if key in _cache:
        return _cache[key]
    if engine == ENGINE_OFFLINE:
        result = _offline_translate(text, source_tag, target)
    else:
        result = _online_translate(text, target)
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = result
    return result


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
class TextReader:
    """Wraps Windows OCR. Create one per thread (it owns an asyncio loop)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    @staticmethod
    def _prepare(img, enhance: bool):
        if enhance:
            gray = img.convert("L")
            if ImageStat.Stat(gray).mean[0] < 110:      # light text on dark background
                gray = ImageOps.invert(gray)
            gray = ImageOps.autocontrast(gray)
            if gray.width < 1500:
                gray = gray.resize((gray.width * 2, gray.height * 2))
            return gray.convert("RGB")
        if img.width < 600:
            return img.resize((img.width * 2, img.height * 2))
        return img

    def read(self, img, lang: str, enhance: bool) -> str:
        result = winocr.recognize_pil(self._prepare(img, enhance), lang)
        if inspect.isawaitable(result):                 # supports sync + async winocr versions
            async def _wait(awaitable):
                return await awaitable
            result = self.loop.run_until_complete(_wait(result))
        text = result.get("text", "") if isinstance(result, dict) else getattr(result, "text", str(result))
        return " ".join(text.split())

    def close(self) -> None:
        self.loop.close()


def image_changed(a, b, threshold: float = 0.25) -> bool:
    if a.size != b.size:
        return True
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    return ImageStat.Stat(diff).mean[0] > threshold


# --------------------------------------------------------------------------
# Screen-selection widgets
# --------------------------------------------------------------------------
class SnipOverlay:
    """Frozen, dimmed screenshot; drag to choose a rectangle (Esc cancels)."""

    def __init__(self, root: tk.Tk, screenshot, on_done: Callable[[Optional[Box]], None]) -> None:
        self.on_done = on_done
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        w, h = screenshot.size
        self.win.geometry(f"{w}x{h}+0+0")
        self.canvas = tk.Canvas(self.win, cursor="cross", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._bg = ImageTk.PhotoImage(ImageEnhance.Brightness(screenshot).enhance(0.55))
        self.canvas.create_image(0, 0, image=self._bg, anchor="nw")
        self.start: Optional[Tuple[int, int]] = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self._close(None))
        self.win.focus_force()

    def _press(self, e) -> None:
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2)

    def _drag(self, e) -> None:
        if self.rect and self.start:
            self.canvas.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e) -> None:
        if not self.start:
            return self._close(None)
        x1, y1 = self.start
        box = (min(x1, e.x), min(y1, e.y), max(x1, e.x), max(y1, e.y))
        self._close(box if box[2] - box[0] > 10 and box[3] - box[1] > 10 else None)

    def _close(self, box: Optional[Box]) -> None:
        self.win.destroy()
        self.on_done(box)


class RegionFrame:
    """Click-through border drawn just *outside* the watched area,
    so it never appears in the OCR capture."""
    B = 3
    KEY = "#ff00ff"  # made transparent

    def __init__(self, root: tk.Tk, box: Box) -> None:
        x1, y1, x2, y2 = box
        b = self.B
        w, h = x2 - x1 + 2 * b, y2 - y1 + 2 * b
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", self.KEY)
        self.win.geometry(f"{w}x{h}+{x1 - b}+{y1 - b}")
        canvas = tk.Canvas(self.win, bg=self.KEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(b / 2, b / 2, w - b / 2, h - b / 2, outline=ACCENT, width=b)

    def show(self) -> None:
        self.win.deiconify()

    def hide(self) -> None:
        self.win.withdraw()

    def destroy(self) -> None:
        self.win.destroy()


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.ocr_langs = installed_ocr_languages()

        self.box: Optional[Box] = None
        self.frame: Optional[RegionFrame] = None
        self.running = False
        self.compact = False
        self.font_size = int(self.settings["font_size"])
        self.opacity = float(self.settings["opacity"])
        self.stop_event = threading.Event()
        self.ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()  # other threads -> UI
        self.cfg = {"src": "en", "dst": "en", "engine": ENGINE_ONLINE,
                    "interval": 1.0, "enhance": True, "version": 0}
        self._downloading = False
        self._drag = (0, 0)
        self._tick = 0
        self._overlap = False
        self._placeholder = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry(self.settings["full_geometry"] or "480x560")
        self.root.attributes("-topmost", True)

        self._build_ui()
        self._sync()
        self._register_hotkeys()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---- UI construction ---------------------------------------------------
    def _build_ui(self) -> None:
        s = self.settings
        root = self.root

        # Header: all the controls (hidden in compact mode)
        self.header = ttk.Frame(root)
        top = ttk.Frame(self.header, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Text on screen is in:").grid(row=0, column=0, sticky="w")
        self.src = ttk.Combobox(top, values=list(self.ocr_langs), state="readonly", width=22)
        self.src.set(s["src"] if s["src"] in self.ocr_langs else next(iter(self.ocr_langs)))
        self.src.grid(row=0, column=1, padx=6, pady=2)

        ttk.Label(top, text="Translate into:").grid(row=1, column=0, sticky="w")
        self.dst = ttk.Combobox(top, values=sorted(TARGET_LANGS), state="readonly", width=22)
        self.dst.set(s["dst"] if s["dst"] in TARGET_LANGS else "English")
        self.dst.grid(row=1, column=1, padx=6, pady=2)

        ttk.Label(top, text="Check every (sec):").grid(row=2, column=0, sticky="w")
        self.interval = tk.DoubleVar(value=float(s["interval"]))
        ttk.Spinbox(top, from_=0.3, to=10, increment=0.1, width=6,
                    textvariable=self.interval, command=self._sync).grid(
            row=2, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(top, text="Translation engine:").grid(row=3, column=0, sticky="w")
        self.engine = ttk.Combobox(top, values=list(ENGINES), state="readonly", width=22)
        self.engine.set(s["engine"] if s["engine"] in ENGINES else ENGINE_ONLINE)
        self.engine.grid(row=3, column=1, padx=6, pady=2)
        self.pack_btn = ttk.Button(top, text="Get offline pack", command=self.download_pack)
        self.pack_btn.grid(row=3, column=2, padx=4)

        for combo in (self.src, self.dst, self.engine):
            combo.bind("<<ComboboxSelected>>", lambda e: self._sync())
        self.interval.trace_add("write", lambda *a: self._sync())

        buttons = ttk.Frame(self.header, padding=(10, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Select area", command=self.select_area).pack(side="left")
        self.toggle_btn = ttk.Button(buttons, text="Start", command=self.toggle)
        self.toggle_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="Compact", command=lambda: self.set_compact(True)).pack(side="left")

        options = ttk.Frame(self.header, padding=(10, 4))
        options.pack(fill="x")
        self.history = tk.BooleanVar(value=bool(s["history"]))
        ttk.Checkbutton(options, text="Keep history", variable=self.history).pack(side="left")
        self.enhance = tk.BooleanVar(value=bool(s["enhance"]))
        ttk.Checkbutton(options, text="Enhance image (dark / video text)",
                        variable=self.enhance, command=self._sync).pack(side="left", padx=10)

        ttk.Label(self.header, text="Original").pack(anchor="w", padx=10)
        self.orig = tk.Text(self.header, height=4, wrap="word", fg="#555")
        self.orig.pack(fill="x", padx=10)

        # Body: the translation
        self.body = ttk.Frame(root)
        self.out_label = ttk.Label(self.body, text="Translation")
        self.out_label.pack(anchor="w", pady=(8, 0))
        self.out = tk.Text(self.body, height=5, wrap="word")
        self.out.pack(fill="both", expand=True)

        # Footer: status + small buttons (hidden in compact mode)
        self.bar = ttk.Frame(root, padding=10)
        ttk.Button(self.bar, text="Copy", command=self.copy).pack(side="left")
        ttk.Button(self.bar, text="Clear", command=self.clear).pack(side="left", padx=6)
        self.status = ttk.Label(self.bar, text="Click 'Select area' to begin")
        self.status.pack(side="right")

        self.body.pack(fill="both", expand=True, padx=10)
        self.header.pack(side="top", fill="x", before=self.body)
        self.bar.pack(side="bottom", fill="x", before=self.body)

        # Resize grip used in compact (borderless) mode
        self.grip = tk.Label(root, text="\u25e2", bg=COMPACT_BG, fg="#888")
        for cursor in ("size_nw_se", "sizing"):      # first name is Windows-only
            try:
                self.grip.configure(cursor=cursor)
                break
            except tk.TclError:
                continue
        self.grip.bind("<B1-Motion>", self._resize_drag)

        # Read-only text boxes (still selectable / copyable)
        for widget in (self.orig, self.out):
            widget.bind("<Key>", lambda e: None if e.state & 0x4 else "break")
        # Compact-mode interactions
        self.out.bind("<ButtonPress-1>", self._drag_start)
        self.out.bind("<B1-Motion>", self._drag_move)
        self.out.bind("<Double-Button-1>", lambda e: self.set_compact(False) if self.compact else None)
        self.out.bind("<Button-3>", self._context_menu)
        self.grip.bind("<Button-3>", self._context_menu)

        self._style_out()

    def _style_out(self) -> None:
        overlap = dict(highlightthickness=3 if self._overlap else 0,
                       highlightbackground=WARN, highlightcolor=WARN)
        if self.compact:
            self.out.configure(bg=COMPACT_BG, fg="#ffffff", insertbackground="#ffffff",
                               font=("Segoe UI", self.font_size), relief="flat",
                               borderwidth=0, padx=12, pady=8, cursor="fleur", **overlap)
        else:
            self.out.configure(bg="#ffffff", fg="#000000", insertbackground="#000000",
                               font=("Segoe UI", 12), relief="solid",
                               borderwidth=1, padx=4, pady=4, cursor="xterm", **overlap)

    # ---- hotkeys -----------------------------------------------------------
    def _register_hotkeys(self) -> None:
        if keyboard is None:
            self.status.config(text="Hotkeys disabled (install 'keyboard')")
            return
        try:
            keyboard.add_hotkey(HOTKEY_SELECT, lambda: self.ui_queue.put(self.select_area))
            keyboard.add_hotkey(HOTKEY_TOGGLE, lambda: self.ui_queue.put(self.toggle))
            keyboard.add_hotkey(HOTKEY_COMPACT, lambda: self.ui_queue.put(self.toggle_compact))
        except Exception as exc:
            self.status.config(text=f"Hotkeys unavailable: {exc}")

    # ---- UI-thread plumbing ------------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self._tick += 1
        if self._tick % 10 == 0:
            self._check_overlap()
        self.root.after(100, self._poll)

    def post_status(self, text: str) -> None:
        self.ui_queue.put(lambda t=text: self.status.config(text=t))

    def _check_overlap(self) -> None:
        """Warn (red border) if this window sits on top of the scanned area."""
        overlap = False
        if self.running and self.box and self.root.state() != "withdrawn":
            x1, y1, x2, y2 = self.box
            wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
            wx2, wy2 = wx + self.root.winfo_width(), wy + self.root.winfo_height()
            overlap = wx < x2 and wx2 > x1 and wy < y2 and wy2 > y1
        if overlap != self._overlap:
            self._overlap = overlap
            self._style_out()
            if overlap and not self.compact:
                self.status.config(text="Window overlaps the scan area!")

    # ---- settings ----------------------------------------------------------
    def _sync(self) -> None:
        """Copy UI values into a plain dict the worker thread can read safely."""
        try:
            interval = max(0.3, float(self.interval.get()))
        except (tk.TclError, ValueError):
            interval = 1.0
        self.cfg = {
            "src": self.ocr_langs.get(self.src.get(), "en"),
            "dst": TARGET_LANGS.get(self.dst.get(), "en"),
            "engine": self.engine.get(),
            "interval": interval,
            "enhance": bool(self.enhance.get()),
            "version": self.cfg["version"] + 1,   # tells the worker to re-read the area
        }
        offline = self.engine.get() == ENGINE_OFFLINE
        self.pack_btn.state(["!disabled"] if offline and not self._downloading else ["disabled"])

    def download_pack(self) -> None:
        """Download the offline model(s) for the current language pair (internet needed once)."""
        cfg = self.cfg
        self._downloading = True
        self._sync()
        self.status.config(text="Preparing download...")

        def job() -> None:
            try:
                install_offline_pack(cfg["src"], cfg["dst"], self.post_status)
                error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

            def finish() -> None:
                self._downloading = False
                self._sync()                        # also lets the worker retry immediately
                if error:
                    self.show_error(error)
                else:
                    self.status.config(text="Offline pack ready")
            self.ui_queue.put(finish)

        threading.Thread(target=job, daemon=True).start()

    # ---- area selection ----------------------------------------------------
    def select_area(self) -> None:
        if self.running:
            self.stop()
        if self.frame:
            self.frame.hide()
        self.root.withdraw()
        self.root.after(250, lambda: SnipOverlay(self.root, ImageGrab.grab(), self._area_done))

    def _area_done(self, box: Optional[Box]) -> None:
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if box is None:
            if self.frame:
                self.frame.show()
            self.status.config(text="Selection cancelled")
            return
        if self.frame:
            self.frame.destroy()
        self.box = box
        self.frame = RegionFrame(self.root, box)
        self.start()

    # ---- start / stop ------------------------------------------------------
    def toggle(self) -> None:
        self.stop() if self.running else self.start()

    def start(self) -> None:
        if not self.box:
            self.select_area()
            return
        if self.running:
            return
        self.running = True
        self.stop_event = threading.Event()
        if self.frame:
            self.frame.show()
        self.toggle_btn.config(text="Stop")
        self.status.config(text="Live...")
        threading.Thread(target=self._worker, args=(self.stop_event, self.box), daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self.stop_event.set()
        self.toggle_btn.config(text="Start")
        self.status.config(text="Paused")

    # ---- background worker -------------------------------------------------
    def _worker(self, stop_event: threading.Event, box: Box) -> None:
        reader = TextReader()
        last_img, last_text, last_version, last_err = None, "", -1, ""
        cooldown_until = 0.0
        try:
            while not stop_event.is_set():
                t0 = time.time()
                cfg = self.cfg
                if cfg["version"] != last_version:          # settings changed
                    last_version, last_img, last_text = cfg["version"], None, ""
                    cooldown_until = 0.0                    # e.g. offline pack just installed
                if t0 < cooldown_until:             # backing off after an error
                    self.post_status(f"Paused {int(cooldown_until - t0)}s (error / rate limit)")
                    stop_event.wait(1)
                    continue
                try:
                    img = ImageGrab.grab(bbox=box)
                    if last_img is None or image_changed(last_img, img):
                        text = reader.read(img, cfg["src"], cfg["enhance"])
                        if not text:
                            self.post_status("No text found in area")
                        elif difflib.SequenceMatcher(None, text, last_text).ratio() < 0.9:
                            self.post_status(
                                "Translating..." if cfg["engine"] == ENGINE_ONLINE
                                else "Translating offline (first run loads the model)...")
                            translated = translate(text, cfg["dst"], cfg["src"], cfg["engine"])
                            last_text = text
                            self.ui_queue.put(lambda a=text, b=translated: self.show(a, b))
                        last_img = img
                    last_err = ""
                except Exception as exc:
                    last_img = None                          # retry after the pause
                    cooldown_until = time.time() + 30
                    msg = f"{type(exc).__name__}: {exc}"
                    if msg != last_err:
                        last_err = msg
                        self.ui_queue.put(lambda m=msg: self.show_error(m))
                stop_event.wait(max(0.1, cfg["interval"] - (time.time() - t0)))
        finally:
            reader.close()

    # ---- output ------------------------------------------------------------
    def _set_placeholder(self, text: str) -> None:
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self._placeholder = True

    def show(self, original: str, translated: str) -> None:
        self.orig.delete("1.0", "end")
        self.orig.insert("1.0", original)
        if self._placeholder:
            self.out.delete("1.0", "end")
            self._placeholder = False
        if self.history.get():
            self.out.insert("end", translated + "\n\n")
            if int(self.out.index("end-1c").split(".")[0]) > 300:   # keep history bounded
                self.out.delete("1.0", "100.0")
        else:
            self.out.delete("1.0", "end")
            self.out.insert("1.0", translated)
        self.out.see("end")
        self.status.config(text="Live... (updated)")

    def show_error(self, message: str) -> None:
        self.orig.delete("1.0", "end")
        self.orig.insert("1.0", "ERROR - " + message)
        self.status.config(text="Error (see Original box)")
        if self.compact:        # the Original box is hidden in compact mode
            self._set_placeholder("\u26a0 " + message[:200])

    def copy(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.out.get("1.0", "end").strip())
        self.status.config(text="Copied")

    def clear(self) -> None:
        self.orig.delete("1.0", "end")
        self.out.delete("1.0", "end")
        self._placeholder = False

    # ---- compact mode ------------------------------------------------------
    def toggle_compact(self) -> None:
        self.set_compact(not self.compact)

    def set_compact(self, on: bool) -> None:
        """Compact = borderless, always-on-top box showing only the translation."""
        if on == self.compact:
            return
        default_compact = f"520x110+{self.root.winfo_x()}+{self.root.winfo_y()}"
        self.root.withdraw()
        if on:
            self.settings["full_geometry"] = self.root.geometry()
            self.compact = True
            self.header.pack_forget()
            self.bar.pack_forget()
            self.out_label.pack_forget()
            self.body.pack_configure(padx=0)
            self.root.overrideredirect(True)
            self.root.geometry(self.settings["compact_geometry"] or default_compact)
            self.root.attributes("-alpha", self.opacity)
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
            if not self.out.get("1.0", "end").strip():
                self._set_placeholder("Waiting for text...  (right-click for menu, double-click to expand)")
        else:
            self.settings["compact_geometry"] = self.root.geometry()
            self.compact = False
            self.grip.place_forget()
            self.root.overrideredirect(False)
            self.root.attributes("-alpha", 1.0)
            self.body.pack_configure(padx=10)
            self.out_label.pack(anchor="w", pady=(8, 0), before=self.out)
            self.header.pack(side="top", fill="x", before=self.body)
            self.bar.pack(side="bottom", fill="x", before=self.body)
            self.root.geometry(self.settings["full_geometry"] or "480x560")
        self._style_out()
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def _drag_start(self, e):
        if not self.compact:
            return None
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        return "break"

    def _drag_move(self, e):
        if not self.compact:
            return None
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        return "break"

    def _resize_drag(self, e) -> None:
        w = max(160, e.x_root - self.root.winfo_rootx())
        h = max(50, e.y_root - self.root.winfo_rooty())
        self.root.geometry(f"{w}x{h}")

    def change_font(self, delta: int) -> None:
        self.font_size = max(8, min(48, self.font_size + delta))
        self._style_out()

    def set_opacity(self, value: float) -> None:
        self.opacity = value
        if self.compact:
            self.root.attributes("-alpha", value)

    def _context_menu(self, e) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        if self.compact:
            menu.add_command(label="Expand window", command=lambda: self.set_compact(False))
        else:
            menu.add_command(label="Compact mode", command=lambda: self.set_compact(True))
        menu.add_command(label="Pause" if self.running else "Resume", command=self.toggle)
        menu.add_command(label="Select new area", command=self.select_area)
        menu.add_command(label="Copy translation", command=self.copy)
        menu.add_separator()
        menu.add_command(label="Bigger text", command=lambda: self.change_font(+2))
        menu.add_command(label="Smaller text", command=lambda: self.change_font(-2))
        opacity = tk.Menu(menu, tearoff=0)
        for pct in (100, 90, 75, 50):
            opacity.add_command(label=f"{pct}%", command=lambda p=pct: self.set_opacity(p / 100))
        menu.add_cascade(label="Opacity (compact)", menu=opacity)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # ---- shutdown ----------------------------------------------------------
    def quit(self) -> None:
        self.stop_event.set()
        geometry = self.root.geometry()
        self.settings.update({
            "src": self.src.get(), "dst": self.dst.get(), "engine": self.engine.get(),
            "interval": self.cfg["interval"], "enhance": bool(self.enhance.get()),
            "history": bool(self.history.get()), "font_size": self.font_size,
            "opacity": self.opacity,
        })
        self.settings["compact_geometry" if self.compact else "full_geometry"] = geometry
        save_settings(self.settings)
        if keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()