"""Listener photos & videos (approved in Hype Live) -> ready-to-show 16:9 media pads in branding/media.

Every item becomes one 1920x1080 file:
  - the picture / video fitted in the middle (portrait phone shots too), over a blurred, darkened copy of itself
  - a name card bottom left in the Hype style: yellow label (VIA WHATSAPP / VIA HYPERADIO.NL), the first name + city,
    the caption (max 2 lines)
Videos: H.264 + AAC, max 120 s; their sound is levelled when they play (like every media pad)."""
import os, subprocess, textwrap
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1920, 1080
FONT = "/usr/share/fonts/opentype/inter/Inter-%s.otf"
YELLOW, NAVY = (255, 224, 0), (4, 28, 51)


def _font(weight, size):
    try: return ImageFont.truetype(FONT % weight, size)
    except OSError: return ImageFont.load_default()


def _spaced(d, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill); x += d.textlength(ch, font=font) + spacing
    return x


def card(name, city, caption, source, kind="photo"):
    """The transparent 1920x1080 overlay with the name card – the same house style as the voice message box
    (Gianno's design A): dark navy rounded box with a yellow left edge, a yellow icon circle, a red dot + yellow spaced
    label, the first name in white (no city, like the voice box), "via WhatsApp / via hyperadio.nl", then the caption."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    first = ((name or "Listener").strip().split() or ["Listener"])[0]
    via = "via hyperadio.nl" if source == "website" else "via WhatsApp" if source == "whatsapp" else ""
    f_l, f_n, f_v, f_c = _font("Black", 21), _font("Black", 46), _font("SemiBold", 26), _font("Medium", 28)
    d0 = ImageDraw.Draw(img)
    lines = textwrap.wrap((caption or "").strip(), 52)
    if len(lines) > 2: lines = lines[:2]; lines[1] = lines[1][:49].rstrip() + "…"
    label = "VIDEO MESSAGE" if kind == "video" else "PHOTO MESSAGE"
    x0, bottom = 80, 943
    text_w = max([d0.textlength(first, font=f_n), d0.textlength(via, font=f_v)] + [d0.textlength(l, font=f_c) for l in lines])
    w = int(max(640, 222 - x0 + text_w + 64)); h = 148 + (len(lines) * 40 + 14 if lines else 0)
    y0 = bottom - h
    # soft shadow
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0)); ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 10, x0 + w, bottom + 10], radius=22, fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(16)))
    box = Image.new("RGBA", (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(box)
    bd.rounded_rectangle([x0, y0, x0 + w, bottom], radius=22, fill=(8, 13, 28, 200))
    img = Image.alpha_composite(img, box); d = ImageDraw.Draw(img)
    # yellow left edge (follows the rounded corners)
    edge = Image.new("L", (W, H), 0); ImageDraw.Draw(edge).rounded_rectangle([x0, y0, x0 + w, bottom], radius=22, fill=255)
    stripe = Image.new("L", (W, H), 0); ImageDraw.Draw(stripe).rectangle([x0, y0, x0 + 6, bottom], fill=255)
    from PIL import ImageChops
    img.paste(YELLOW + (255,), (0, 0), ImageChops.multiply(edge, stripe))
    # icon: yellow circle with a soft ring, camera or play symbol in navy
    cx, cy = 152, y0 + 74
    d.ellipse([cx - 54, cy - 54, cx + 54, cy + 54], fill=(255, 224, 0, 48))
    d.ellipse([cx - 46, cy - 46, cx + 46, cy + 46], fill=YELLOW + (255,))
    if kind == "video":
        d.polygon([(cx - 12, cy - 18), (cx - 12, cy + 18), (cx + 20, cy)], fill=NAVY)
    else:
        d.rounded_rectangle([cx - 24, cy - 14, cx + 24, cy + 18], radius=6, fill=NAVY)
        d.rounded_rectangle([cx - 10, cy - 21, cx + 8, cy - 11], radius=3, fill=NAVY)
        d.ellipse([cx - 10, cy - 8, cx + 10, cy + 12], fill=YELLOW); d.ellipse([cx - 5, cy - 3, cx + 5, cy + 7], fill=NAVY)
    # label, name, via, caption
    d.ellipse([226 - 6, y0 + 33 - 6, 226 + 6, y0 + 33 + 6], fill=(255, 59, 92))
    _spaced(d, (242, y0 + 21), label, f_l, YELLOW, 5)
    d.text((222, y0 + 87), first, font=f_n, fill=(255, 255, 255), anchor="ls")
    if via: d.text((222, y0 + 123), via, font=f_v, fill=(200, 210, 224), anchor="ls")
    for i, l in enumerate(lines): d.text((222, y0 + 123 + 42 * (i + 1)), l, font=f_c, fill=(240, 244, 248), anchor="ls")
    return img


def make_photo(src, out, name, city, caption, source):
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    bg = ImageOps.fit(im, (W, H)).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (6, 20, 35)), 0.45)
    fg = im.copy(); fg.thumbnail((W - 160, H - 80))
    if fg.width < W * 0.45 and fg.height < H * 0.45:          # small picture: a bit bigger (never more than 2x)
        k = min(2.0, (H - 80) / fg.height, (W - 160) / fg.width); fg = fg.resize((int(fg.width * k), int(fg.height * k)), Image.LANCZOS)
    canvas = bg.convert("RGBA")
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0)); px, py = (W - fg.width) // 2, (H - fg.height) // 2
    ImageDraw.Draw(sh).rectangle([px, py + 10, px + fg.width, py + fg.height + 10], fill=(0, 0, 0, 140))
    canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(18)))
    canvas.paste(fg, (px, py))
    canvas = Image.alpha_composite(canvas, card(name, city, caption, source, "photo"))
    tmp = out + ".part.jpg"; canvas.convert("RGB").save(tmp, "JPEG", quality=90); os.replace(tmp, out)


def make_video(src, out, name, city, caption, source, max_s=120):
    ov = out + ".card.png"; card(name, city, caption, source, "video").save(ov)
    tmp = os.path.join(os.path.dirname(out), "." + os.path.basename(out) + ".part.mp4")
    fc = ("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=40,eq=brightness=-0.12:saturation=0.8[bg];"
          "[0:v]scale=1920-160:1080-80:force_original_aspect_ratio=decrease[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2[v1];[v1][1:v]overlay=0:0,fps=30,format=yuv420p[v]")
    cmd = ["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", src, "-i", ov, "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
           "-t", str(max_s), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if p.returncode != 0 or not os.path.exists(tmp): raise RuntimeError("ffmpeg: " + (p.stderr or "")[-200:])
        os.replace(tmp, out)
    finally:
        for f in (ov, tmp):
            try: os.remove(f)
            except FileNotFoundError: pass
