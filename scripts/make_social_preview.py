"""Render assets/social-preview.png, the repo's GitHub and X social card.

Run `python scripts/make_social_preview.py` from the repo root, then upload the
result under Settings > General > Social preview. GitHub exposes no API for that
field, so the upload stays manual. This script at least keeps the image
reproducible instead of leaving an unexplained binary in git.

Needs Pillow, which x-use already depends on.
"""

import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 640
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "social-preview.png")

FONT_DIRS = [
    r"C:\Windows\Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/TTF",
]

# Preferred face first, then per-platform stand-ins.
FALLBACKS = {
    "seguibl.ttf": ["Arial Black.ttf", "ariblk.ttf", "DejaVuSans-Bold.ttf"],
    "segoeuib.ttf": ["Arial Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "segoeuisl.ttf": ["Arial.ttf", "arial.ttf", "DejaVuSans.ttf"],
    "consola.ttf": ["Menlo.ttc", "DejaVuSansMono.ttf"],
    "consolab.ttf": ["Menlo.ttc", "DejaVuSansMono-Bold.ttf"],
}

BG = (9, 11, 15)
WHITE = (248, 250, 253)
MUTED = (139, 152, 172)
DIM = (94, 106, 126)
LABEL = (116, 130, 152)
BLUE = (74, 150, 255)
VIOLET = (150, 110, 255)
CYAN = (80, 214, 210)
GREEN = (122, 214, 138)
ORANGE = (235, 170, 100)
PANEL = (15, 18, 25)
STROKE = (36, 44, 57)


def _resolve(name):
    for candidate in [name] + FALLBACKS.get(name, []):
        for directory in FONT_DIRS:
            path = os.path.join(directory, candidate)
            if os.path.exists(path):
                return path
    sys.exit("No usable font found for %s. Install DejaVu or edit FONT_DIRS." % name)


def _font(name, size):
    return ImageFont.truetype(_resolve(name), size)


def black(size):
    return _font("seguibl.ttf", size)


def bold(size):
    return _font("segoeuib.ttf", size)


def light(size):
    return _font("segoeuisl.ttf", size)


def mono(size):
    return _font("consola.ttf", size)


def monob(size):
    return _font("consolab.ttf", size)


def layer():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def over(base, lyr, blur=0):
    """Composite a layer onto the canvas.

    Translucent fills must go through here rather than being drawn straight onto
    the image: ImageDraw replaces pixels instead of blending them, so an alpha
    fill drawn directly comes out fully opaque.
    """
    if blur:
        lyr = lyr.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base, lyr)


