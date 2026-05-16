"""Open the stock popup on the home page and capture the chart with cursor visible."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "shared" / "img" / "screens" / "_audit"
OUT.mkdir(parents=True, exist_ok=True)


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  color_scheme="dark", device_scale_factor=1)
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
        page.wait_for_timeout(8000)

        # Programmatically open the popup for a known ticker that we know has data
        page.evaluate("window.StockPopup && StockPopup.open('TCS')")
        page.wait_for_timeout(4500)  # let chart + deep sections load

        # Capture the default state (sticky crosshair on last point)
        out1 = OUT / "popup-default.png"
        page.screenshot(path=str(out1), clip={'x': 280, 'y': 60, 'width': 720, 'height': 700})
        print(f"  OK popup-default -> {out1.name} ({out1.stat().st_size//1024}kb)")

        # Hover the chart
        cvs = page.query_selector('#sp-mini-chart')
        if cvs:
            box = cvs.bounding_box()
            if box:
                # Move to ~60% along the chart
                page.mouse.move(box['x'] + box['width'] * 0.6, box['y'] + box['height'] * 0.4)
                page.wait_for_timeout(700)
                out2 = OUT / "popup-hover.png"
                page.screenshot(path=str(out2), clip={'x': 280, 'y': 60, 'width': 720, 'height': 700})
                print(f"  OK popup-hover -> {out2.name} ({out2.stat().st_size//1024}kb)")

        # Click 1D tab
        btn1d = page.query_selector('button[data-p="1D"]')
        if btn1d:
            btn1d.click()
            page.wait_for_timeout(2500)
            out3 = OUT / "popup-1d.png"
            page.screenshot(path=str(out3), clip={'x': 280, 'y': 60, 'width': 720, 'height': 700})
            print(f"  OK popup-1d -> {out3.name} ({out3.stat().st_size//1024}kb)")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    capture()
