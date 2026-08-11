"""
card.py — renders the PNG "tree card" attached to /tree and growth
announcements. Pure rendering: no network/Discord calls in here, so it can
be unit-tested by just calling render_tree_card() and saving the result.

Tree art is procedural (circles/polygons), not static image assets, so it
scales smoothly to any level instead of jumping between a fixed set of
pre-drawn pictures.
"""

import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1000, 400
FONT_DIR = "/usr/share/fonts/truetype/google-fonts"


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{FONT_DIR}/Poppins-{weight}.ttf", size)


F_TITLE = _font("Bold", 40)
F_SUB = _font("Medium", 22)
F_LABEL = _font("Medium", 18)
F_STAT = _font("Bold", 30)
F_SMALL = _font("Regular", 16)

# ---------- Tier palettes ----------
# (min_level, name, foliage_colors[light->dark], trunk_color, glow_color)
TIERS = [
    (0,    "No Tree Yet",         [(90, 90, 90)],                         (70, 60, 55),  (60, 60, 60)),
    (1,    "Seedling",            [(140, 220, 120), (100, 190, 90)],      (110, 80, 55), (120, 220, 110)),
    (10,   "Sprout",              [(120, 210, 110), (80, 175, 80)],       (105, 75, 50), (100, 220, 100)),
    (50,   "Sapling",             [(90, 190, 90), (55, 150, 70)],         (100, 70, 48), (80, 210, 100)),
    (150,  "Young Tree",          [(70, 170, 90), (35, 130, 65)],         (95, 65, 45),  (60, 200, 110)),
    (400,  "Mature Tree",         [(45, 140, 90), (20, 100, 65)],         (85, 58, 42),  (40, 190, 130)),
    (800,  "Ancient Tree",        [(35, 120, 95), (15, 80, 65)],          (75, 50, 38),  (255, 200, 80)),
    (1000, "MAX — Legendary Tree",[(255, 210, 90), (255, 150, 60), (30, 100, 80)], (110, 70, 40), (255, 215, 100)),
]


def get_tier(level: int):
    current = TIERS[0]
    for t in TIERS:
        if level >= t[0]:
            current = t
        else:
            break
    return current


# ---------- helpers ----------

def _rounded_bg(w, h, radius, top, bottom):
    """Vertical gradient background clipped to a rounded rect."""
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        f = y / max(h - 1, 1)
        r = round(top[0] + (bottom[0] - top[0]) * f)
        g = round(top[1] + (bottom[1] - top[1]) * f)
        b = round(top[2] + (bottom[2] - top[2]) * f)
        grad.putpixel((0, y), (r, g, b))
    grad = grad.resize((w, h))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _blob(draw, cx, cy, r, color, wobble=0.22, points=14, seed=0):
    rnd = random.Random(seed)
    pts = []
    for i in range(points):
        ang = 2 * math.pi * i / points
        rad = r * (1 + rnd.uniform(-wobble, wobble))
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang) * 0.9))
    draw.polygon(pts, fill=color)


def _draw_tree(canvas: Image.Image, cx: int, base_y: int, level: int, max_level: int, seed: int):
    """Draws a procedural tree scaled by level. Bigger level -> taller trunk,
    more foliage clusters, wider canopy, richer color."""
    tier = get_tier(level)
    foliage_colors = tier[2]
    trunk_color = tier[3]

    if level <= 0:
        # bare seed on the ground
        draw = ImageDraw.Draw(canvas)
        draw.ellipse([cx - 10, base_y - 16, cx + 10, base_y], fill=(120, 90, 55))
        return

    growth = min(level, max_level) / max_level  # 0..1
    scale = 0.35 + growth * 1.05  # visual scale factor

    trunk_h = int(70 * scale) + 20
    trunk_w_base = 10 + growth * 22
    trunk_w_top = trunk_w_base * 0.45
    canopy_r = 46 + growth * 150

    draw = ImageDraw.Draw(canvas, "RGBA")

    # ground shadow
    shadow_w = canopy_r * 1.1
    draw.ellipse(
        [cx - shadow_w, base_y - 8, cx + shadow_w, base_y + 14],
        fill=(0, 0, 0, 70),
    )

    # trunk (tapered polygon), slight curve for taller trees
    top_x = cx + (12 if growth > 0.5 else 0)
    trunk_pts = [
        (cx - trunk_w_base / 2, base_y),
        (cx + trunk_w_base / 2, base_y),
        (top_x + trunk_w_top / 2, base_y - trunk_h),
        (top_x - trunk_w_top / 2, base_y - trunk_h),
    ]
    draw.polygon(trunk_pts, fill=trunk_color)

    # branches for bigger trees
    if growth > 0.15:
        n_branches = 2 + int(growth * 4)
        rnd = random.Random(seed)
        for i in range(n_branches):
            frac = 0.3 + 0.6 * (i / max(n_branches - 1, 1))
            by = base_y - trunk_h * frac
            bx = top_x + (top_x - cx) * frac
            direction = 1 if i % 2 == 0 else -1
            blen = trunk_w_base * (1.6 + growth) * direction
            draw.line(
                [(bx, by), (bx + blen, by - abs(blen) * 0.5)],
                fill=trunk_color,
                width=max(2, int(trunk_w_top * 0.5)),
            )

    # canopy: layered wobbly blobs, using every color in the tier's palette
    canopy_cx, canopy_cy = top_x, base_y - trunk_h - canopy_r * 0.55
    n_clusters = 3 + int(growth * 6)
    rnd = random.Random(seed + 1)
    for i in range(n_clusters):
        ang = rnd.uniform(0, 2 * math.pi)
        dist = rnd.uniform(0, canopy_r * 0.55)
        r = canopy_r * rnd.uniform(0.42, 0.75)
        color = foliage_colors[i % len(foliage_colors)]
        _blob(
            draw,
            canopy_cx + math.cos(ang) * dist,
            canopy_cy + math.sin(ang) * dist * 0.75,
            r,
            color,
            seed=seed + i,
        )

    # sparkle accents at max tier
    if level >= max_level:
        rnd = random.Random(seed + 99)
        for _ in range(10):
            sx = canopy_cx + rnd.uniform(-canopy_r, canopy_r)
            sy = canopy_cy + rnd.uniform(-canopy_r * 0.8, canopy_r * 0.8)
            sr = rnd.uniform(2, 5)
            draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 255, 255, 220))


