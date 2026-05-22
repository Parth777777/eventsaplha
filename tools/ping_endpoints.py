"""Hit the local Flask endpoints the main page calls and report status."""
import json
import sys
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

BASE = "http://127.0.0.1:5000"  # default Flask port; override with sys.argv[1]
if len(sys.argv) > 1:
    BASE = sys.argv[1].rstrip("/")

ENDPOINTS = [
    "/api/health",
    "/api/feed?limit=10",
    "/api/feed?category=policy&limit=10",
    "/api/signals?limit=10",
    "/api/status",
    "/api/market/clock",
]

print(f"\nPinging Flask at {BASE}\n")
for ep in ENDPOINTS:
    url = BASE + ep
    try:
        with urlopen(url, timeout=8) as r:
            code = r.status
            body = r.read().decode("utf-8", errors="replace")
            try:
                j = json.loads(body)
                # Get a sense of payload shape
                if isinstance(j, dict):
                    keys = list(j.keys())[:5]
                    items = j.get("data") or j.get("items") or j.get("signals") or []
                    item_count = len(items) if isinstance(items, list) else "?"
                    print(f"  [{code}]  {ep:40s} keys={keys} items={item_count}")
                elif isinstance(j, list):
                    print(f"  [{code}]  {ep:40s} list len={len(j)}")
                else:
                    print(f"  [{code}]  {ep:40s} type={type(j).__name__}")
            except Exception:
                print(f"  [{code}]  {ep:40s} (non-JSON, {len(body)} bytes)")
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  [{e.code}] {ep:40s} ERR: {body}")
    except URLError as e:
        print(f"  [---] {ep:40s} CONNECT FAIL: {e.reason}")
    except Exception as e:
        print(f"  [???] {ep:40s} {type(e).__name__}: {e}")
print()
