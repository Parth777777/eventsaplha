"""Add the shared stock-popup.js include to every page that has tickers but
isn't already including the script.

Strategy:
  - Locate the closing </body> tag (or the end of </main> if </body> is missing).
  - Insert: <script src="./shared/js/stock-popup.js" defer></script> just
    before </body>.
  - Skip pages that already reference the script.
  - Skip pages where a popup makes no sense (login/signup/onboarding/legal).

This keeps the popup behaviour identical across the app — click any ticker,
get a quick preview with a "Open full page" CTA — without retrofitting each
file by hand.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")
SCRIPT_TAG = '<script src="./shared/js/stock-popup.js" defer></script>'

# Pages where ticker popups don't apply.
SKIP = {
    "login.html", "signup.html", "onboarding.html",
    "pricing.html", "privacy.html", "terms.html", "trust.html",
    "stock.html",  # already the full page; popup script auto-skips anyway
    "index.html.backup",
}


def needs_inject(content: str) -> bool:
    return "stock-popup.js" not in content


def inject(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if not needs_inject(src):
        return False
    # Prefer to insert just before </body>.
    if "</body>" in src:
        new = src.replace("</body>", f"{SCRIPT_TAG}\n</body>", 1)
    else:
        # Fall back: append at end.
        new = src.rstrip() + "\n" + SCRIPT_TAG + "\n"
    if new == src:
        return False
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return True


def main():
    changed = []
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".html") or name in SKIP:
            continue
        p = os.path.join(ROOT, name)
        if inject(p):
            changed.append(name)
    print(f"Injected stock-popup.js into {len(changed)} pages: {', '.join(changed) or '(none)'}")


if __name__ == "__main__":
    main()
