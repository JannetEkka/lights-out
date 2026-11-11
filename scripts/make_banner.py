"""Render docs/banner.png (1200x600): a thermal-paper receipt, one red accent (red = still billing)."""
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 600
BG, PAPER, INK, FAINT, RED = (38, 36, 33), (246, 243, 235), (30, 30, 28), (130, 126, 118), (200, 32, 32)
F = "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-{}.ttf"
mono = ImageFont.truetype(F.format("Regular"), 19)
bold = ImageFont.truetype(F.format("Bold"), 19)
head = ImageFont.truetype(F.format("ExtraBold"), 50)
sub = ImageFont.truetype(F.format("Regular"), 22)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# headline, left
d.text((60, 150), "Done shouldn't", font=head, fill=PAPER)
d.text((60, 210), "mean still", font=head, fill=PAPER)
d.text((60, 270), "billing.", font=head, fill=RED)
d.text((62, 360), "Lights Out for Hermes Agent", font=sub, fill=(200, 196, 186))
d.text((62, 395), "hermes plugins install lights-out", font=mono, fill=FAINT)

# receipt, right, with a torn bottom edge
x0, y0, x1, y1 = 600, 50, 1150, 560
d.rectangle((x0, y0, x1, y1), fill=PAPER)
for i in range(x0, x1 - 13, 14):
    d.polygon([(i, y1), (i + 7, y1 + 10), (i + 14, y1)], fill=PAPER)
lines = [   # values from the measured run: bench/runs/C/t04-alb and t09-keep-demo
    ("──── LIGHTS OUT · session receipt ────", INK, mono),
    ("STILL RUNNING                  $/month", INK, bold),
    ("  i-063c0ce119c3f2f9d t3.micro    7.59", RED, mono),
    ("  i-771cd5f963a2c4054 t3.micro    7.59", RED, mono),
    ("  my-alb         elb application 16.43", RED, mono),
    ("KEPT ON PURPOSE (until 15 Dec 23:59)", INK, bold),
    ("  i-ba1100e2f8f795add t3.small", INK, mono),
    ("                   ~17.74 by then", INK, mono),
    ("UNKNOWN (check by hand)      0 items", INK, mono),
    ("──────────────────────────────────────", INK, mono),
    ("Billing now: $31.61/month", RED, bold),
    ("→ /lights out  to turn them off", INK, mono),
    ("prices: AWS us-east-1 on-demand", FAINT, mono),
    ("read 2026-11-10", FAINT, mono),
]
y = y0 + 34
for text, color, font in lines:
    d.text((x0 + 34, y), text, font=font, fill=color)
    y += 33
img.save("docs/banner.png", optimize=True)
print("docs/banner.png", img.size)
