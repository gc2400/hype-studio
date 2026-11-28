"""SLIDESHOW scenes: every subfolder of branding/slideshow/photos and branding/slideshow/videos becomes its own full-screen
scene ("Photos · Drone", "Videos · Party EQ") with the graphics, show name, ticker and Live Layer on top - like the Hype Screen.
Photos: OBS's own image slideshow (random, N s each, crossfade). Videos: one OBS media source that Hype Studio feeds a random
next video whenever one ends, sound always off (the music / your mix keeps playing). A new subfolder makes a scene within
~30 s, a removed one takes its scene away; files added or removed are picked up by themselves."""
import os, time, random, logging

log = logging.getLogger("hype")
ROOT = "/srv/hype/branding/slideshow"
KINDS = {"photos": ("Photos · ", (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")),
         "videos": ("Videos · ", (".mp4", ".mov", ".mkv", ".webm", ".m4v"))}
SKIP = {"Hype Screen page", "Ad box"}          # the Hype Screen's own page + the ad box are not on a slideshow
KEEP_T = ("positionX", "positionY", "rotation", "scaleX", "scaleY", "alignment", "boundsType", "boundsAlignment", "boundsWidth",
          "boundsHeight", "cropTop", "cropBottom", "cropLeft", "cropRight")
STATE = {"list": [], "sig": {}, "vid": {}, "t": 0}
# LIVE MIX versions (28 Nov, Gianno): a video slideshow full screen with the CAM 1 box of "M 1 LIVE MIX" on top
LIVE_MIX = {"Drone"}                 # the video slideshows that get one ("LIVE MIX · Drone")
LM_PREFIX, LM_TMPL = "LIVE MIX · ", "M 1 LIVE MIX"


def drives(scene):
    """A scene whose slideshow videos Hype Studio feeds: a Videos scene or its LIVE MIX version."""
    return is_slideshow(scene) or bool(scene) and scene.startswith(LM_PREFIX)


def also_cam(cl, have, sc="Hype Stations CAM 1", tmpl="Hype Stations", cams="M 1 LIVE MIX"):
    """Make "Hype Stations CAM 1" (28 Nov, Gianno): the Hype Stations PiP with CAM 1 (slate, black, feed – as in the camera
    scenes) in the window where the music video (Clips window) is."""
    if sc in have or tmpl not in have or cams not in have: return
    T = sorted(cl.get_scene_item_list(tmpl).scene_items, key=lambda i: i["sceneItemIndex"])
    win = next((i for i in T if i["sourceName"] == "Clips window"), None)
    if not win: return
    wt = cl.get_scene_item_transform(tmpl, win["sceneItemId"]).scene_item_transform
    C = sorted(cl.get_scene_item_list(cams).scene_items, key=lambda i: i["sceneItemIndex"])
    C = [i for i in C if i["sourceName"] in ("CAM 1 slate", "Cam black", "Cam 1 feed")]
    cl.create_scene(sc)
    def put(it, t):
        nid = cl.create_scene_item(sc, it["sourceName"], it["sceneItemEnabled"]).scene_item_id
        try: cl.set_scene_item_transform(sc, nid, {k: v for k, v in t.items() if k in KEEP_T and not (k == "boundsWidth" and v < 1) and not (k == "boundsHeight" and v < 1)})
        except Exception as e: log.warning("%s: %s transform: %s", sc, it["sourceName"], e)
    for it in T:
        if it is win:
            for c_ in C: put(c_, wt)
        else: put(it, cl.get_scene_item_transform(tmpl, it["sceneItemId"]).scene_item_transform)
    log.info("scene %s made", sc)


def live_mix(cl, have, name):
    """Make "LIVE MIX · <name>": the slideshow's video (+ its blue background) at the bottom, then everything of M 1 LIVE MIX
    except its Media window (CAM 1 box, frame, graphics, logo, clock, badge, audio, Live Layer …), in the same places."""
    sc, ss = LM_PREFIX + name, scene_name("videos", name)
    if sc in have or ss not in have or LM_TMPL not in have: return
    cl.create_scene(sc)
    base = sorted(cl.get_scene_item_list(ss).scene_items, key=lambda i: i["sceneItemIndex"])
    base = [i for i in base if i["sourceName"] in ("Media background", "SS " + ss)]
    top = sorted(cl.get_scene_item_list(LM_TMPL).scene_items, key=lambda i: i["sceneItemIndex"])
    top = [i for i in top if i["sourceName"] != "Media window"]
    for from_sc, items in ((ss, base), (LM_TMPL, top)):
        for it in items:
            nid = cl.create_scene_item(sc, it["sourceName"], it["sceneItemEnabled"]).scene_item_id
            try:
                t = cl.get_scene_item_transform(from_sc, it["sceneItemId"]).scene_item_transform
                cl.set_scene_item_transform(sc, nid, {k: v for k, v in t.items() if k in KEEP_T and not (k == "boundsWidth" and v < 1) and not (k == "boundsHeight" and v < 1)})
            except Exception as e: log.warning("%s: %s transform: %s", sc, it["sourceName"], e)
    log.info("slideshow: scene %s made", sc)


def folders():
    """[(kind, name, dir, files)] for every slideshow subfolder."""
    out = []
    for kind, (_, exts) in KINDS.items():
        base = os.path.join(ROOT, kind)
        if not os.path.isdir(base):       # first time: the folders you (WinSCP user "upload") can make subfolders in
            os.makedirs(base, exist_ok=True)
            try:
                import pwd, grp; u, g = pwd.getpwnam("upload").pw_uid, grp.getgrnam("hype").gr_gid
                for x in (ROOT, base): os.chown(x, u, g); os.chmod(x, 0o2775)
            except Exception: pass
        for name in sorted(os.listdir(base), key=str.lower):
            d = os.path.join(base, name)
            if name.startswith(".") or not os.path.isdir(d): continue
            files = sorted(f for f in os.listdir(d) if f.lower().endswith(exts) and not f.startswith("."))
            out.append((kind, name, d, files))
    return out


def scene_name(kind, name): return KINDS[kind][0] + name


def source_name(kind, name): return "SS " + scene_name(kind, name)


def is_slideshow(scene): return bool(scene) and scene.startswith(tuple(p for p, _ in KINDS.values()))


def photo_settings(d, sec, fade, rnd):
    return {"files": [{"value": d, "hidden": False, "selected": False}], "slide_time": int(sec * 1000), "transition": "fade",
            "transition_speed": int(fade * 1000), "randomize": bool(rnd), "loop": True, "hide": False,
            "playback_behavior": "always_play", "slide_mode": "mode_auto", "use_custom_size": "1920x1080"}


def fit(cl, sc, iid):
    """The whole photo / video, as big as fits, centred - never cut off (the Hype blue background shows around it)."""
    cl.set_scene_item_transform(sc, iid, {"positionX": 0, "positionY": 0, "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER",
                                         "boundsWidth": 1920, "boundsHeight": 1080, "boundsAlignment": 0})


def ensure_bg(cl, sc, src):
    """Hype blue behind it (the 'Media background' of the MEDIA scenes), at the bottom; the slideshow itself fitted on top."""
    items = cl.get_scene_item_list(sc).scene_items
    if not any(i["sourceName"] == "Media background" for i in items):
        bid = cl.create_scene_item(sc, "Media background", True).scene_item_id
        try:
            ref = next(i for i in cl.get_scene_item_list("MEDIA FULL").scene_items if i["sourceName"] == "Media background")
            t = cl.get_scene_item_transform("MEDIA FULL", ref["sceneItemId"]).scene_item_transform
            cl.set_scene_item_transform(sc, bid, {k: v for k, v in t.items() if k in KEEP_T and not (k in ("boundsWidth", "boundsHeight") and v < 1)})
        except StopIteration: pass
        cl.set_scene_item_index(sc, bid, 0)
    sid = next((i["sceneItemId"] for i in cl.get_scene_item_list(sc).scene_items if i["sourceName"] == src), None)
    if sid is not None: fit(cl, sc, sid); cl.set_scene_item_index(sc, sid, 1)


def sync(cl, sec=8.0, fade=1.0, rnd=True):
    """Make OBS match the folders: create / remove scenes, refresh a photo slideshow when its files or settings changed."""
    have = {s["sceneName"] for s in cl.get_scene_list().scenes}
    want = folders(); names = set()
    tmpl = [i for i in cl.get_scene_item_list("Hype Screen").scene_items if i["sourceName"] not in SKIP]
    tmpl.sort(key=lambda i: i["sceneItemIndex"])
    for kind, name, d, files in want:
        sc, src = scene_name(kind, name), source_name(kind, name); names.add(sc)
        sig = (tuple(files), sec, fade, rnd) if kind == "photos" else tuple(files)
        if sc not in have:
            cl.create_scene(sc)
            if kind == "photos":
                iid = cl.create_input(sc, src, "slideshow_v2", photo_settings(d, sec, fade, rnd), True).scene_item_id
            else:
                iid = cl.create_input(sc, src, "ffmpeg_source", {"is_local_file": True, "local_file": "", "looping": False, "restart_on_activate": False,
                                                                  "close_when_inactive": True, "clear_on_media_end": False, "hw_decode": True}, True).scene_item_id
                cl.set_input_mute(src, True)
            fit(cl, sc, iid)
            for it in tmpl:        # graphics, logo, clock, show intro, ticker / lower third, audio, Live Layer … as on the Hype Screen
                nid = cl.create_scene_item(sc, it["sourceName"], it["sceneItemEnabled"]).scene_item_id
                try:
                    t = cl.get_scene_item_transform("Hype Screen", it["sceneItemId"]).scene_item_transform
                    cl.set_scene_item_transform(sc, nid, {k: v for k, v in t.items() if k in KEEP_T and not (k == "boundsWidth" and v < 1) and not (k == "boundsHeight" and v < 1)})
                except Exception as e: log.warning("slideshow %s: %s transform: %s", sc, it["sourceName"], e)
            STATE["sig"][sc] = sig; log.info("slideshow: scene %s made (%d files)", sc, len(files))
        if sc not in STATE.setdefault("fitted", set()):    # (also scenes made before 15 Nov: whole picture on Hype blue)
            try: ensure_bg(cl, sc, src); STATE["fitted"].add(sc)
            except Exception as e: log.warning("slideshow %s background: %s", sc, e)
        elif kind == "photos" and STATE["sig"].get(sc) != sig:
            cl.set_input_settings(src, photo_settings(d, sec, fade, rnd), False); STATE["sig"][sc] = sig
        elif kind == "videos": STATE["sig"][sc] = sig
    for kind, name, d, files in want:
        if kind == "videos" and name in LIVE_MIX:
            names.add(LM_PREFIX + name)
            try: live_mix(cl, have | {scene_name(kind, name)}, name)
            except Exception as e: log.warning("live mix %s: %s", name, e)
    try: also_cam(cl, have)
    except Exception as e: log.warning("Hype Stations CAM 1: %s", e)
    for sc in have:        # a removed folder (or no LIVE MIX wanted any more): its scene goes (only scenes this module made)
        if sc.startswith(LM_PREFIX) and sc not in names:
            try: cl.remove_scene(sc); log.info("slideshow: scene %s removed", sc)
            except Exception as e: log.warning("slideshow remove %s: %s", sc, e)
            continue
        if is_slideshow(sc) and sc not in names:
            try:
                kind = "photos" if sc.startswith(KINDS["photos"][0]) else "videos"
                cl.remove_scene(sc); cl.remove_input("SS " + sc); STATE["sig"].pop(sc, None); log.info("slideshow: scene %s removed (folder gone)", sc)
            except Exception as e: log.warning("slideshow remove %s: %s", sc, e)
    STATE["list"] = [{"scene": scene_name(k, n), "kind": k, "name": n, "n": len(f), "folder": os.path.relpath(d, "/srv/hype")} for k, n, d, f in want]
    STATE["list"] += [{"scene": LM_PREFIX + n, "kind": k, "name": n + " · LIVE MIX", "n": len(f), "folder": os.path.relpath(d, "/srv/hype")}
                      for k, n, d, f in want if k == "videos" and n in LIVE_MIX]
    STATE["t"] = time.time()


def drive(cl, program):
    """While a Videos scene is on air: when its video ended (or none is loaded yet) a random next one, never the same twice in a row."""
    if program and program.startswith(LM_PREFIX): program = KINDS["videos"][0] + program[len(LM_PREFIX):]     # LIVE MIX · X: the videos of Videos · X
    if not program or not program.startswith(KINDS["videos"][0]): STATE["cur"] = None; return
    name = program[len(KINDS["videos"][0]):]; d = os.path.join(ROOT, "videos", name); src = "SS " + program
    try: files = sorted(f for f in os.listdir(d) if f.lower().endswith(KINDS["videos"][1]) and not f.startswith("."))
    except OSError: return
    if not files: return
    v = STATE["vid"].setdefault(program, {"bag": [], "last": None})
    st = cl.get_media_input_status(src)
    state = getattr(st, "media_state", "")
    fresh = STATE.get("cur") != program
    if fresh or state in ("OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_STOPPED", "OBS_MEDIA_STATE_NONE", "OBS_MEDIA_STATE_ERROR"):
        if not v["bag"]: v["bag"] = random.sample(files, len(files))
        nxt = v["bag"].pop()
        if nxt == v["last"] and len(files) > 1:
            v["bag"].insert(0, nxt); nxt = v["bag"].pop()
        cl.set_input_settings(src, {"local_file": os.path.join(d, nxt)}, True)
        cl.set_input_mute(src, True)
        v["last"] = nxt; STATE["cur"] = program
