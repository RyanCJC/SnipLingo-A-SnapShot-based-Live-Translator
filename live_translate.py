"""
Live Screen Translator  -  pick a region once, it keeps reading + translating.

Setup (Windows 10/11, Python 3.9+):
    pip install pillow winocr deep-translator keyboard

Run:
    python live_translate.py

Use:
    1. "Select area" (Ctrl+Shift+T): drag a box over the text. A green frame stays.
    2. It starts automatically. Ctrl+Shift+S pauses / resumes.

Keep this window OUTSIDE the green box (it would read its own output).
Errors are shown in the "Original" box so you can see what went wrong.
"""
import asyncio
import ctypes
import difflib
import inspect
import json
import queue
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import ttk

from PIL import ImageChops, ImageEnhance, ImageGrab, ImageOps, ImageStat, ImageTk
import winocr
from deep_translator import GoogleTranslator

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

HOTKEY_SELECT = "ctrl+shift+t"
HOTKEY_TOGGLE = "ctrl+shift+s"

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
    "Russian": "ru",
    "Arabic": "ar",
    "Thai": "th",
    "Vietnamese": "vi",
}
TARGET_LANGS = GoogleTranslator().get_supported_languages(as_dict=True)  # name -> code

_cache = {}


def _gtx(text, target):
    """Google's lightweight endpoint (separate from the one deep-translator uses)."""
    url = ("https://translate.googleapis.com/translate_a/single?"
           + urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": target, "dt": "t"}))
    body = urllib.parse.urlencode({"q": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(part[0] for part in data[0] if part and part[0])


def translate(text, target):
    key = (text, target)
    if key in _cache:
        return _cache[key]
    try:
        result = _gtx(text, target)
    except Exception as e1:
        try:
            result = GoogleTranslator(source="auto", target=target).translate(text)
        except Exception as e2:
            raise RuntimeError(f"Translation failed. Endpoint 1: {e1}. Endpoint 2: {e2}")
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = result
    return result


# --------------------------------------------------------------------------
class SnipOverlay:
    def __init__(self, root, screenshot, on_done):
        self.shot, self.on_done = screenshot, on_done
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        w, h = screenshot.size
        self.win.geometry(f"{w}x{h}+0+0")
        self.canvas = tk.Canvas(self.win, cursor="cross", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.bg = ImageTk.PhotoImage(ImageEnhance.Brightness(screenshot).enhance(0.55))
        self.canvas.create_image(0, 0, image=self.bg, anchor="nw")
        self.start = self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.win.bind("<Escape>", lambda e: self.close(None))
        self.win.focus_force()

    def press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00d26a", width=2)

    def drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, *self.start, e.x, e.y)

    def release(self, e):
        x1, y1 = self.start
        box = (min(x1, e.x), min(y1, e.y), max(x1, e.x), max(y1, e.y))
        self.close(box if box[2] - box[0] > 10 and box[3] - box[1] > 10 else None)

    def close(self, box):
        self.win.destroy()
        self.on_done(box)


class RegionFrame:
    """Click-through green border drawn just OUTSIDE the watched area."""
    B = 3
    KEY = "#ff00ff"

    def __init__(self, root, box):
        x1, y1, x2, y2 = box
        b = self.B
        w, h = x2 - x1 + 2 * b, y2 - y1 + 2 * b
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", self.KEY)
        self.win.geometry(f"{w}x{h}+{x1 - b}+{y1 - b}")
        c = tk.Canvas(self.win, bg=self.KEY, highlightthickness=0)
        c.pack(fill="both", expand=True)
        c.create_rectangle(b / 2, b / 2, w - b / 2, h - b / 2, outline="#00d26a", width=b)

    def show(self):
        self.win.deiconify()

    def hide(self):
        self.win.withdraw()

    def destroy(self):
        self.win.destroy()


# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Live Screen Translator")
        self.root.geometry("480x500")
        self.root.attributes("-topmost", True)

        self.box = None
        self.frame = None
        self.running = False
        self.stop_event = threading.Event()
        self.q = queue.Queue()          # worker/hotkey threads -> UI thread
        self.cfg = {"src": "en", "dst": "en", "interval": 1.0, "enhance": True, "version": 0}

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Text on screen is in:").grid(row=0, column=0, sticky="w")
        self.src = ttk.Combobox(top, values=list(OCR_LANGS), state="readonly", width=22)
        self.src.set("English")
        self.src.grid(row=0, column=1, padx=6, pady=2)

        ttk.Label(top, text="Translate into:").grid(row=1, column=0, sticky="w")
        self.dst = ttk.Combobox(top, values=sorted(TARGET_LANGS), state="readonly", width=22)
        self.dst.set("english" if "english" in TARGET_LANGS else sorted(TARGET_LANGS)[0])
        self.dst.grid(row=1, column=1, padx=6, pady=2)

        ttk.Label(top, text="Check every (sec):").grid(row=2, column=0, sticky="w")
        self.interval = tk.DoubleVar(value=1.0)
        ttk.Spinbox(top, from_=0.3, to=10, increment=0.1, width=6,
                    textvariable=self.interval, command=self.sync).grid(
            row=2, column=1, sticky="w", padx=6, pady=2)

        for w in (self.src, self.dst):
            w.bind("<<ComboboxSelected>>", lambda e: self.sync())
        self.interval.trace_add("write", lambda *a: self.sync())

        btns = ttk.Frame(self.root, padding=(10, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text=f"Select area ({HOTKEY_SELECT})", command=self.select_area).pack(side="left")
        self.toggle_btn = ttk.Button(btns, text=f"Start ({HOTKEY_TOGGLE})", command=self.toggle)
        self.toggle_btn.pack(side="left", padx=8)

        opts = ttk.Frame(self.root, padding=(10, 4))
        opts.pack(fill="x")
        self.history = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Keep history", variable=self.history).pack(side="left")
        self.enhance = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Enhance image (helps dark / video text)",
                        variable=self.enhance, command=self.sync).pack(side="left", padx=10)

        ttk.Label(self.root, text="Original").pack(anchor="w", padx=10)
        self.orig = tk.Text(self.root, height=6, wrap="word", fg="#555")
        self.orig.pack(fill="both", expand=True, padx=10)

        ttk.Label(self.root, text="Translation").pack(anchor="w", padx=10, pady=(8, 0))
        self.out = tk.Text(self.root, height=9, wrap="word", font=("Segoe UI", 12))
        self.out.pack(fill="both", expand=True, padx=10)

        bar = ttk.Frame(self.root, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Copy", command=self.copy).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="Select an area to begin")
        self.status.pack(side="right")

        self.sync()
        try:
            import keyboard
            keyboard.add_hotkey(HOTKEY_SELECT, lambda: self.q.put(self.select_area))
            keyboard.add_hotkey(HOTKEY_TOGGLE, lambda: self.q.put(self.toggle))
        except Exception as e:
            self.status.config(text=f"Hotkeys unavailable: {e}")

        self.root.after(100, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---- UI-thread queue ------------------------------------------------
    def poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def set_status(self, text):
        self.q.put(lambda t=text: self.status.config(text=t))

    # ---- settings -------------------------------------------------------
    def sync(self):
        try:
            interval = max(0.3, float(self.interval.get()))
        except (tk.TclError, ValueError):
            interval = 1.0
        self.cfg = {
            "src": OCR_LANGS[self.src.get()],
            "dst": TARGET_LANGS[self.dst.get()],
            "interval": interval,
            "enhance": bool(self.enhance.get()),
            "version": self.cfg["version"] + 1,
        }

    # ---- area selection -------------------------------------------------
    def select_area(self):
        if self.running:
            self.stop()
        if self.frame:
            self.frame.hide()
        self.root.withdraw()
        self.root.after(250, self._grab)

    def _grab(self):
        SnipOverlay(self.root, ImageGrab.grab(), self.area_done)

    def area_done(self, box):
        self.root.deiconify()
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

    # ---- start / stop ---------------------------------------------------
    def toggle(self):
        self.stop() if self.running else self.start()

    def start(self):
        if not self.box:
            self.select_area()
            return
        if self.running:
            return
        self.running = True
        self.stop_event = threading.Event()
        if self.frame:
            self.frame.show()
        self.toggle_btn.config(text=f"Stop ({HOTKEY_TOGGLE})")
        self.status.config(text="Live...")
        threading.Thread(target=self.worker, args=(self.stop_event, self.box), daemon=True).start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        self.toggle_btn.config(text=f"Start ({HOTKEY_TOGGLE})")
        self.status.config(text="Paused")

    # ---- OCR ------------------------------------------------------------
    @staticmethod
    def changed(a, b):
        if a.size != b.size:
            return True
        diff = ImageChops.difference(a.convert("L"), b.convert("L"))
        return ImageStat.Stat(diff).mean[0] > 0.25

    def ocr(self, loop, img, cfg):
        if cfg["enhance"]:
            g = img.convert("L")
            if ImageStat.Stat(g).mean[0] < 110:      # light text on dark background
                g = ImageOps.invert(g)
            g = ImageOps.autocontrast(g)
            if g.width < 1500:
                g = g.resize((g.width * 2, g.height * 2))
            img = g.convert("RGB")
        elif img.width < 600:
            img = img.resize((img.width * 2, img.height * 2))

        res = winocr.recognize_pil(img, cfg["src"])
        if inspect.isawaitable(res):                # works for async and sync winocr versions
            async def _wrap(a):
                return await a
            res = loop.run_until_complete(_wrap(res))
        text = res.get("text", "") if isinstance(res, dict) else getattr(res, "text", str(res))
        return " ".join(text.split())

    def worker(self, stop_event, box):
        loop = asyncio.new_event_loop()
        last_img, last_text, last_version, last_err = None, "", -1, ""
        cooldown_until = 0.0

        while not stop_event.is_set():
            t0 = time.time()
            cfg = self.cfg
            if t0 < cooldown_until:                  # backing off after an error
                self.set_status(f"Paused {int(cooldown_until - t0)}s (rate limit / error)")
                stop_event.wait(1)
                continue
            try:
                if cfg["version"] != last_version:
                    last_version, last_img, last_text = cfg["version"], None, ""

                img = ImageGrab.grab(bbox=box)
                if last_img is None or self.changed(last_img, img):
                    text = self.ocr(loop, img, cfg)
                    if not text:
                        self.set_status("No text found in area")
                    elif difflib.SequenceMatcher(None, text, last_text).ratio() < 0.9:
                        self.set_status("Translating...")
                        translated = translate(text, cfg["dst"])
                        last_text = text
                        self.q.put(lambda a=text, b=translated: self.show(a, b))
                    last_img = img
                last_err = ""
            except Exception as e:
                last_img = None                      # retry after the pause
                cooldown_until = time.time() + 30
                msg = f"{type(e).__name__}: {e}  (pausing 30s before retry)"
                if msg != last_err:
                    last_err = msg
                    self.q.put(lambda m=msg: self.show_error(m))
            stop_event.wait(max(0.1, cfg["interval"] - (time.time() - t0)))
        loop.close()

    # ---- output ---------------------------------------------------------
    def show(self, original, translated):
        self.orig.delete("1.0", "end")
        self.orig.insert("1.0", original)
        if self.history.get():
            self.out.insert("end", translated + "\n\n")
        else:
            self.out.delete("1.0", "end")
            self.out.insert("1.0", translated)
        self.out.see("end")
        self.status.config(text="Live... (updated)")

    def show_error(self, msg):
        self.orig.delete("1.0", "end")
        self.orig.insert("1.0", "ERROR - " + msg)
        self.status.config(text="Error (see Original box)")

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.out.get("1.0", "end").strip())
        self.status.config(text="Copied")

    def clear(self):
        self.orig.delete("1.0", "end")
        self.out.delete("1.0", "end")

    def quit(self):
        self.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    App().root.mainloop()