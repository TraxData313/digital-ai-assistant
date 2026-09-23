"""Draws the plain default pictures a new home starts with.

A home's own `artwork/` overrides any of these by name. They are deliberately
nobody: a soft circle with the assistant's initial, and a quiet gradient to sit
behind the room. Run once; the results are committed beside this script.

    python artwork/make_default_art.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
TEAL = (46, 125, 120)
DIM = (110, 110, 110)


def _font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def badge(size, colour, letter="A", maskable=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, size, size], fill=colour)
    else:
        pad = size // 16
        d.ellipse([pad, pad, size - pad, size - pad], fill=colour)
    f = _font(int(size * 0.5))
    box = d.textbbox((0, 0), letter, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), letter,
           font=f, fill=(245, 245, 240))
    return img


def wallpaper(w=1920, h=1080):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    top, bottom = (28, 44, 52), (58, 92, 88)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t)
                                            for a, b in zip(top, bottom)))
    return img.filter(ImageFilter.GaussianBlur(2))


def main():
    badge(256, TEAL).save(HERE / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    badge(256, DIM).save(HERE / "icon-down.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    badge(256, TEAL).save(HERE / "portrait.png")
    wallpaper().save(HERE / "wallpaper.png")
    badge(192, TEAL).save(WEB / "icon-192.png")
    badge(512, TEAL).save(WEB / "icon-512.png")
    badge(512, TEAL, maskable=True).save(WEB / "icon-maskable-512.png")
    print("drew the defaults into", HERE, "and", WEB)


if __name__ == "__main__":
    main()
