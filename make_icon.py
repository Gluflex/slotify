"""One-off: render the step2kit mark (matching server.py's inline favicon) as a multi-resolution .ico."""
from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]
NAVY = (19, 21, 45, 255)
WHITE = (255, 255, 255, 255)

frames = []
for s in SIZES:
    scale = 8
    big = s * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(big * 14 / 64)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=r, fill=NAVY)
    w = max(3, int(big * 5 / 64))
    pts = [(big * 14 / 64, big * 42 / 64), (big * 32 / 64, big * 14 / 64), (big * 50 / 64, big * 42 / 64)]
    d.line(pts, fill=WHITE, width=w, joint="curve")
    for p in pts:
        d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=WHITE)
    d.rounded_rectangle([big * 14 / 64, big * 42 / 64, big * 50 / 64, big * 51 / 64], radius=int(big * 2 / 64), fill=WHITE)
    img = img.resize((s, s), Image.LANCZOS)
    frames.append(img)

frames[-1].save("app_icon.ico", sizes=[(f.width, f.height) for f in frames])
print("wrote app_icon.ico")
