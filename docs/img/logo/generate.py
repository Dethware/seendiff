#!/usr/bin/env python3
"""Generate the seendiff logo/favicon set from one 16x16 pixel grid.

The mark is two pixel-art chevrons facing away from each other -- the `<` `>`
of a diff, drawn as stair-stepped blocks so it stays crisp at 16px, over a soft
halo of the same green so it reads like the glowing reviewed-bar on the site.
Everything below derives from GRID, so a size is added by editing SIZES and
re-running:

    python3 docs/img/logo/generate.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent
N = 16                  # grid is 16x16 so the favicon lands on exact pixels
FG = "#a3be8c"          # --add on the site: the green the review bar glows in
BG = "#111113"          # --bg on the site, so the tile is the page's own black
RADIUS = 3 / N          # tile corner radius, as a fraction of the side
GLOW = 0.9              # blur sigma, in grid cells
GLOW_PASSES = 3         # re-composites of the blur; more passes, hotter halo

# apex on the outside, arms stepping back in: x = 2 + |y - 7| for the left
# chevron, mirrored (15 - x) for the right one. Arms stop at y=3/y=11 so the
# two chevrons keep a two-pixel gutter instead of closing into a diamond.
GRID = []
for dy in range(-4, 5):
    x, y = 2 + abs(dy), 7 + dy
    GRID += [(x, y), (N - 1 - x, y)]

SIZES = [16, 32, 48, 64, 128, 180, 256, 512, 1024]
ICO_SIZES = [16, 32, 48]


def _tile_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    box = [0, 0, size - 1, size - 1]
    d = ImageDraw.Draw(m)
    if radius:
        d.rounded_rectangle(box, radius=round(RADIUS * size), fill=255)
    else:
        d.rectangle(box, fill=255)
    return m


def draw(size, tile=True, radius=True, glow=True):
    """Rasterise the grid at `size` px. Block edges are snapped to whole
    pixels so the stair-steps stay hard even when size isn't a multiple of 16."""
    glyph = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(glyph)
    for gx, gy in GRID:
        x0, y0 = round(gx * size / N), round(gy * size / N)
        x1, y1 = round((gx + 1) * size / N), round((gy + 1) * size / N)
        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=255)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if tile:
        t = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        t.paste(BG, mask=_tile_mask(size, radius))
        img.alpha_composite(t)
    if glow:
        # one blur stacked GLOW_PASSES times *before* it meets the tile: a
        # single pass is too faint to survive the tile behind it, and blurring
        # wider just smears instead of glowing. Stacking against the tile
        # instead (alpha_composite in a loop) blends each pass into a lighter
        # backdrop and lands somewhere the SVG's feMerge doesn't.
        a = glyph.filter(ImageFilter.GaussianBlur(GLOW * size / N))
        a = a.point(lambda v: round(255 * (1 - (1 - v / 255) ** GLOW_PASSES)))
        halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        halo.paste(FG, mask=a)
        img.alpha_composite(halo)
    crisp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    crisp.paste(FG, mask=glyph)
    img.alpha_composite(crisp)
    if tile:
        # the halo spills past the rounded corners, so clip it back to the tile
        m = _tile_mask(size, radius)
        img.putalpha(Image.composite(img.getchannel("A"), m,
                                     m.point(lambda v: 255 - v)))
    return img


def svg(tile=True):
    """Same mark as draw(), as vector. The halo is one blur merged with itself
    GLOW_PASSES times *inside* the filter, so the stacking happens in the
    filter's linearRGB space -- stacking separate filtered groups instead would
    blend them in sRGB and come out dimmer than the PNGs."""
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>'
                    for x, y in GRID)
    merge = "".join('<feMergeNode in="b"/>' for _ in range(GLOW_PASSES))
    bg = f'<rect width="16" height="16" rx="3" fill="{BG}"/>' if tile else ""
    clip = ('<clipPath id="t"><rect width="16" height="16" rx="3"/></clipPath>'
            if tile else "")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
            'width="16" height="16" shape-rendering="crispEdges">'
            '<defs><filter id="g" x="-50%" y="-50%" width="200%" height="200%" '
            'color-interpolation-filters="linearRGB">'
            f'<feGaussianBlur stdDeviation="{GLOW}" result="b"/>'
            f'<feMerge>{merge}</feMerge></filter>{clip}</defs>'
            f'{bg}<g{chr(32) + chr(34).join(["clip-path=", "url(#t)", ""]) if tile else ""}>'
            f'<g fill="{FG}" filter="url(#g)">{rects}</g>'
            f'<g fill="{FG}">{rects}</g></g></svg>\n')


for n in SIZES:
    draw(n).save(OUT / f"seendiff-{n}.png")
draw(180, radius=False).save(OUT / "apple-touch-icon.png")
draw(512, tile=False).save(OUT / "seendiff-mark-512.png")
(OUT / "seendiff.svg").write_text(svg())
(OUT / "seendiff-mark.svg").write_text(svg(tile=False))
draw(64).save(OUT.parent.parent / "favicon.ico",
              sizes=[(n, n) for n in ICO_SIZES])
print(svg().strip().replace("#", "%23").replace('"', "'"))
