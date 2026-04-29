"""Normalize the <nav class="sidebar-nav"> block on every app/*.html page.

Every page must list the same canonical 18 sidebar links so the side menu is
identical site-wide. The "active" class is applied only to the link matching
the page itself. bootstrap.js then groups the links under section headers.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")

# Canonical link table: (href slug, icon, label). Order is irrelevant — bootstrap.js
# physically reorders by section, but we keep section order here for readability.
LINKS = [
    # Markets
    ("index",       "home",                    "Home"),
    ("explore",     "explore",                 "Explore"),
    ("sectors",     "grid_view",               "Sectors"),
    ("map",         "map",                     "Map"),
    ("global",      "public",                  "Global Markets"),
    ("commodities", "inventory_2",             "Commodities"),
    # Research
    ("compare",     "compare_arrows",          "Compare"),
    ("screeners",   "filter_alt",              "Screeners"),
    ("earnings",    "event_note",              "Earnings"),
    ("events",      "event",                   "Events"),
    # Activity
    ("watchlist",   "star",                    "Watchlist"),
    ("alerts",      "notifications_active",    "Alerts"),
    ("portfolio",   "account_balance_wallet",  "Portfolio"),
    ("paper",       "credit_score",            "Paper Trading"),
    ("simulator",   "calculate",               "Simulator"),
    # Insights
    ("analytics",   "insights",                "Analytics"),
    ("social",      "forum",                   "Social"),
    ("trust",       "verified",                "Trust"),
]

# Pages that have a sidebar (login/signup/onboarding don't).
PAGES = [
    "alerts.html", "analytics.html", "commodities.html", "compare.html",
    "earnings.html", "events.html", "explore.html", "global.html",
    "index.html", "map.html", "paper.html", "portfolio.html",
    "screeners.html", "sectors.html", "simulator.html", "social.html",
    "stock.html", "trust.html", "watchlist.html",
]

NAV_RE = re.compile(r'(<nav class="sidebar-nav">)([\s\S]*?)(</nav>)', re.MULTILINE)


def build_nav(active_slug: str, indent: str = "  ") -> str:
    lines = []
    for slug, icon, label in LINKS:
        cls = "sidebar-link active" if slug == active_slug else "sidebar-link"
        lines.append(
            f'{indent}<a class="{cls}" href="{slug}.html">'
            f'<span class="material-symbols-outlined sidebar-icon">{icon}</span>'
            f'{label}</a>'
        )
    return "\n" + "\n".join(lines) + "\n"


def page_active_slug(filename: str) -> str:
    slug = filename.replace(".html", "")
    # stock.html is a deep page with no matching sidebar link — leave nothing active.
    return slug if slug != "stock" else ""


def sync(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    m = NAV_RE.search(src)
    if not m:
        return False
    active = page_active_slug(os.path.basename(path))
    # Match indentation of the opening <nav> so the file stays tidy.
    line_start = src.rfind("\n", 0, m.start()) + 1
    indent = src[line_start:m.start()] + "  "
    new_block = m.group(1) + build_nav(active, indent) + indent[:-2] + m.group(3)
    new_src = src[:m.start()] + new_block + src[m.end():]
    if new_src == src:
        return False
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_src)
    return True


def main():
    changed = []
    for name in PAGES:
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            continue
        if sync(p):
            changed.append(name)
    print(f"Updated {len(changed)} pages: {', '.join(changed)}")


if __name__ == "__main__":
    main()
