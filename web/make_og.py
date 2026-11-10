"""Imagen de vista previa para redes (docs/og.png, 1200x630) a partir de un fotograma del video.

  python butterfly.py --stills 0 --no-audio --out out/preview/x.mp4
  python web/make_og.py out/preview/still_00.00s.png
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (5, 6, 13)
INK = (236, 234, 242)
MUTED = (141, 149, 180)
here = os.path.dirname(os.path.abspath(__file__))
fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def font(name, size, index=0):
    p = os.path.join(fonts, name)
    return ImageFont.truetype(p, size, index=index) if os.path.exists(p) else ImageFont.load_default(size)


still = Image.open(sys.argv[1]).convert("RGB")
side = still.width
# La mariposa ocupa la parte alta del fotograma; el texto del video, la baja.
fan = still.crop((0, int(0.02 * side), side, int(0.68 * side)))
fan = fan.resize((int(fan.width * H / fan.height), H), Image.LANCZOS)

canvas = Image.new("RGB", (W, H), BG)
canvas.paste(fan, (-90, 0))
# Se funde hacia la derecha, donde va el texto.
a = np.asarray(canvas, dtype=np.float32)
x = np.linspace(0, 1, W, dtype=np.float32)[None, :, None]
fade = np.clip((x - 0.50) / 0.22, 0, 1) ** 1.5
a = a * (1 - fade) + np.array(BG, dtype=np.float32) * fade
canvas = Image.fromarray(a.astype(np.uint8))

d = ImageDraw.Draw(canvas)
serif = font("Sitka.ttc", 62, 4)
label = font("bahnschrift.ttf", 21)
x0, y = 690, 150
d.text((x0, y), "EFECTO MARIPOSA", font=label, fill=MUTED, spacing=4)
y += 52
for line in ("Basta un aleteo", "para que el mañana", "sea otro."):
    d.text((x0, y), line, font=serif, fill=INK)
    y += 76
d.text((x0, y + 26), "chaos.meruw.dev", font=label, fill=MUTED)

out = os.path.normpath(os.path.join(here, "..", "docs", "og.png"))
canvas.save(out, optimize=True)
print("escrito", out, os.path.getsize(out) // 1024, "KB")
