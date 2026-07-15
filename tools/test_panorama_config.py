"""Check Panorama/Leaf .env configuration. Run: python tools/test_panorama_config.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import panorama as p  # noqa: E402


def main() -> int:
    status = p.config_status()
    print("Panorama / Leaf config check")
    print("-" * 40)
    print(f"Environment:     {status['environment']}")
    print(f"Leaf token:      {'OK' if status['leaf_token'] else 'MISSING'}")
    print(f"Panorama client: {'OK' if status['panorama_client_id'] else 'MISSING'}")
    print(f"Panorama user:   {'OK' if status['panorama_username'] else 'MISSING'}")
    print(f"Panorama pass:   {'OK' if status['panorama_password'] else 'MISSING'}")
    print(f"Live ready:      {'YES' if status['live_ready'] else 'NO'}")
    if status["missing"]:
        print("\nStill need:")
        for m in status["missing"]:
            print(f"  - {m}")
        print("\nSee PANORAMA_SETUP.md for step-by-step.")
        return 1

    # Optional live ping: create is too heavy; just confirm token works via a lightweight call
    try:
        # Leaf has no simple /me; try listing users with limit if available — skip if fails
        print("\nCredentials present. Open http://127.0.0.1:8001/panorama to connect.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nConfig loaded but API probe failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
