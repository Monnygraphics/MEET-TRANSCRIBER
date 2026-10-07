"""Generate a branded app icon (icon.ico) — gradient tile + white microphone."""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "icon.ico")

S = 256
TOP = (91, 140, 255)      # #5b8cff
BOT = (123, 91, 255)      # #7b5bff


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


# Vertical gradient background.
bg = Image.new("RGB", (S, S), TOP)
px = bg.load()
for y in range(S):
    t = y / (S - 1)
    c = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
    for x in range(S):
        px[x, y] = c

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(bg, (0, 0), rounded_mask(S, 56))

d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)

# Microphone capsule (body).
bx0, by0, bx1, by1 = 100, 54, 156, 150
d.rounded_rectangle([bx0, by0, bx1, by1], radius=28, fill=W)

# U-shaped holder arc beneath the body.
ax0, ay0, ax1, ay1 = 82, 92, 174, 178
d.arc([ax0, ay0, ax1, ay1], start=20, end=160, fill=W, width=12)

# Stand pole + base.
cx = S // 2
d.line([cx, 172, cx, 196], fill=W, width=12)
d.rounded_rectangle([cx - 30, 196, cx + 30, 208], radius=6, fill=W)

# Save multi-resolution .ico.
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
img.save(OUT, format="ICO", sizes=sizes)
print("Wrote", OUT)
