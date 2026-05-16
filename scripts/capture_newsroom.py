"""Capture events.html (Newsroom) to confirm the card layout is no longer collapsed."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_audit"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=1, color_scheme="dark")
        ctx.add_init_script("""
          try {
            localStorage.setItem('sebi_ack_v1', '1');
            localStorage.setItem('ea_skipped_onboarding', '1');
            localStorage.setItem('tw-theme', 'dark');
          } catch(e){}
        """)
        page = ctx.new_page()
        for slug, path in [("newsroom","/events.html"), ("home","/index.html")]:
            page.goto("http://127.0.0.1:5000" + path, wait_until="commit", timeout=30000)
            page.wait_for_timeout(7000)
            out = OUT / f"{slug}.png"
            page.screenshot(path=str(out), full_page=False)
            print(f"  OK {slug:<10} -> {out.name} ({out.stat().st_size//1024}kb)")
        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
