"""Capture the rebuilt landing page at multiple scroll positions for visual review."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_landing"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=1, color_scheme="dark")
        page = ctx.new_page()
        page.goto("http://127.0.0.1:5000/landing.html", wait_until="commit", timeout=45000)
        page.wait_for_timeout(4000)  # let GSAP, marquee, screenshots load

        # Section-by-section captures
        sections = ["hero", "live", "engine", "features", "forensics", "pricing", "waitlist"]
        for s in sections:
            try:
                page.evaluate(f"document.getElementById('{s}')?.scrollIntoView({{behavior:'instant', block:'start'}})")
                page.wait_for_timeout(900)
                out = OUT / f"section-{s}.png"
                page.screenshot(path=str(out), full_page=False)
                print(f"  OK {s:<12} -> {out.name} ({out.stat().st_size//1024}kb)")
            except Exception as e:
                print(f"  FAIL {s} {type(e).__name__}: {str(e)[:80]}")

        # Full page
        page.goto("http://127.0.0.1:5000/landing.html", wait_until="commit", timeout=45000)
        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "FULL.png"), full_page=True)
        print(f"  OK FULL page -> FULL.png ({(OUT / 'FULL.png').stat().st_size//1024}kb)")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
