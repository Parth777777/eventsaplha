"""Capture one feature card in its hover state, so we can verify the overlay design."""
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
        page.wait_for_timeout(3000)

        page.evaluate("document.getElementById('features')?.scrollIntoView({block:'start'})")
        page.wait_for_timeout(1000)
        # Hover over the second feature card (Signal Scanner)
        page.hover(".lp-features-grid .lp-feat:nth-child(2)")
        page.wait_for_timeout(500)
        out = OUT / "features-hover.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"  OK hover -> {out.name} ({out.stat().st_size//1024}kb)")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
