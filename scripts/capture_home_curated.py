"""Capture home page focused on the top-signals sidebar + suggested-stocks section."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_audit"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme="dark", device_scale_factor=1)
        ctx.add_init_script("""
            try {
              localStorage.setItem('sebi_ack_v1', '1');
              localStorage.setItem('ea_skipped_onboarding', '1');
              localStorage.setItem('tw-theme', 'dark');
              localStorage.setItem('tw_disclaimer_seen', '1');
              sessionStorage.setItem('tw_clock_dismissed', '1');
            } catch(e){}
        """)
        page = ctx.new_page()
        page.goto("http://127.0.0.1:5000/index.html", wait_until="commit", timeout=30000)
        page.wait_for_timeout(20000)  # let dashboard fully load + curated.fetch resolve

        # Top of page (sidebar visible)
        out1 = OUT / "home-top.png"
        page.screenshot(path=str(out1), full_page=False)
        print(f"  OK home-top -> {out1.name} ({out1.stat().st_size//1024}kb)")

        # Show suggested + sidebar — full page so we can see everything
        out2 = OUT / "home-full.png"
        page.screenshot(path=str(out2), full_page=True)
        print(f"  OK home-full -> {out2.name} ({out2.stat().st_size//1024}kb)")

        # Zoom into one suggested-stock card
        page.evaluate("document.getElementById('suggestedSection')?.scrollIntoView({block:'start'})")
        page.wait_for_timeout(1500)
        # Hover one chart so we can see the crosshair tooltip
        canvases = page.query_selector_all("canvas[id^='sugChart_']")
        if canvases:
            box = canvases[0].bounding_box()
            if box:
                page.mouse.move(box['x'] + box['width'] * 0.6, box['y'] + box['height'] * 0.4)
                page.wait_for_timeout(800)
        out3 = OUT / "home-suggested-hover.png"
        page.screenshot(path=str(out3), full_page=False, clip={
            'x': 0, 'y': 0, 'width': 1440, 'height': 900
        })
        print(f"  OK home-suggested-hover -> {out3.name} ({out3.stat().st_size//1024}kb)")

        # Zoom into one card at higher detail
        try:
            page.wait_for_selector('.curated-card:not(.curated-card--dense)', timeout=15000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  wait_for_selector failed: {e}")
        card = page.query_selector('.curated-card:not(.curated-card--dense)')
        if card:
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)
            box = card.bounding_box()
            if box and box['height'] > 0:
                out4 = OUT / "card-zoom.png"
                page.screenshot(path=str(out4), clip={
                    'x': max(0, box['x'] - 8),
                    'y': max(0, box['y'] - 8),
                    'width':  min(1440 - max(0, box['x'] - 8), box['width']  + 16),
                    'height': min(900  - max(0, box['y'] - 8), box['height'] + 16),
                })
                print(f"  OK card-zoom -> {out4.name} ({out4.stat().st_size//1024}kb)")
            else:
                print(f"  SKIP card-zoom (no box)")
        else:
            print(f"  SKIP card-zoom (no .curated-card found)")

        # Probe what curated returned
        diag = page.evaluate("""
            (async () => {
              const c = await CuratedSignals.fetch(5);
              return c.map(s => ({
                ticker: s.ticker, sector: s.sector, alpha: s.alpha_score,
                sent: s.sentiment, age_h: Math.round((Date.now() - Date.parse(s.created_at))/3600000)
              }));
            })()
        """)
        print('\\nCurated slate:')
        for s in diag: print(' ', s)
        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
