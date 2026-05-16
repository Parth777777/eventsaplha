"""Apply the official TickWave logo (shared/img/logo.jpeg) consistently
across every page in app/.

Three changes per file:
  1. Inject favicon + apple-touch-icon + manifest links into <head> if missing.
  2. Replace the sidebar text brand with the logo image.
  3. Replace the auth-page / onboarding text brand with the logo image.

Idempotent: running twice is a no-op (presence of `tw-favicon` / `sidebar-logo`
markers is detected).
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")
LOGO_SRC = "./shared/img/logo.jpeg"
MANIFEST_SRC = "./manifest.json"

FAVICON_BLOCK = (
    f'<link rel="icon" type="image/jpeg" href="{LOGO_SRC}" data-tw-favicon>\n'
    f'    <link rel="apple-touch-icon" href="{LOGO_SRC}">\n'
    f'    <link rel="manifest" href="{MANIFEST_SRC}">'
)

# Sidebar brand: keep the <p class="sidebar-brand-sub"> subtitle (varies per page).
SIDEBAR_BRAND_RE = re.compile(
    r'<h1\s+class="sidebar-brand">\s*Tickwave\s*</h1>',
    re.IGNORECASE,
)
SIDEBAR_LOGO_TAG = (
    f'<img class="sidebar-logo" src="{LOGO_SRC}" alt="TickWave" '
    f'width="120" height="120" data-tw-logo>'
)

# Auth pages (login.html, signup.html).
AUTH_BRAND_RE = re.compile(
    r'<h1\s+class="text-3xl font-black font-headline text-primary mb-2">\s*Tickwave\s*</h1>',
    re.IGNORECASE,
)
AUTH_LOGO_TAG = (
    f'<img class="auth-logo mx-auto mb-2" src="{LOGO_SRC}" alt="TickWave" '
    f'width="96" height="96" data-tw-logo>'
)

# Onboarding header: emoji + span. The logo already includes the chart mark,
# so drop the emoji too.
ONBOARDING_BRAND_RE = re.compile(
    r'<span style="font-size:28px;">\s*📈\s*</span>\s*'
    r'<span style="font-family:\'Plus Jakarta Sans\'; font-weight:900; font-size:22px; color:#4ee6b8;">\s*Tickwave\s*</span>',
    re.IGNORECASE,
)
ONBOARDING_LOGO_TAG = (
    f'<img class="onboarding-logo" src="{LOGO_SRC}" alt="TickWave" '
    f'width="40" height="40" style="border-radius:8px;" data-tw-logo>'
)

SKIP_FILES = {"index.html.backup"}


def inject_favicon(src: str) -> str:
    if "data-tw-favicon" in src:
        return src
    # Insert right after the <title>...</title> line.
    title_match = re.search(r"(<title>.*?</title>)", src, re.IGNORECASE | re.DOTALL)
    if not title_match:
        # Fall back: insert before </head>.
        return src.replace("</head>", f"    {FAVICON_BLOCK}\n</head>", 1)
    insert_at = title_match.end()
    return src[:insert_at] + f"\n    {FAVICON_BLOCK}" + src[insert_at:]


def replace_sidebar_brand(src: str) -> str:
    if 'class="sidebar-logo"' in src:
        return src
    return SIDEBAR_BRAND_RE.sub(SIDEBAR_LOGO_TAG, src)


def replace_auth_brand(src: str) -> str:
    if 'class="auth-logo' in src:
        return src
    return AUTH_BRAND_RE.sub(AUTH_LOGO_TAG, src)


def replace_onboarding_brand(src: str) -> str:
    if 'class="onboarding-logo"' in src:
        return src
    return ONBOARDING_BRAND_RE.sub(ONBOARDING_LOGO_TAG, src)


def process(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    new = src
    new = inject_favicon(new)
    new = replace_sidebar_brand(new)
    new = replace_auth_brand(new)
    new = replace_onboarding_brand(new)
    if new == src:
        return False
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return True


def main() -> None:
    changed = []
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".html") or name in SKIP_FILES:
            continue
        path = os.path.join(ROOT, name)
        if process(path):
            changed.append(name)
    print(f"Updated {len(changed)} files:")
    for name in changed:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
