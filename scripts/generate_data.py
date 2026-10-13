"""Generate the seeded dataset for a profile: `make data` or `python scripts/generate_data.py --profile small`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fleet_routing.config import PROFILES
from fleet_routing.data import generate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="full")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    profile = PROFILES[args.profile]
    out = args.out or Path("data") / profile.name
    print(json.dumps(generate(profile, out), indent=2))


if __name__ == "__main__":
    main()
