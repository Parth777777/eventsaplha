"""Diagnose why the home page's unified reco section is empty."""
from playwright.sync_api import sync_playwright

def diag():
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
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append("[console.error] " + m.text))
        page.goto("http://127.0.0.1:5000/index.html", wait_until="commit", timeout=30000)
        page.wait_for_timeout(15000)
        diag = page.evaluate("""
            (async () => {
              const recoCards = document.getElementById('recoCards');
              const recoSection = document.getElementById('recoSection');
              const tabs = document.querySelectorAll('#recoTabs .reco-tab');
              let curatedLen = -1;
              if (window.CuratedSignals) {
                try { curatedLen = (await CuratedSignals.fetch(5)).length; } catch (e) { curatedLen = 'err:' + e.message; }
              }
              return {
                hasCuratedSignals: !!window.CuratedSignals,
                recoSectionExists: !!recoSection,
                recoCardsExists: !!recoCards,
                tabsCount: tabs.length,
                curatedLen,
                recoCardsHTMLstart: recoCards ? recoCards.innerHTML.slice(0, 220) : null,
                initFnExists: typeof window.initUnifiedRecoTabs === 'function',
              };
            })()
        """)
        print('=== Diag ===')
        for k, v in diag.items(): print(f'  {k}: {v}')
        print('\\n=== Page errors ===')
        for e in errors: print('  -', e)
        ctx.close(); browser.close()

if __name__ == "__main__":
    diag()
