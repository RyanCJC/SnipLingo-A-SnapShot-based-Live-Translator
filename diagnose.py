"""Run:  python diagnose.py   - tests every step and shows which one fails."""
import asyncio
import inspect
import sys
import traceback


def step(name):
    print(f"\n=== {name} ===")


def fail():
    print("FAILED:")
    traceback.print_exc()


print("Python", sys.version)

step("1. Pillow + screen capture")
try:
    from PIL import Image, ImageDraw, ImageFont, ImageGrab
    shot = ImageGrab.grab()
    print("OK, screen size:", shot.size)
except Exception:
    fail()

step("2. winocr import")
try:
    import winocr
    print("OK, winocr", getattr(winocr, "__version__", "(version unknown)"))
    print("recognize_pil is coroutine function:",
          inspect.iscoroutinefunction(winocr.recognize_pil))
except Exception:
    fail()

step("3. OCR languages installed in Windows")
tags = []
try:
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        from winsdk.windows.media.ocr import OcrEngine
    tags = [l.language_tag for l in OcrEngine.available_recognizer_languages]
    print("Available:", tags or "NONE")
    if not tags:
        print("-> Add a language: Settings > Time & language > Language & region")
except Exception:
    fail()

step("4. OCR on a generated test image")
try:
    img = Image.new("RGB", (600, 120), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((20, 30), "Hello world, this is a test", fill="black", font=font)

    r = winocr.recognize_pil(img, "en")
    if inspect.isawaitable(r):
        async def _w(a):
            return await a
        r = asyncio.run(_w(r))
    text = r.get("text") if isinstance(r, dict) else getattr(r, "text", str(r))
    print("Result type:", type(r))
    print("Text read:", repr(text))
    if not text:
        print("-> OCR ran but returned nothing. Is English installed (step 3)?")
except Exception:
    fail()

step("5a. Translation via Google gtx endpoint (used by live app)")
try:
    import json
    import urllib.parse
    import urllib.request
    url = ("https://translate.googleapis.com/translate_a/single?"
           + urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "es", "dt": "t"}))
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode({"q": "Hello world"}).encode(),
        headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    print("OK:", data[0][0][0])
except Exception:
    fail()

step("5b. Translation via deep-translator (fallback)")
try:
    from deep_translator import GoogleTranslator
    print("OK:", GoogleTranslator(source="auto", target="es").translate("Hello world"))
except Exception:
    fail()

input("\nDone. Copy everything above and send it to me. Press Enter to close.")