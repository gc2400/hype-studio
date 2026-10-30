"""Hype Studio – tickers (news, traffic, showbizz, agenda, weather …) for the Graphics page.
A ticker = an RSS/Atom feed, or a Hype Live ticker link (text between [[HYPE]] … [[/HYPE]], items split by •).
Fetched every 10 minutes in the background; when a feed fails, the last good items stay."""
import os, json, re, time, html, threading, logging, urllib.request, urllib.parse
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime

log = logging.getLogger("tickers")
CONF = "/var/lib/hype/tickers.json"
SCREEN_DIR = "/srv/hype/branding/screen"
BARS = {"news": "08_NewsBar.png", "weather": "10_WeatherBar.png", "traffic": "12_TrafficBar.png", "showbiz": "13_ShowbizBar.png", "agenda": "15_AgendaBar.png"}
EVERY = 600
BLOCK_NEWS = ("dood, doden, dode, omgekomen, overleden, moord, vermoord, doodgeschoten, schietpartij, schietincident, steekpartij, "
              "neergestoken, aanslag, terreur, oorlog, bombardement, verkracht, misbruik, ontvoerd, lijk, zelfdoding, gewond, "
              "explosie, vliegtuigcrash, ongeluk, drama")
DEFAULT = [
    {"id": "news", "label": "NEWS", "sub": "NU.NL", "url": "https://www.nu.nl/rss", "always": ["Hype Screen"], "max": 12, "hours": 12, "block": BLOCK_NEWS, "on": True},
    {"id": "traffic", "label": "TRAFFIC", "sub": "ROAD & RAIL", "url": "https://hyperadio.nl/?hype_ticker=traffic", "always": [], "max": 12, "hours": 24, "block": "", "on": True},
    {"id": "showbiz", "label": "SHOWBIZZ", "sub": "MUSIC NEWS", "url": "https://hyperadio.nl/?hype_ticker=showbiz", "always": [], "max": 12, "hours": 48, "block": "", "on": True},
    {"id": "agenda", "label": "AGENDA", "sub": "EVENTS & SHOWS", "url": "https://hyperadio.nl/?hype_ticker=agenda", "always": [], "max": 10, "hours": 0, "block": "", "on": True},
    {"id": "weather", "label": "WEATHER", "sub": "AMSTERDAM", "url": "https://www.hyperadio.nl/?hype_ticker=weather", "always": [], "max": 8, "hours": 0, "block": "", "on": True},
]
DATA = {}          # id -> {"items": [...], "at": time, "err": str}
_wake = threading.Event()

def config():
    try:
        c = json.load(open(CONF))
        if not (isinstance(c, list) and c): raise ValueError
    except Exception: c = [dict(t) for t in DEFAULT]
    for t in c:      # the bar graphic: Gianno's PNG (branding/screen) when there is one for this ticker
        if "bar" not in t: t["bar"] = BARS.get(t["id"], "")
    return c

