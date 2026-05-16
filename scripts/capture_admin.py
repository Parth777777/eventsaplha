"""Capture /admin/status.html to confirm the data freshness page renders."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_audit"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                                  color_scheme="dark", device_scale_factor=1)
        page = ctx.new_page()
        page.goto("http://127.0.0.1:5000/admin/status.html", wait_until="commit", timeout=20000)
        page.wait_for_timeout(5000)
        out = OUT / "admin-status.png"
        page.screenshot(path=str(out), full_page=True)
        print(f"  OK admin-status -> {out.name} ({out.stat().st_size//1024}kb)")

        # Trust page too
        page.goto("http://127.0.0.1:5000/trust.html", wait_until="commit", timeout=20000)
        page.wait_for_timeout(3500)
        out2 = OUT / "trust.png"
        page.screenshot(path=str(out2), full_page=False)
        print(f"  OK trust         -> {out2.name} ({out2.stat().st_size//1024}kb)")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
