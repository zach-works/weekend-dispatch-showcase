"""
The unfurling-letter version of the envelope GIF.

The sequence: the pigeon peeks over the envelope, the seal glints (both exactly
as in dispatch_media.build_envelope_gif), then the seal cracks, the flap lifts
back, the folded letter slides out of the envelope and unfolds to show the
usual message. It plays once and rests on the open letter.

Everything is drawn here with Pillow (no browser), in the same typefaces as the
email (assets/fonts: Courier Prime and Special Elite, both open licences), so it
runs in the Saturday job with nothing extra installed. The picture is one GIF
that links to the dispatch; the same words are in the email's alt text and
plain-text part, so nothing depends on the animation.

Geometry is in GIF pixels (900 wide). The envelope photo is 1200 wide, so
K = 0.75 converts the photo's coordinates (dispatch_media.ENV_BOX etc.).
"""

import html as _html
import io
import math
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import dispatch_media as dm

K = 0.75                      # photo pixels -> GIF pixels
GIF_W = 900
CROP_X = (80, 820)             # the scene is drawn 900 wide; the wood beyond these columns is cut so the letter fills more of the width
FONT_DIR = "assets/fonts"
F_MONO = f"{FONT_DIR}/CourierPrime-Regular.ttf"
F_TITLE = f"{FONT_DIR}/SpecialElite-Regular.ttf"

# the email's colours
PAPER = (247, 240, 220)       # #F7F0DC
PAPER_EDGE = (212, 185, 138)  # #D4B98A
RULE = (196, 164, 106)        # #C4A46A
INK = (26, 18, 8)             # #1A1208
BODY = (42, 30, 10)           # #2A1E0A
RED = (155, 58, 26)           # #9B3A1A
MUTED = (138, 106, 58)        # #8A6A3A

LW = 656                      # letter width: a little narrower than the envelope (664) so it fits the pocket
PAD = 44


# ─────────────────────────────────────────────
# THE WORDS
# ─────────────────────────────────────────────

def plain(html_text):
    """HTML entities and tags to plain text."""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))).strip()


def letter_content(saturday, sunday, blurb_html):
    """The words on the letter (and in the alt text and plain-text part)."""
    return {
        "eyebrow_l": "Weekend Dispatch",
        "eyebrow_r": "Fort Greene · Brooklyn",
        "title": ("The", "Week", "end."),          # "Week" is red, as in the email
        "date": f"{saturday.strftime('%A, %B %d')} — {sunday.strftime('%B %d')} · {saturday.year}",
        "blurb": plain(blurb_html),
        "button": "Open Full Newsletter",       # the arrow before it is drawn, Courier Prime has none
    }


def letter_text(content, url=""):
    """The same words as plain text (the email's alt text and plain-text part)."""
    t = (f"{content['eyebrow_l'].upper()} · {content['eyebrow_r'].upper()}\n"
         f"{content['title'][0]} {content['title'][1]}{content['title'][2]}\n"
         f"{content['date'].upper()}\n\n{content['blurb']}")
    return t + (f"\n\nOpen the full newsletter: {url}" if url else "")


# ─────────────────────────────────────────────
# THE LETTER SHEET
# ─────────────────────────────────────────────

def _tracked(draw, xy, text, font, fill, tracking, anchor="l"):
    """Draw text with letter-spacing (Pillow has none). Returns the width drawn."""
    x, y = xy
    widths = [font.getlength(ch) + tracking for ch in text]
    total = sum(widths) - tracking
    if anchor == "r":
        x -= total
    elif anchor == "m":
        x -= total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w
    return total


