"""Capture just the unified recommendations section after a slow, single-pass load."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_audit"

def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme="dark")
        ctx.add_init_script("""
            try {
              localStorage.setItem('sebi_ack_v1', '1');
              localStorage.setItem('ea_skipped_onboarding', '1');
              localStorage.setItem('tw-theme', 'dark');
              sessionStorage.setItem('tw_clock_dismissed', '1');
            } catch(e){}
        """)
        page = ctx.new_page()
        page.goto("http://127.0.0.1:5000/index.html", wait_until="commit", timeout=30000)
        # Wait for first card to render (signals reco-card-wrap presence)
        page.wait_for_selector('.reco-card-wrap, #recoCards .skeleton', state='attached', timeout=15000)
        page.wait_for_timeout(8000)
        # Make sure the section is in view
        page.evaluate("document.getElementById('recoSection')?.scrollIntoView({block:'start'})")
        page.wait_for_timeout(3000)
        out = OUT / "reco-default.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"  OK reco-default -> {out.name} ({out.stat().st_size//1024}kb)")

        # Click Pre-movers tab
        pm = page.query_selector('button[data-reco-tab="premover"]')
        if pm:
            pm.click()
            page.wait_for_timeout(5000)
            out2 = OUT / "reco-premover.png"
            page.screenshot(path=str(out2), full_page=False)
            print(f"  OK reco-premover -> {out2.name} ({out2.stat().st_size//1024}kb)")

        # Click News catalyst tab
        nc = page.query_selector('button[data-reco-tab="news"]')
        if nc:
            nc.click()
            page.wait_for_timeout(5000)
            out3 = OUT / "reco-news.png"
            page.screenshot(path=str(out3), full_page=False)
            print(f"  OK reco-news -> {out3.name} ({out3.stat().st_size//1024}kb)")

        ctx.close(); browser.close()

if __name__ == "__main__":
    capture()
