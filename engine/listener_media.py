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


def card(name, city, caption, source):
    """The transparent 1920x1080 overlay with the name card (bottom left, above the safe edge)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
    lbl = "VIA HYPERADIO.NL" if source == "website" else "VIA WHATSAPP"
    who = (name or "Listener").strip().split()[0].upper() + ((" · " + city.strip().upper()) if (city or "").strip() else "")
    f_l, f_n, f_c = _font("Black", 22), _font("Black", 52), _font("SemiBold", 32)
    lines = textwrap.wrap((caption or "").strip(), 46)[:2]
    if len(textwrap.wrap((caption or "").strip(), 46)) > 2: lines[-1] = lines[-1][:43].rstrip() + "…"
    x, bottom = 72, H - 80
    nw = d.textlength(who, font=f_n); cw = max([d.textlength(l, font=f_c) for l in lines] or [0])
    box_w = int(max(nw, cw) + 64); box_h = 84 + (len(lines) * 44 + 10 if lines else 0)
    top = bottom - box_h
    # label tab
    lw = int(d.textlength(lbl, font=f_l) + 28)
    d.rounded_rectangle([x, top - 38, x + lw, top + 8], radius=8, fill=YELLOW)
    d.rectangle([x, top - 4, x + lw, top + 8], fill=YELLOW)
    d.text((x + 14, top - 32), lbl, font=f_l, fill=NAVY)
    # name box (shadow, navy, yellow left edge)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0)); ImageDraw.Draw(sh).rounded_rectangle([x, top + 8, x + box_w, bottom + 8], radius=14, fill=(0, 0, 0, 120))
    img = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), img); d = ImageDraw.Draw(img)
    d.rounded_rectangle([x, top, x + box_w, bottom], radius=14, fill=NAVY + (225,))
    d.rectangle([x, top, x + 12, bottom], fill=NAVY + (225,))
    d.rectangle([x, top, x + 6, bottom], fill=YELLOW)
    d.text((x + 30, top + 12), who, font=f_n, fill=(255, 255, 255))
    for i, l in enumerate(lines): d.text((x + 30, top + 82 + i * 44), l, font=f_c, fill=(214, 226, 238))
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
    canvas = Image.alpha_composite(canvas, card(name, city, caption, source))
    tmp = out + ".part.jpg"; canvas.convert("RGB").save(tmp, "JPEG", quality=90); os.replace(tmp, out)


def make_video(src, out, name, city, caption, source, max_s=120):
    ov = out + ".card.png"; card(name, city, caption, source).save(ov)
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