def _wrap(text, font, width):
    lines, cur = [], ""
    for word in text.split(" "):
        trial = (cur + " " + word).strip()
        if font.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def render_letter(content):
    """
    The letter as an RGB image, LW wide. Sized so that its text matches the
    email's text size once the GIF is shown at 620px: body 19px here is 13px
    there, as in the email's own card.
    """
    f_eyebrow = ImageFont.truetype(F_MONO, 14)
    f_title = ImageFont.truetype(F_TITLE, 72)
    f_date = ImageFont.truetype(F_MONO, 14)
    f_body = ImageFont.truetype(F_MONO, 21)
    f_btn = ImageFont.truetype(F_MONO, 15)
    inner = LW - 2 * PAD
    lines = _wrap(content["blurb"], f_body, inner)
    lh = 37

    y_eyebrow = 40
    y_title = y_eyebrow + 40
    title_lh = 68
    y_rule = y_title + 2 * title_lh + 18
    y_date = y_rule + 22
    y_body = y_date + 34
    y_btn = y_body + len(lines) * lh + 28
    btn_h = 58
    H = y_btn + btn_h + 46

    img = Image.new("RGB", (LW, H), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, LW - 1, H - 1], outline=PAPER_EDGE, width=2)
    _tracked(d, (PAD, y_eyebrow), content["eyebrow_l"].upper(), f_eyebrow, MUTED, 2.0)
    _tracked(d, (LW - PAD, y_eyebrow), content["eyebrow_r"].upper(), f_eyebrow, MUTED, 2.0, anchor="r")
    t1, t2a, t2b = content["title"]
    d.text((PAD, y_title), t1, font=f_title, fill=INK)
    d.text((PAD, y_title + title_lh), t2a, font=f_title, fill=RED)
    d.text((PAD + f_title.getlength(t2a), y_title + title_lh), t2b, font=f_title, fill=INK)
    d.line([(PAD, y_rule), (LW - PAD, y_rule)], fill=RULE, width=1)
    _tracked(d, (PAD, y_date), content["date"].upper(), f_date, RED, 2.8)
    for i, ln in enumerate(lines):
        d.text((PAD, y_body + i * lh), ln, font=f_body, fill=BODY)
    # button: a drawn down-arrow, then the label (the font has no arrow glyph)
    label = content["button"].upper()
    tw = _tracked(ImageDraw.Draw(Image.new("L", (1, 1))), (0, 0), label, f_btn, 0, 3.0)
    arrow_w, gap = 13, 18
    bw = int(arrow_w + gap + tw) + 2 * 32
    bx = (LW - bw) // 2
    d.rectangle([bx, y_btn, bx + bw, y_btn + btn_h], fill=INK, outline=(58, 42, 16))
    cream = (245, 237, 216)
    ax, ay = bx + 32 + arrow_w // 2, y_btn + btn_h // 2
    d.line([(ax, ay - 8), (ax, ay + 7)], fill=cream, width=2)
    d.line([(ax - 6, ay + 1), (ax, ay + 8), (ax + 6, ay + 1)], fill=cream, width=2)
    _tracked(d, (bx + 32 + arrow_w + gap, y_btn + btn_h // 2 - 9), label, f_btn, cream, 3.0)
    return img


# ─────────────────────────────────────────────
# THE SCENE
# ─────────────────────────────────────────────

def _desk_canvas(base_rgb, mouth_y):
    """
    The envelope photo on a taller desk: the wood above the envelope is extended
    by mirroring and sliding the photo's own wood strip (the grain is horizontal,
    so the joins do not show). Returns (canvas, photo_y_offset, canvas_height).
    """
    photo = base_rgb.convert("RGB").resize((GIF_W, round(base_rgb.height * K)), Image.LANCZOS)
    top_edge = round(dm.ENV_BOX[1] * K)
    oy = mouth_y - top_edge
    a = np.asarray(photo).astype(float)
    band = a[0:top_edge - 12]                          # clean wood above the envelope's shadow
    rng = np.random.default_rng(7)
    tiles, flip = [], True                             # the tile next to the photo is its own strip, mirrored: seamless
    total = 0
    while total < oy:
        t = band[::-1] if flip else band
        if tiles:                                      # slide every tile but the first sideways to hide repetition
            t = np.roll(t, int(rng.integers(0, GIF_W)), axis=1)
        tiles.append(t)
        total += len(t)
        flip = not flip
    ext = np.concatenate(tiles[::-1], axis=0)[-oy:] if oy > 0 else a[:0]
    full = np.concatenate([ext, a], axis=0)
    env_bottom = oy + round(dm.ENV_BOX[3] * K)
    H = env_bottom + 62
    return Image.fromarray(full[:H].clip(0, 255).astype("uint8")), oy, H


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in_out(t):
    return 3 * t * t - 2 * t * t * t


def build_letter_gif(base_rgb, content, out_w=740, dither=False):
    """
    base_rgb: the 1200x760 envelope photo with the date in the postmark.
    content: letter_content(...). out_w: width of the GIF in pixels. dither: error-diffusion dithering
    (smoother gradients, but several times the file size). Returns (gif_bytes, (width, height)); the GIF plays
    once and rests on the open letter.
    """
    letter = render_letter(content)
    LH = letter.height
    mouth_y = 30 + LH + 40                              # the envelope's top edge: room for the whole letter above it
    canvas0, oy, CH = _desk_canvas(base_rgb, mouth_y)
    W = GIF_W
    ex0, ex1 = round(dm.ENV_BOX[0] * K), round(dm.ENV_BOX[2] * K)
    ey1 = oy + round(dm.ENV_BOX[3] * K)
    seal_c = (round(dm.SEAL_CENTER[0] * K), oy + round(dm.SEAL_CENTER[1] * K))
    seal_r = round(dm.SEAL_RADIUS * K)
    tip_y = oy + round(392 * K)                         # where the flap's point meets the seal
    flap_h = tip_y - mouth_y
    c0_arr = np.asarray(canvas0).astype(float)
    rng = np.random.default_rng(11)

    # envelope body (everything below the top edge) and its soft mask: it is pasted over the
    # pigeon and the letter so they come out from behind the edge
    body_box = (ex0, mouth_y, ex1, ey1)
    body0 = canvas0.crop(body_box)
    body_mask = Image.new("L", body0.size, 255)

    # ----- paper colours, sampled from the photo itself -----
    paper_out = c0_arr[mouth_y + 24:mouth_y + 60, seal_c[0] - 70:seal_c[0] + 70].reshape(-1, 3).mean(0)   # the flap's own paper

    # ----- the seal, as wax-only pieces -----
    yy, xx = np.mgrid[0:CH, 0:W]
    dist = np.hypot(xx - seal_c[0], yy - seal_c[1])
    wax = (np.clip((c0_arr[..., 0] - c0_arr[..., 1] - 28) / 22, 0, 1) * np.clip((seal_r + 10 - dist) / 8, 0, 1))
    pad = seal_r + 10
    sb = (seal_c[0] - pad, seal_c[1] - pad, seal_c[0] + pad, seal_c[1] + pad)
    seal_rgb = canvas0.crop(sb)
    wax_box = wax[sb[1]:sb[3], sb[0]:sb[2]]
    # the crack: a jagged line just above the flap's point
    crack_y = tip_y - 10
    gx = np.arange(sb[0], sb[2])[None, :]
    gy = np.arange(sb[1], sb[3])[:, None]
    line = crack_y + np.tan(math.radians(-10)) * (gx - seal_c[0]) + 3.0 * np.sin(gx / 8.5) + 2.0 * np.sin(gx / 3.7 + 1.1)
    upper_m = (gy < line).astype(float)
    def piece(mask):
        a = Image.fromarray((wax_box * mask * 255).astype("uint8"))
        p = seal_rgb.convert("RGBA")
        p.putalpha(a)
        return p
    seal_upper, seal_lower = piece(upper_m), piece(1 - upper_m)
    seal_whole = piece(np.ones_like(upper_m))
    # paper under the seal, for when the pieces move away
    ring = (dist > seal_r + 12) & (dist < seal_r + 22) & (wax < 0.05)
    ring_col = c0_arr[ring].mean(0)

    def heal(body):
        """Paper where the seal was: the surrounding paper diffused inwards, so there is no pale disc."""
        r = seal_r + 6
        bx, by = seal_c[0] - ex0, seal_c[1] - mouth_y
        box = (bx - r - 24, by - r - 24, bx + r + 24, by + r + 24)
        crop = body.crop(box)
        m = Image.new("L", crop.size, 0)
        ImageDraw.Draw(m).ellipse([24, 24, crop.width - 24, crop.height - 24], fill=255)
        fill = crop.copy()
        for _ in range(70):                                  # blur, then keep only the blurred values inside the hole
            fill.paste(fill.filter(ImageFilter.GaussianBlur(9)), (0, 0), m)
        soft = m.filter(ImageFilter.GaussianBlur(2.5))
        crop.paste(fill, (0, 0), soft)
        out = body.copy()
        out.paste(crop, box[:2])
        return out
    body_healed = heal(body0)

    # ----- the flap -----
    fx0, fx1 = ex0, ex1
    flap_poly = lambda apex_y: [(fx0, mouth_y), (fx1, mouth_y), (seal_c[0], apex_y)]
    flap_tex = body_healed.crop((0, 0, ex1 - ex0, flap_h + 1))           # the flap's outside, seal removed
    # inside of the envelope under the flap: paper, darker toward the opening
    gradient = (0.80 + 0.20 * np.clip(np.arange(flap_h + 1) / flap_h, 0, 1))[:, None, None]
    bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0 - 0.5     # 4x4 ordered-dither matrix
    def dithered(arr):
        h, w = arr.shape[:2]
        tile = np.tile(bayer, (h // 4 + 1, w // 4 + 1))[:h, :w, None]
        return Image.fromarray((arr + tile * 4.0).clip(0, 255).astype("uint8"))
    inside_arr = np.broadcast_to((paper_out * np.array([0.98, 0.955, 0.90]))[None, None, :] * gradient, (flap_h + 1, ex1 - ex0, 3))   # a touch warmer than the outside
    inside_tex = dithered(np.array(inside_arr))
    # the flap's inner face, when it lies open above the envelope
    face_g = (0.93 + 0.10 * np.clip(np.arange(flap_h + 1) / flap_h, 0, 1))[::-1][:, None, None]
    face_arr = np.broadcast_to((paper_out * 1.03)[None, None, :] * face_g, (flap_h + 1, ex1 - ex0, 3))
    face_tex = dithered(np.array(face_arr))

    def tri_mask(size, apex_y_rel, flip=False):
        m = Image.new("L", size, 0)
        w, h = size
        poly = [(0, 0), (w - 1, 0), (seal_c[0] - ex0, apex_y_rel)] if not flip else [(0, h - 1), (w - 1, h - 1), (seal_c[0] - ex0, h - 1 - apex_y_rel)]
        ImageDraw.Draw(m).polygon(poly, fill=255)
        return m.filter(ImageFilter.GaussianBlur(0.7))

    # ----- the pigeon (as in the first animation, in this scene's coordinates) -----
    sprite0 = dm._pigeon_sprite()
    sprite = sprite0.resize((round(sprite0.width * K), round(sprite0.height * K)), Image.LANCZOS)
    pig_x = round(dm.PIGEON_X * K)
    pig_up = mouth_y - round((dm.ENV_BOX[1] - (dm.PIGEON_PEEK_TOP - 3)) * K)

    # ----- helpers that draw one frame -----
    def shadow_of(img_mask, blur, alpha, offset):
        """Soft drop shadow, as (L mask, offset)."""
        s = Image.new("L", (img_mask.width + 2 * blur * 3, img_mask.height + 2 * blur * 3), 0)
        s.paste(img_mask, (blur * 3, blur * 3))
        s = s.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * alpha))
        return s, (offset[0] - blur * 3, offset[1] - blur * 3)

    def with_pieces(body, g, s):
        """The envelope body with the cracked seal: g = how far the halves have parted (0 to 1),
        s = how far the flap has lifted (1 closed, 0 edge-on). The upper half travels with the flap."""
        out = body.convert("RGBA")
        if g <= 0:
            return out.convert("RGB")
        # lower half stays on the envelope
        lo = seal_lower.rotate(-2.0 * g, resample=Image.BICUBIC, center=(pad, pad))
        out.alpha_composite(lo, (seal_c[0] - pad - ex0, seal_c[1] - pad - mouth_y + round(3 * g)))
        if s > 0.02:
            up = seal_upper.rotate(3.5 * g, resample=Image.BICUBIC, center=(pad, pad))
            cy = seal_c[1] - round(7 * g)
            cy2 = mouth_y + s * (cy - mouth_y)
            h2 = max(2, round(up.height * s))
            up2 = up.resize((up.width, h2), Image.BICUBIC)
            out.alpha_composite(up2, (seal_c[0] - pad - ex0, round(cy2 - h2 / 2) - mouth_y))
        return out.convert("RGB")

    def crack_line(body, g):
        """The dark crack across the seal, drawn on the body image."""
        d = ImageDraw.Draw(body)
        pts = []
        for x in range(seal_c[0] - seal_r - 4, seal_c[0] + seal_r + 5, 3):
            yv = crack_y + math.tan(math.radians(-10)) * (x - seal_c[0]) + 3.0 * math.sin(x / 8.5) + 2.0 * math.sin(x / 3.7 + 1.1)
            pts.append((x - ex0, yv - mouth_y + 1.5 * g))
        d.line(pts, fill=(78, 36, 30), width=1 + int(2 * g))
        return body

    def body_state(s, g):
        """The envelope's front for flap position s (1 closed .. -1 fully open) and seal crack g."""
        if s >= 0.999 and g <= 0:
            return body0.copy()
        body = body_healed.copy() if g > 0 or s < 0.999 else body0.copy()
        if s < 0.999:
            body.paste(inside_tex, (0, 0), tri_mask(inside_tex.size, flap_h))
            if s > 0.02:                                     # the flap swinging up, foreshortened
                fh = max(1, round(flap_h * s))
                warped = flap_tex.transform(flap_tex.size, Image.AFFINE, (1, 0, 0, 0, 1 / s, 0), resample=Image.BICUBIC)
                shade = 1 - 0.10 * (1 - s)
                warped = Image.fromarray((np.asarray(warped).astype(float) * shade).clip(0, 255).astype("uint8"))
                m = tri_mask(flap_tex.size, fh)
                # soft shadow of the flap onto the inside
                sh, so = shadow_of(m.convert("L"), 6, 0.35, (0, 5))
                tmp = Image.new("L", body.size, 0)
                tmp.paste(sh, so)
                body.paste(Image.new("RGB", body.size, (60, 46, 28)), (0, 0), tmp.crop((0, 0) + body.size))
                body.paste(warped, (0, 0), m)
        if g > 0:
            body = with_pieces(body, g, s)
        elif s >= 0.999:
            pass
        if g > 0 and s >= 0.999:
            body = crack_line(body, g)
        if s < 0.6:                                           # the dark mouth of the envelope, where the letter will come out
            a = int(255 * 0.55 * min(1, (0.6 - s) / 0.6))
            slit = Image.new("L", (ex1 - ex0 - 24, 4), a).filter(ImageFilter.GaussianBlur(0.8))
            body.paste(Image.new("RGB", slit.size, (52, 40, 24)), (12, 0), slit)
        return body

    def frame_with(body, s=1.0, pigeon=None, sheet=None):
        """Compose: desk, open flap (above the edge), pigeon, letter, then the envelope front over them."""
        f = canvas0.copy()
        if s < -0.01 or (s <= 0.0 and s > -0.01):
            pass
        if s < 0:                                              # the flap lying open above the top edge
            fh = max(1, round(flap_h * -s))
            h = flap_h + 1
            face = face_tex.resize(face_tex.size) if False else face_tex
            squashed = face.resize((face.width, fh), Image.BICUBIC)
            m = Image.new("L", squashed.size, 0)
            ImageDraw.Draw(m).polygon([(0, fh - 1), (squashed.width - 1, fh - 1), (seal_c[0] - ex0, 0)], fill=255)
            m = m.filter(ImageFilter.GaussianBlur(0.7))
            sh, so = shadow_of(m, 7, 0.45, (4, 7))
            f.paste(Image.new("RGB", sh.size, (20, 12, 6)), (ex0 + so[0], mouth_y - fh + so[1]), sh)
            f.paste(squashed, (ex0, mouth_y - fh), m)
            ImageDraw.Draw(f).line([(ex0, mouth_y - 1), (seal_c[0], mouth_y - fh), (ex1, mouth_y - 1)], fill=(196, 184, 156), width=1)
        if pigeon is not None:
            lift, bob = pigeon
            spr = sprite if not bob else sprite.rotate(-3.5 * bob, resample=Image.BICUBIC, center=(sprite.width // 2, sprite.height - 15), expand=False)
            y = round(pig_up + (1 - lift) * (mouth_y + 8 - pig_up))
            x = round(pig_x - spr.width / 2 + 7 * bob)
            f.paste(spr.convert("RGB"), (x, y), spr.getchannel("A"))
        if sheet is not None:
            sh_img, top = sheet
            sx = (W - LW) // 2
            sm = Image.new("L", sh_img.size, 255)
            sh, so = shadow_of(sm, 10, 0.5, (0, 10))
            f.paste(Image.new("RGB", sh.size, (18, 10, 4)), (sx + so[0], top + so[1]), sh)
            f.paste(sh_img, (sx, top))
        f.paste(body, (ex0, mouth_y), body_mask)
        return f

    def folded(c):
        """The letter folded in three and opening: c is its height as a fraction of full (about .34 folded to 1)."""
        h = max(8, round(LH * c))
        img = letter.resize((LW, h), Image.LANCZOS)
        a = (1 - c) * 0.20
        arr = np.asarray(img).astype(float)
        for k in range(3):
            y0, y1 = round(h * k / 3), round(h * (k + 1) / 3)
            arr[y0:y1] *= (1 + a) if k % 2 == 0 else (1 - a)
        for k in (1, 2):                                       # the creases
            y = round(h * k / 3)
            arr[max(0, y - 1):y + 1] *= (1 - 0.35 * (1 - c))
        return Image.fromarray(arr.clip(0, 255).astype("uint8"))

    def glint(pos):
        centre = (pos * 2.4 - 0.7) * seal_r
        band = np.exp(-(((xx - seal_c[0]) + (yy - seal_c[1]) * 0.55 - centre) / (seal_r * 0.28)) ** 2)
        lift = (band * wax * 0.55)[..., None]
        arr = c0_arr * (1 - lift) + 255 * lift
        return Image.fromarray(arr.clip(0, 255).astype("uint8"))

    # ----- the timeline -----
    frames, delays = [], []
    def add(img, ms):
        frames.append(img)
        delays.append(ms)

    add(canvas0, 1300)                                         # 1. the closed envelope (also the still shown by apps that do not animate)
    for i in range(1, 6):                                      # 2. the pigeon peeks (as before)
        add(frame_with(body0, pigeon=(_ease_out(i / 5), 0.0)), 65)
    for b in (0.6, 1.0, 0.2, -0.5, -1.0, -0.3, 0.5, 0.9, 0.0):
        add(frame_with(body0, pigeon=(1.0, b)), 120)
    add(frame_with(body0, pigeon=(1.0, 0.0)), 450)
    for i in range(1, 5):
        add(frame_with(body0, pigeon=(1 - _ease_out(i / 4) * 0.97, 0.0)), 65)
    add(canvas0, 400)
    for i in range(8):                                         # 3. the seal shimmers (as before)
        add(glint(i / 7), 55)
    add(frame_with(crack_line(body0.copy(), 0.0)), 140)        # 4. the seal cracks and the halves part
    for g in (0.35, 0.7, 1.0):
        add(frame_with(body_state(1.0, g)), 90)
    for s in (0.78, 0.50, 0.22, -0.05, -0.35, -0.65, -0.88, -1.0):            # 5. the flap lifts back
        add(frame_with(body_state(s, 1.0), s=s), 60)
    s_open = -1.0
    body_open = body_state(s_open, 1.0)
    c0 = 0.34
    h0 = round(LH * c0)
    for i in range(1, 5):                                      # 6. the folded letter slides out of the envelope
        top = round((mouth_y - 36) - _ease_out(i / 4) * (h0 + 14))
        add(frame_with(body_open, s=s_open, sheet=(folded(c0), top)), 65)
    bottom0 = (mouth_y - 36) - (h0 + 14) + h0                  # where its bottom edge is once clear of the envelope
    final_bottom = mouth_y - 40
    for i in range(1, 8):                                      # 7. it unfolds, rising to its resting place
        t = _ease_out(i / 7)
        c = c0 + (1 - c0) * t
        bottom = bottom0 + (final_bottom - bottom0) * t
        h = max(8, round(LH * c))
        add(frame_with(body_open, s=s_open, sheet=(folded(c), round(bottom - h))), 75)
    add(frames[-1].copy(), 20000)                              # 8. it rests on the open letter

    # ----- crop the side wood, shrink to the output size, then one palette for every frame -----
    cx0, cx1 = CROP_X
    sc = out_w / (cx1 - cx0)
    OH = round(CH * sc)
    frames = [f.crop((cx0, 0, cx1, CH)).resize((out_w, OH), Image.LANCZOS) for f in frames]
    canvas_s = frames[0]
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE

    def part(img, n):
        q = img.quantize(colors=n, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        return q.getpalette()[:n * 3]

    photo_part = canvas_s.crop((0, round(oy * sc), out_w, OH))
    sp_px = np.asarray(sprite)
    sp_px = sp_px[sp_px[..., 3] > 128][:, :3].astype("uint8")
    sprite_img = Image.fromarray(sp_px.reshape(1, -1, 3))
    stamp_img = canvas_s.crop((round((dm.STAMP_BOX[0] * K - cx0) * sc), round((oy + dm.STAMP_BOX[1] * K) * sc),
                               round((dm.STAMP_BOX[2] * K - cx0) * sc), round((oy + dm.STAMP_BOX[3] * K) * sc)))
    sheet_part = Image.new("RGB", (LW, LH + 220))
    sheet_part.paste(letter, (0, 0))
    sheet_part.paste(folded(0.5).resize((LW, 220)), (0, LH))
    open_part = frames[-1].crop((0, round((mouth_y - flap_h - 4) * sc), out_w, round((mouth_y + 8) * sc)))
    exact = [c for rgb in (PAPER, PAPER_EDGE, RULE, INK, BODY, RED, MUTED, (245, 237, 216), (58, 42, 16)) for c in rgb]   # the email's own colours, exactly
    pal = (part(photo_part.resize((max(2, out_w // 2), max(2, photo_part.height // 2))), 102) + part(sprite_img, 32) + part(stamp_img, 16)
           + part(sheet_part.resize((LW // 2, sheet_part.height // 2)), 52) + part(open_part.resize((max(2, out_w // 2), max(2, open_part.height // 2))), 44) + exact)
    pal += [0] * (768 - len(pal))
    palette = Image.new("P", (1, 1))
    palette.putpalette(pal)

    base_arr = np.asarray(canvas_s, dtype=int)
    p_base = canvas_s.quantize(palette=palette, dither=mode)
    seq = []
    for img in frames:
        arr = np.asarray(img, dtype=int)
        diff = np.abs(arr - base_arr).sum(-1)
        ys, xs = np.where(diff > 6)
        pf = p_base.copy()
        if len(xs):
            box = (max(xs.min() - 2, 0), max(ys.min() - 2, 0), min(xs.max() + 3, out_w), min(ys.max() + 3, OH))
            pf.paste(img.crop(box).quantize(palette=palette, dither=mode), box[:2])
        seq.append(pf)

    buf = io.BytesIO()
    seq[0].save(buf, "GIF", save_all=True, append_images=seq[1:], duration=delays, optimize=False, disposal=1)
    return buf.getvalue(), (out_w, OH)
