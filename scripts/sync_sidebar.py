"""Normalize the <nav class="sidebar-nav"> block on every app/*.html page.

Every page lists the same canonical sidebar so the side menu is identical
site-wide. Links are grouped under collapsible section headers ("MARKETS",
"NEWS", "TOOLS", "INSIGHTS") to keep the nav scannable instead of a flat
20-item wall. The "active" class is applied only to the link matching the
current page.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")

# Sidebar structure:
#   - Home is pinned at the top with no header.
#   - Each subsequent group has a heading rendered as <div class="sidebar-section">.
#   - Quant Portfolio was removed — too jargon-heavy for the simplified FTUE.
SECTIONS = [
    (None, [
        ("index", "home", "Home"),
    ]),
    ("Markets", [
        ("explore",     "explore",     "Explore"),
        ("sectors",     "grid_view",   "Sectors"),
        ("map",         "map",         "Heatmap"),
        ("global",      "public",      "Global"),
        ("commodities", "inventory_2", "Commodities"),
    ]),
    ("News", [
        ("events",   "newspaper",         "Newsroom"),
        ("earnings", "event_note",        "Earnings"),
        ("ipo",      "rocket_launch",     "IPOs"),
        ("fo",       "candlestick_chart", "F&O"),
        ("ma",       "handshake",         "M&A"),
    ]),
    ("Tools", [
        ("screeners", "filter_alt",           "Screeners"),
        ("compare",   "compare_arrows",       "Compare"),
        ("watchlist", "star",                 "Watchlist"),
        ("alerts",    "notifications_active", "Alerts"),
    ]),
    ("Insights", [
        ("analytics", "insights",  "Analytics"),
        ("forensics", "policy",    "Forensics"),
        ("trust",     "verified",  "Trust"),
    ]),
]

# Pages that have a sidebar (login/signup/onboarding don't).
PAGES = [
    "alerts.html", "analytics.html", "commodities.html", "compare.html",
    "earnings.html", "events.html", "explore.html", "fo.html",
    "forensics.html",
    "global.html", "index.html", "ipo.html", "ma.html", "map.html",
    "paper.html", "policy.html",
    "premover.html", "pricing.html", "privacy.html",
    "screeners.html", "sectors.html", "simulator.html", "social.html",
    "stock.html", "terms.html", "trust.html", "watchlist.html",
]

NAV_RE = re.compile(r'(<nav class="sidebar-nav">)([\s\S]*?)(</nav>)', re.MULTILINE)


def build_nav(active_slug: str, indent: str = "  ") -> str:
    lines = []
    for section, links in SECTIONS:
        if section is not None:
            lines.append(f'{indent}<div class="sidebar-section">{section}</div>')
        for slug, icon, label in links:
            cls = "sidebar-link active" if slug == active_slug else "sidebar-link"
            lines.append(
                f'{indent}<a class="{cls}" href="{slug}.html">'
                f'<span class="material-symbols-outlined sidebar-icon">{icon}</span>'
                f'{label}</a>'
            )
    return "\n" + "\n".join(lines) + "\n"


def page_active_slug(filename: str) -> str:
    slug = filename.replace(".html", "")
    # stock.html and a few deep pages have no matching sidebar link — leave nothing active.
    return slug if slug not in ("stock", "paper", "policy", "premover",
                                 "pricing", "privacy", "simulator", "social",
                                 "terms", "portfolio") else ""


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
