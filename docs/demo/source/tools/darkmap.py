"""Turn the repo's light matplotlib route map into the dark panels used in the video.

Inverts the image, rotates hue by 180 degrees (so colours keep their hue), blends it onto the
video background (#0b1722), crops each panel's axes area, upscales 2x, and paints over the
matplotlib legend (the video draws its own). No data is redrawn.

Usage: python3 darkmap.py <repo>/outputs/full/route_map.png <demo>/assets
Needs: numpy, Pillow, matplotlib.
"""

import sys

import numpy as np
from matplotlib.colors import hsv_to_rgb, rgb_to_hsv
from PIL import Image, ImageDraw

src, out = sys.argv[1], sys.argv[2].rstrip("/") + "/"
im = np.asarray(Image.open(src).convert("RGB")).astype(float) / 255
hsv = rgb_to_hsv(1 - im)
hsv[..., 0] = (hsv[..., 0] + 0.5) % 1.0
bg = np.array([0x0B, 0x17, 0x22]) / 255
dark = bg + hsv_to_rgb(hsv) * (1 - bg)
img = Image.fromarray((dark * 255).clip(0, 255).astype(np.uint8))
# axes areas of the two panels in the 1612x780 figure written by fleet_routing/plot.py
for name, box in {"map-sweep.png": (98, 106, 822, 712), "map-ortools.png": (866, 106, 1590, 712)}.items():
    c = img.crop(box)
    c = c.resize((c.width * 2, c.height * 2), Image.LANCZOS)
    if name == "map-sweep.png":  # remove the matplotlib legend (bottom-left)
        ImageDraw.Draw(c).rectangle([0, 1100, 600, c.height], fill=(0x0B, 0x17, 0x22))
    c.save(out + name)
    print(out + name, c.size)
end = Image.open(out + "map-ortools.png")
end.resize((724, 606), Image.LANCZOS).save(out + "map-ortools-end.png")
