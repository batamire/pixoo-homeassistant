"""Rerunnable live-device probe for multi-frame components animation.

1. contract control - 3 solid-colour frames, PicNum 3, shared PicID, 600 ms
2. real shape - 8 composited sun+text frames, PicNum 8, 200 ms
3. static control - PicNum 1 single frame (must render static)

Usage: PIXOO_HOST=192.168.1.8 python3 scripts/probe_gif_animation.py
Prints exact commands + raw responses. error_code 0 is NOT proof - the user
must eyeball the display for motion (see prompts).
"""

import argparse
import base64
import json
import os
import sys
import urllib.request

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("need Pillow: pip install pillow")
    sys.exit(2)


def post(host, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://{host}/post", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
    print(f">> {json.dumps({k: v for k, v in payload.items() if k != 'PicData'})}")
    print(f"<< {raw}")
    return json.loads(raw)


def frame_buffer(draw_fn):
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    draw_fn(ImageDraw.Draw(img))
    return base64.b64encode(img.tobytes()).decode()


def solid(color):
    return lambda d: d.rectangle([0, 0, 63, 63], fill=color)


def sun_frame(i):
    def draw(d):
        d.rectangle([0, 0, 63, 63], fill=(0, 0, 0))
        r = 8 + (i % 4) * 2
        d.ellipse([32 - r, 20 - r, 32 + r, 20 + r], fill=(255, 200, 0))
        d.text((4, 48), f"F{i}", fill=(255, 255, 255))
    return draw


def send_animation(host, buffers, speed_ms, pic_id=1):
    post(host, {"Command": "Draw/ResetHttpGifId"})
    for offset, pic in enumerate(buffers):
        post(host, {"Command": "Draw/SendHttpGif", "PicNum": len(buffers),
                    "PicWidth": 64, "PicOffset": offset, "PicID": pic_id,
                    "PicSpeed": speed_ms, "PicData": pic})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PIXOO_HOST", "192.168.1.8"))
    args = ap.parse_args()

    print("== probe 1: contract control (3 solids, 600 ms) ==")
    send_animation(args.host, [frame_buffer(solid(c)) for c in
                               ((255, 0, 0), (0, 255, 0), (0, 0, 255))], 600)
    input("EYEBALL: display cycling R->G->B? [Enter] ")

    print("\n== probe 2: real shape (8 sun+text frames, 200 ms) ==")
    send_animation(args.host, [frame_buffer(sun_frame(i)) for i in range(8)], 200)
    input("EYEBALL: sun pulsing, text F0-F7 static-ish? [Enter] ")

    print("\n== probe 3: negative control (PicNum 1, static) ==")
    post(args.host, {"Command": "Draw/SendHttpGif", "PicNum": 1, "PicWidth": 64,
                     "PicOffset": 0, "PicID": 9, "PicSpeed": 1000,
                     "PicData": frame_buffer(solid((0, 0, 255)))})
    input("EYEBALL: solid blue, NO motion? [Enter] ")
    print("done.")


if __name__ == "__main__":
    main()