_AREA = {}
def bar_area(f):
    """Where the text goes on a full-frame bar PNG: the dark track (x from-to) on the row through the middle of the bar."""
    path = os.path.join(SCREEN_DIR, f)
    try: key = (f, os.path.getmtime(path))
    except OSError: return None
    if key in _AREA: return _AREA[key]
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA"); W, H = im.size
        a = [im.getpixel((W // 2, y))[3] for y in range(H)]; ys = [y for y in range(H) if a[y] > 150]
        if not ys: return None
        y0, y1 = ys[0], ys[-1]; yc = (y0 + y1) // 2
        ref = im.getpixel((W // 2, yc)); row = [im.getpixel((x, yc)) for x in range(W)]
        same = lambda p: abs(p[0] - ref[0]) + abs(p[1] - ref[1]) + abs(p[2] - ref[2]) < 24 and abs(p[3] - ref[3]) < 30
        x0 = W // 2
        while x0 > 0 and same(row[x0 - 1]): x0 -= 1
        x1 = W // 2
        while x1 < W - 1 and same(row[x1 + 1]): x1 += 1
        # the small label box left of the track (e.g. "AMSTERDAM"): same fill, behind a 1 px divider
        top = [im.getpixel((x, y0 + 5)) for x in range(W)]     # a row above the label text
        bx = x0 - 2
        while bx > 0 and same(top[bx - 1]): bx -= 1
        if x0 - bx < 20: bx = x0
        # text inside the outline: 1 px in from the top and bottom edge
        _AREA[key] = {"x": x0, "y": y0 + 1, "w": x1 - x0 + 1, "h": y1 - y0 - 1, "bx": bx, "by": y0 + 1, "bw": x1 - bx + 1, "bh": y1 - y0 - 1, "v": int(key[1])}
        return _AREA[key]
    except Exception as e:
        log.warning("bar %s: %s", f, e); return None

def save(lst):
    clean = []
    for t in (lst or [])[:12]:
        tid = re.sub(r"[^a-z0-9_-]", "", str(t.get("id") or t.get("label") or "").lower())[:20]
        if not tid or any(x["id"] == tid for x in clean): continue
        clean.append({"id": tid, "label": str(t.get("label") or tid).strip()[:24].upper(), "sub": str(t.get("sub") or "").strip()[:28].upper(),
                      "url": str(t.get("url") or "").strip()[:500], "always": [str(s)[:40] for s in (t.get("always") or [])][:10],
                      "max": max(1, min(30, int(t.get("max") or 12))), "hours": max(0, min(168, int(t.get("hours") or 0))),
                      "block": str(t.get("block") or "")[:2000], "on": bool(t.get("on", True)), "bar": os.path.basename(str(t.get("bar") or ""))[:120]})
    json.dump(clean, open(CONF, "w"), indent=1); _wake.set(); return clean

def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
    s = re.sub(r"^(video|liveblog|update|column|podcast|review|interview|reconstructie|analyse)\s*\|\s*", "", s, flags=re.I)   # nu.nl prefixes
    return re.sub(r"\s+", " ", s)

def blocked(text, words):
    w = [x.strip().lower() for x in (words or "").split(",") if x.strip()]
    t = " " + re.sub(r"[^\w]+", " ", text.lower()) + " "
    return any((" " + x + " ") in t or (len(x) > 5 and x in t) for x in w)

def fetch(t):
    req = urllib.request.Request(t["url"], headers={"User-Agent": "HypeStudio/1.0 (+https://hyperadio.nl)"})
    with urllib.request.urlopen(req, timeout=15) as r: raw = r.read(2_000_000).decode("utf-8", "replace")
    m = re.search(r"\[\[HYPE\]\](.*?)\[\[/HYPE\]\]", raw, re.S)
    if m:      # a Hype Live ticker link: already filtered and worded by the website
        items = [clean_text(x) for x in re.split(r"\s*•\s*", m.group(1))]
        return [x for x in items if x and not blocked(x, t.get("block"))][: t["max"]]
    root = ET.fromstring(raw.encode("utf-8"))
    out, now = [], time.time()
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"): continue
        title = date = None
        for ch in it:
            n = ch.tag.split("}")[-1]
            if n == "title": title = clean_text(ch.text)
            if n in ("pubDate", "published", "updated", "date") and ch.text and not date:
                try: date = parsedate_to_datetime(ch.text.strip()).timestamp()
                except Exception:
                    try: date = time.mktime(time.strptime(ch.text.strip()[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception: pass
        if not title or blocked(title, t.get("block")): continue
        if t.get("hours") and date and now - date > t["hours"] * 3600: continue
        out.append((date or 0, title))
    out.sort(key=lambda x: -x[0]); seen, items = set(), []
    for _, title in out:
        k = title.lower()[:60]
        if k in seen: continue
        seen.add(k); items.append(title)
    return items[: t["max"]]

def refresh_all():
    for t in config():
        if not t.get("on") or not t.get("url"): continue
        try:
            items = fetch(t)
            if items: DATA[t["id"]] = {"items": items, "at": time.time(), "err": ""}
            else: DATA.setdefault(t["id"], {"items": [], "at": 0})["err"] = "no items"
        except Exception as e:
            DATA.setdefault(t["id"], {"items": [], "at": 0})["err"] = str(e)[:120]; log.warning("ticker %s: %s", t["id"], e)

def run():
    while True:
        try: refresh_all()
        except Exception as e: log.warning("tickers: %s", e)
        _wake.wait(EVERY); _wake.clear()

def start():
    threading.Thread(target=run, daemon=True).start()

def get(tid):
    t = next((x for x in config() if x["id"] == tid), None)
    if not t or not t.get("on"): return None
    items = (DATA.get(tid) or {}).get("items") or []
    if not items: return None
    g = {"id": tid, "label": t["label"], "sub": t["sub"], "items": items}
    area = bar_area(t["bar"]) if t.get("bar") else None
    if area: g.update(bar="/screen/" + urllib.parse.quote(t["bar"]) + "?v=%d" % area["v"], area=area)
    return g

def always_for(scene):
    for t in config():
        if t.get("on") and scene in (t.get("always") or []):
            g = get(t["id"])
            if g: return g
    return None

def status():
    return {"tickers": config(), "data": {k: {"n": len(v.get("items") or []), "at": v.get("at"), "err": v.get("err", ""), "first": (v.get("items") or [""])[:3]} for k, v in DATA.items()}}

def as_ticker(f):
    """A ticker's bar PNG chosen as a graphic (+ ELEMENT, ON SCREEN) means: show that ticker, with its text."""
    if f and not f.startswith(("ticker:", "scene:")):
        for t in config():
            if t.get("on") and t.get("bar") and t["bar"] == f: return "ticker:" + t["id"]
    return f
