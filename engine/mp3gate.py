#!/usr/bin/env python3
"""MP3 gate – sits between nginx and Icecast for the public radio.mp3.

Icecast 2.4 starts every new listener somewhere in the middle of an MP3 frame. Chrome/Edge
(FFmpeg inside) then can't recognise the stream until it has 128 KB, which with a 64 KB burst
means ~4 s of extra waiting. This gate skips those first broken bytes so every listener
starts exactly on a frame: the browser recognises it after a few KB and plays in ~1 s.

Per listener: one connection to Icecast (so Icecast still counts every listener), the first
partial frame dropped, the rest passed through untouched. ICY metadata (song titles) is
re-inserted on the listener's own count when the player asks for it.
"""
import asyncio, re

import os
LISTEN = ("127.0.0.1", int(os.environ.get("GATE_PORT", "8002")))
UP = ("127.0.0.1", 8000)
METAINT = 16000
MAX_SKIP = 16384           # never hunt longer than this; pass through as-is instead

BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],      # MPEG1 layer III
      2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}            # MPEG2/2.5 layer III
SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def frame_len(b, i):
    """Length of the MP3 frame at b[i], or 0 when there is no valid frame header."""
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return 0
    ver = (b[i + 1] >> 3) & 3
    if ver == 1 or ((b[i + 1] >> 1) & 3) != 1:      # reserved version / not layer III
        return 0
    bri, sri, pad = b[i + 2] >> 4, (b[i + 2] >> 2) & 3, (b[i + 2] >> 1) & 1
    if bri in (0, 15) or sri == 3:
        return 0
    br = BR[1 if ver == 3 else 2][bri] * 1000
    sr = SR[ver][sri]
    return (144 if ver == 3 else 72) * br // sr + pad


def find_start(b):
    """Offset of the first frame followed by two more valid frames, or -1."""
    for i in range(len(b) - 4):
        l1 = frame_len(b, i)
        if not l1:
            continue
        l2 = frame_len(b, i + l1)
        if l2 and frame_len(b, i + l1 + l2):
            return i
    return -1


async def read_head(reader):
    data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    return data.decode("latin-1")


async def handle(cr, cw):
    ur = uw = None
    try:
        req = await read_head(cr)
        lines = req.split("\r\n")
        m = re.match(r"(GET|HEAD) (\S+)", lines[0])
        if not m:
            cw.write(b"HTTP/1.0 400 Bad Request\r\n\r\n"); return
        hdr = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1); hdr[k.strip().lower()] = v.strip()
        want_meta = hdr.get("icy-metadata", "").strip() == "1"

        ur, uw = await asyncio.wait_for(asyncio.open_connection(*UP), 5)
        fwd = [f"GET {m.group(2)} HTTP/1.0", f"Host: {UP[0]}:{UP[1]}", "Icy-MetaData: 1"]
        for k in ("user-agent", "x-forwarded-for", "referer"):
            if k in hdr:
                fwd.append(f"{k.title()}: {hdr[k]}")
        uw.write(("\r\n".join(fwd) + "\r\n\r\n").encode("latin-1"))
        await uw.drain()

        resp = await read_head(ur)
        rl = resp.split("\r\n")
        up_metaint = 0
        out = []
        for l in rl[1:]:
            if not l:
                continue
            k = l.split(":", 1)[0].strip().lower()
            if k == "icy-metaint":
                up_metaint = int(l.split(":", 1)[1].strip() or 0)
                continue
            out.append(l)
        if " 200" not in rl[0] or m.group(1) == "HEAD":
            cw.write(resp.encode("latin-1"))
            if " 200" not in rl[0]:
                cw.write(await ur.read(65536))
            await cw.drain(); return
        if want_meta:
            out.append(f"icy-metaint:{METAINT}")
        cw.write(("\r\n".join([rl[0]] + out) + "\r\n\r\n").encode("latin-1"))
        await cw.drain()

        # ---- body: split audio / metadata coming from Icecast ----
        pending = b""            # upstream bytes not yet parsed
        to_meta = up_metaint     # audio bytes left before the next metadata block upstream
        meta = b"\x00"           # latest metadata block (length byte + text) from Icecast
        meta_sent = None
        aligned = False
        hunt = b""
        count = 0                # audio bytes sent since our last metadata block

        while True:
            chunk = await ur.read(65536)
            if not chunk:
                break
            pending += chunk
            audio = bytearray()
            while pending:
                if up_metaint and to_meta == 0:
                    n = pending[0] * 16
                    if len(pending) < 1 + n:
                        break
                    if n:
                        meta = pending[:1 + n]
                    pending = pending[1 + n:]
                    to_meta = up_metaint
                    continue
                take = len(pending) if not up_metaint else min(len(pending), to_meta)
                audio += pending[:take]; pending = pending[take:]
                if up_metaint:
                    to_meta -= take
            if not audio:
                continue
            audio = bytes(audio)
            if not aligned:
                hunt += audio
                s = find_start(hunt)
                if s < 0 and len(hunt) < MAX_SKIP:
                    continue
                audio = hunt[max(s, 0):]; hunt = b""; aligned = True
            if not want_meta:
                cw.write(audio)
            else:
                parts = []
                while audio:
                    room = METAINT - count
                    parts.append(audio[:room]); count += min(room, len(audio)); audio = audio[room:]
                    if count == METAINT:
                        if meta != meta_sent:
                            parts.append(meta); meta_sent = meta
                        else:
                            parts.append(b"\x00")
                        count = 0
                cw.write(b"".join(parts))
            await cw.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        pass
    finally:
        for w in (uw, cw):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


async def main():
    srv = await asyncio.start_server(handle, *LISTEN, backlog=1024, limit=65536)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