def _circle_avatar(avatar_bytes: bytes, size: int, ring_color) -> Image.Image:
    img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    out = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
    ring = Image.new("L", (size + 12, size + 12), 0)
    ImageDraw.Draw(ring).ellipse([0, 0, size + 12, size + 12], fill=255)
    ring_layer = Image.new("RGBA", (size + 12, size + 12), ring_color + (255,))
    out.paste(ring_layer, (0, 0), ring)
    out.paste(img, (6, 6), mask)
    return out


def _flame(draw, cx, cy, size, color=(255, 150, 40)):
    """Small drawn flame icon — avoids depending on an emoji font being
    installed (Poppins has no color-emoji glyphs, so 🔥 renders as a
    missing-glyph box)."""
    pts = [
        (cx, cy - size),
        (cx + size * 0.55, cy - size * 0.15),
        (cx + size * 0.32, cy - size * 0.15),
        (cx + size * 0.6, cy + size * 0.75),
        (cx, cy + size),
        (cx - size * 0.6, cy + size * 0.75),
        (cx - size * 0.32, cy - size * 0.15),
        (cx - size * 0.55, cy - size * 0.15),
    ]
    draw.polygon(pts, fill=color)
    draw.ellipse(
        [cx - size * 0.22, cy + size * 0.15, cx + size * 0.22, cy + size * 0.65],
        fill=(255, 220, 120),
    )


def _progress_bar(draw, x, y, w, h, frac, fg, bg=(255, 255, 255, 35)):
    frac = max(0.0, min(1.0, frac))
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    if frac > 0:
        fw = max(h, w * frac)
        draw.rounded_rectangle([x, y, x + fw, y + h], radius=h // 2, fill=fg)


def render_tree_card(
    display_name: str,
    avatar_bytes: bytes,
    level: int,
    max_level: int,
    streak_days: int,
    global_rank: int = None,
    seed: int = None,
) -> bytes:
    """Returns PNG bytes for the tree card."""
    seed = seed if seed is not None else (hash(display_name) & 0xFFFF)
    tier_min, tier_name, foliage_colors, trunk_color, glow = get_tier(level)

    bg_top = (22, 24, 28)
    bg_bottom = tuple(max(0, c // 5) for c in glow)
    card = _rounded_bg(W, H, 28, bg_top, bg_bottom)

    # soft glow behind the tree, colored per tier
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).ellipse(
        [W - 430, 20, W - 30, H - 20], fill=glow + (55,)
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(60))
    card.alpha_composite(glow_layer)

    _draw_tree(card, cx=W - 230, base_y=H - 55, level=level, max_level=max_level, seed=seed)

    draw = ImageDraw.Draw(card, "RGBA")

    # avatar + name block (left side)
    ring = tuple(min(255, c + 30) for c in glow)
    avatar_size = 96
    avatar_img = _circle_avatar(avatar_bytes, avatar_size, ring)
    card.alpha_composite(avatar_img, (40, 36))

    text_x = 40 + avatar_size + 12 + 12
    draw.text((text_x, 44), display_name, font=F_TITLE, fill=(255, 255, 255))
    draw.text((text_x, 92), tier_name, font=F_SUB, fill=tuple(min(255, c + 60) for c in glow))

    # stat row
    stat_y = 190
    draw.text((40, stat_y), "LEVEL", font=F_LABEL, fill=(160, 165, 170))
    draw.text((40, stat_y + 22), f"{level} / {max_level}", font=F_STAT, fill=(255, 255, 255))

    draw.text((230, stat_y), "STREAK", font=F_LABEL, fill=(160, 165, 170))
    _flame(draw, 246, stat_y + 40, 12)
    draw.text((264, stat_y + 22), f"{streak_days}d", font=F_STAT, fill=(255, 180, 80))

    if global_rank is not None:
        draw.text((400, stat_y), "GLOBAL RANK", font=F_LABEL, fill=(160, 165, 170))
        draw.text((400, stat_y + 22), f"#{global_rank}", font=F_STAT, fill=(120, 200, 255))

    # progress bar to next tier
    next_tier = next((t for t in TIERS if t[0] > tier_min), None)
    bar_y = 280
    draw.text((40, bar_y - 26), "PROGRESS", font=F_LABEL, fill=(160, 165, 170))
    if next_tier and level < max_level:
        frac = (level - tier_min) / (next_tier[0] - tier_min)
        _progress_bar(draw, 40, bar_y, 500, 16, frac, fg=glow + (255,))
        draw.text(
            (40, bar_y + 22),
            f"{next_tier[0] - level} more to {next_tier[1]}",
            font=F_SMALL,
            fill=(150, 155, 160),
        )
    else:
        _progress_bar(draw, 40, bar_y, 500, 16, 1.0, fg=glow + (255,))
        draw.text((40, bar_y + 22), "Max tier reached", font=F_SMALL, fill=(150, 155, 160))

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
