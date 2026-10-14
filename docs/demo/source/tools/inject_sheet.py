"""Inline the exported dispatch sheet into compositions/value.html (renders cannot fetch files).

Usage: python3 tools/inject_sheet.py assets/dispatch_sheet.json compositions/value.html \
         --rows 12,20,33,45,52,54 --animate 20,45,54,52
"""

import argparse
import json
import re

ap = argparse.ArgumentParser()
ap.add_argument("sheet")
ap.add_argument("html")
ap.add_argument("--rows", required=True, help="vehicle ids shown as sheet rows, in order")
ap.add_argument("--animate", required=True, help="vehicle ids driven in the 2D scene (max 4)")
a = ap.parse_args()

with open(a.sheet) as fh:
    d = json.load(fh)
by_v = {t["vehicle"]: t for t in d["trucks"]}
rows = [int(v) for v in a.rows.split(",")]
anim = [int(v) for v in a.animate.split(",")]
slim = {
    "depot": d["depot"],
    "depot_xy": d["depot_xy"],
    "dispatch_day": d["dispatch_day"],
    "trucks_used": d["trucks_used"],
    "orders": d["orders"],
    "delivered": d["delivered"],
    "on_time": d["on_time"],
    "total_km": d["total_km"],
    "rows": [
        {
            k: by_v[v][k]
            for k in (
                "vehicle",
                "depart",
                "stops",
                "km",
                "return",
                "first_eta",
                "last_eta",
                "load",
                "capacity",
            )
        }
        | {"ids": [s["order_id"] for s in by_v[v]["sequence"]], "animate": anim.index(v) if v in anim else -1}
        for v in rows
    ],
    "routes": [[[s["x"], s["y"], s["eta"]] for s in by_v[v]["sequence"]] for v in anim],
}
with open(a.html) as fh:
    html = fh.read()
block = "/* SHEET:BEGIN */ const SHEET = " + json.dumps(slim, separators=(",", ":")) + "; /* SHEET:END */"
html, n = re.subn(r"/\* SHEET:BEGIN \*/.*?/\* SHEET:END \*/", lambda _: block, html, flags=re.S)
assert n == 1, "marker not found"
with open(a.html, "w") as fh:
    fh.write(html)
print("injected", len(slim["rows"]), "rows,", len(slim["routes"]), "routes")
