"""Capture the landing page on mobile (390x844) so we can verify mobile-first works."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_landing"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  device_scale_factor=2, color_scheme="dark", is_mobile=True)
        page = ctx.new_page()
        page.goto("http://127.0.0.1:5000/landing.html", wait_until="commit", timeout=45000)
        page.wait_for_timeout(4500)

        # Section-by-section
        sections = ["hero", "live", "reel", "engine", "surfaces", "forensics", "pricing", "waitlist"]
        for s in sections:
            try:
                page.evaluate(f"document.getElementById('{s}')?.scrollIntoView({{block:'start'}})")
                page.wait_for_timeout(800)
                out = OUT / f"mobile-{s}.png"
                page.screenshot(path=str(out), full_page=False)
                print(f"  OK {s:<10} -> {out.name} ({out.stat().st_size//1024}kb)")
            except Exception as e:
                print(f"  FAIL {s} {type(e).__name__}: {str(e)[:80]}")
        # Full mobile page
        page.goto("http://127.0.0.1:5000/landing.html", wait_until="commit", timeout=45000)
        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "mobile-FULL.png"), full_page=True)
        print(f"  OK mobile FULL -> mobile-FULL.png ({(OUT / 'mobile-FULL.png').stat().st_size//1024}kb)")
        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