def tracked(draw, xy, text, font, fill, track):
    """Draw text with manual letter-spacing, which Pillow has no option for."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + track
    return x


def build():
    img = Image.new("RGBA", (W, H), BG + (255,))

    for (cx, cy), rad, col, alpha, blur in [
        ((120, 90), 470, BLUE, 60, 165),
        ((1030, 600), 430, VIOLET, 52, 165),
        ((980, 130), 300, CYAN, 20, 140),
    ]:
        g = layer()
        ImageDraw.Draw(g).ellipse(
            [cx - rad, cy - rad * 0.72, cx + rad, cy + rad * 0.72], fill=col + (alpha,)
        )
        img = over(img, g, blur)

    grid = layer()
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 48):
        gd.line([(x, 0), (x, H)], fill=(255, 255, 255, 9))
    for y in range(0, H, 48):
        gd.line([(0, y), (W, y)], fill=(255, 255, 255, 9))
    img = over(img, grid)

    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 3], fill=BLUE + (255,))

    x0 = 76
    tracked(d, (x0 + 2, 96), "MODEL CONTEXT PROTOCOL SERVER", mono(18), LABEL + (255,), 3.6)
    d.text((x0 - 7, 132), "x-use", font=black(150), fill=WHITE + (255,))
    d.rectangle([x0, 300, x0 + 70, 305], fill=BLUE + (255,))
    d.text((x0, 332), "Browser-native AI agents", font=bold(42), fill=WHITE + (255,))
    d.text((x0, 380), "for X (Twitter).", font=bold(42), fill=WHITE + (255,))
    d.text((x0, 432), "No API key required.", font=light(38), fill=MUTED + (255,))

    chip_box, chip_text = layer(), layer()
    cbd, ctd = ImageDraw.Draw(chip_box), ImageDraw.Draw(chip_text)
    cf, track = monob(17), 1.5
    cx = x0
    for label, col in [("32 MCP TOOLS", BLUE), ("MULTI-ACCOUNT", CYAN), ("MIT", GREEN)]:
        width = sum(ctd.textlength(c, font=cf) + track for c in label) - track
        box = width + 32
        cbd.rounded_rectangle(
            [cx, 496, cx + box, 536], radius=9, fill=col + (30,), outline=col + (105,), width=1
        )
        tracked(ctd, (cx + 16, 507), label, cf, col + (255,), track)
        cx += box + 13
    img = over(over(img, chip_box), chip_text)
    d = ImageDraw.Draw(img)

    d.text((x0, 574), "$", font=monob(27), fill=BLUE + (255,))
    d.text((x0 + 25, 574), " pip install x-use-mcp", font=monob(27), fill=WHITE + (255,))

    tx0, ty0, tx1, ty1 = 712, 146, 1216, 498

    shadow = layer()
    ImageDraw.Draw(shadow).rounded_rectangle(
        [tx0 + 4, ty0 + 18, tx1 + 4, ty1 + 22], radius=16, fill=(0, 0, 0, 170)
    )
    img = over(img, shadow, 24)
    d = ImageDraw.Draw(img)

    d.rounded_rectangle(
        [tx0, ty0, tx1, ty1], radius=15, fill=PANEL + (255,), outline=STROKE + (255,), width=1
    )
    d.line([(tx0 + 1, ty0 + 46), (tx1 - 1, ty0 + 46)], fill=STROKE + (255,))
    for i, col in enumerate([(255, 96, 88), (255, 190, 47), (40, 202, 64)]):
        d.ellipse([tx0 + 21 + i * 21, ty0 + 18, tx0 + 31 + i * 21, ty0 + 28], fill=col + (255,))
    d.text((tx0 + 104, ty0 + 14), "claude_desktop_config.json", font=mono(16), fill=DIM + (255,))

    rows = [
        [("{", DIM)],
        [('  "mcpServers"', CYAN), (": {", DIM)],
        [('    "x-use"', CYAN), (": {", DIM)],
        [('      "command"', MUTED), (": ", DIM), ('"x-use"', ORANGE), (",", DIM)],
        [('      "args"', MUTED), (": [", DIM), ('"mcp"', ORANGE), ("]", DIM)],
        [("    }", DIM)],
        [("  }", DIM)],
        [("}", DIM)],
    ]
    mf = mono(20)
    y = ty0 + 66
    for row in rows:
        x = tx0 + 30
        for text, col in row:
            d.text((x, y), text, font=mf, fill=col + (255,))
            x += d.textlength(text, font=mf)
        y += 27

    d.line([(tx0 + 30, ty1 - 62), (tx1 - 30, ty1 - 62)], fill=STROKE + (255,))
    halo = layer()
    ImageDraw.Draw(halo).ellipse([tx0 + 26, ty1 - 46, tx0 + 46, ty1 - 26], fill=GREEN + (110,))
    img = over(img, halo, 5)
    d = ImageDraw.Draw(img)
    d.ellipse([tx0 + 31, ty1 - 41, tx0 + 41, ty1 - 31], fill=GREEN + (255,))
    d.text((tx0 + 53, ty1 - 43), "32 tools connected", font=mono(18), fill=MUTED + (255,))
    d.text((tx1 - 302, H - 46), "github.com/ihuzaifashoukat/x-use", font=mono(18), fill=DIM + (255,))

    random.seed(11)
    noise = layer()
    px = noise.load()
    for _ in range(30000):
        x, y = random.randrange(W), random.randrange(H)
        v = random.randrange(140, 255)
        px[x, y] = (v, v, v, random.randrange(3, 10))
    img = over(img, noise)

    vignette = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vignette).ellipse([-420, -360, W + 420, H + 360], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(200))
    return Image.composite(img, Image.new("RGBA", (W, H), (0, 0, 0, 255)), vignette)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    build().convert("RGB").save(OUT, quality=96)
    print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
