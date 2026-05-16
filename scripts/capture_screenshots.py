"""Capture screenshots of every Tickwave app page for the landing page.

Strategy:
  - Pre-seed localStorage to skip every disclaimer / onboarding gate.
  - Visit each page, wait 8 seconds for async data to land.
  - Capture both the visible viewport AND a full-page screenshot (so we have
    material to crop from when the above-the-fold is sparse).
  - Outputs land in app/shared/img/screens/ at two viewport sizes.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens"
OUT.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:5000"

# Only the pages we know carry real data locally — drop sparse ones.
PAGES = [
    ("home",       "/index.html"),
    ("explore",    "/explore.html"),
    ("events",     "/events.html"),
    ("alerts",     "/alerts.html"),
    ("map",        "/map.html"),
    ("forensics",  "/forensics.html"),
    ("fo",         "/fo.html"),
    ("policy",     "/policy.html"),
    ("ipo",        "/ipo.html"),
    ("earnings",   "/earnings.html"),
    ("portfolio",  "/portfolio.html"),
    ("sectors",    "/sectors.html"),
    ("stock",      "/stock.html?ticker=TCS"),
    ("watchlist",  "/watchlist.html"),
    ("compare",    "/compare.html?a=TCS&b=INFY"),
    ("analytics",  "/analytics.html"),
]

SIZES = [
    ("desktop", 1440, 900),
    ("mobile",  390, 844),
]


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for size_name, w, h in SIZES:
            print(f"\n=== {size_name} {w}x{h} ===")
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=2,
                                      color_scheme="dark")
            # Aggressively pre-dismiss every modal known to gate the app.
            ctx.add_init_script("""
                try {
                    localStorage.setItem('sebi_ack_v1', '1');
                    localStorage.setItem('ea_skipped_onboarding', '1');
                    localStorage.setItem('tw-theme', 'dark');
                    localStorage.setItem('tw_disclaimer_seen', '1');
                    localStorage.setItem('tw_onboarding_done', '1');
                } catch (e) {}
            """)
            page = ctx.new_page()
            for slug, path in PAGES:
                url = BASE + path
                out_v = OUT / f"{slug}-{size_name}.png"
                out_f = OUT / f"{slug}-{size_name}-full.png"
                try:
                    page.goto(url, wait_until="commit", timeout=45000)
                    # 8s is enough for the slowest async tables to populate.
                    page.wait_for_timeout(8000)
                    # Click "I understand" if it slipped past the localStorage gate.
                    try:
                        page.evaluate("""
                            const btn = document.querySelector('#sebi-disclaimer-ok, [data-action=ack]');
                            if (btn) btn.click();
                        """)
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    page.screenshot(path=str(out_v), full_page=False)
                    page.screenshot(path=str(out_f), full_page=True)
                    print(f"  OK {slug:<12} -> {out_v.name} ({out_v.stat().st_size//1024}kb) + full ({out_f.stat().st_size//1024}kb)")
                except Exception as e:
                    print(f"  FAIL {slug:<12} {type(e).__name__}: {str(e)[:60]}")
            ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
