import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRAPER = os.path.join(ROOT, "scraper")
BACKEND = os.path.join(ROOT, "backend")

for p in (ROOT, SCRAPER, BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)
