import base64
import json
import unittest
from io import BytesIO
from unittest import mock

import requests_mock
from PIL import Image

from custom_components.divoom_pixoo.pixoo64._gif import (
    MAX_ANIMATION_FRAMES,
    clamp_pic_speed,
    extract_frames,
)
from custom_components.divoom_pixoo.pixoo64 import _pixoo as _pixoo_mod
from custom_components.divoom_pixoo.pixoo64._pixoo import Pixoo
IP_ADDRESS = "FAKE_IP_ADDRESS"


def make_gif(colors, size=(8, 8), durations=None):
    frames = [Image.new("RGB", size, color) for color in colors]
    buf = BytesIO()
    save_kwargs = {"format": "GIF", "save_all": True, "append_images": frames[1:],
                   "loop": 0}
    if durations is not None:
        save_kwargs["duration"] = durations
    frames[0].save(buf, **save_kwargs)
    buf.seek(0)
    return Image.open(buf)


def posted_commands(m):
    return [json.loads(req.text) for req in m.request_history
            if req.url.endswith("/post")]


@requests_mock.Mocker()
class TestPushAnimation(unittest.TestCase):
    def make_pixoo(self, m):
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()  # drop the GetHttpGifId handshake from __init__
        return pixoo

    def test_multi_frame_push_resets_then_sends_offsets(self, m):
        m.post("/post", json={"error_code": 0, "PicId": 0})
        pixoo = self.make_pixoo(m)

        frames = [pixoo.get_buffer(), pixoo.get_buffer(), pixoo.get_buffer()]
        pixoo.push_animation(frames, pic_speed=200)

        commands = posted_commands(m)
        self.assertEqual(1 + 3, len(commands))
        self.assertEqual("Draw/ResetHttpGifId", commands[0]["Command"])
        pic_ids = set()
        for offset, cmd in enumerate(commands[1:]):
            self.assertEqual("Draw/SendHttpGif", cmd["Command"])
            self.assertEqual(3, cmd["PicNum"])
            self.assertEqual(offset, cmd["PicOffset"])
            self.assertEqual(200, cmd["PicSpeed"])
            self.assertEqual(64, cmd["PicWidth"])
            pic_ids.add(cmd["PicID"])
            raw = base64.b64decode(cmd["PicData"])
            self.assertEqual(64 * 64 * 3, len(raw))
        self.assertEqual(1, len(pic_ids))  # shared PicID across frames

    def test_single_frame_push_keeps_static_path(self, m):
        m.post("/post", json={"error_code": 0, "PicId": 0})
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        pixoo.push_animation([pixoo.get_buffer()])

        commands = posted_commands(m)
        self.assertEqual(1, len(commands))
        self.assertEqual("Draw/SendHttpGif", commands[0]["Command"])
        self.assertEqual(1, commands[0]["PicNum"])
        self.assertEqual(1000, commands[0]["PicSpeed"])

    def test_push_after_animation_uses_fresh_pic_id(self, m):
        m.post("/post", json={"error_code": 0, "PicId": 0})
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        pixoo.push_animation([pixoo.get_buffer(), pixoo.get_buffer()])
        animated_ids = {cmd["PicID"] for cmd in posted_commands(m)[1:]}
        m.reset_mock()

        pixoo.push()
        static = posted_commands(m)[0]
        self.assertNotIn(static["PicID"], animated_ids)

    def test_empty_frames_push_nothing(self, m):
        m.post("/post", json={"error_code": 0, "PicId": 0})
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        pixoo.push_animation([])
        self.assertEqual([], posted_commands(m))

    def test_connection_reset_retries_frame(self, m):
        m.post("/post", [
            {"json": {"error_code": 0, "PicId": 0}},  # __init__ handshake
            {"json": {"error_code": 0}},  # ResetHttpGifId
            {"exc": ConnectionResetError(104, "Connection reset by peer")},
            {"json": {"error_code": 0}},  # retry succeeds
            {"json": {"error_code": 0}},  # frame 2
        ])
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        with self.assertNoLogs(level="WARNING"):
            pixoo.push_animation([pixoo.get_buffer(), pixoo.get_buffer()])

        commands = posted_commands(m)
        frames = [cmd for cmd in commands if cmd["Command"] == "Draw/SendHttpGif"]
        self.assertEqual(3, len(frames))  # failed frame + retry + frame 2
        self.assertEqual([0, 0, 1], [cmd["PicOffset"] for cmd in frames])

    def test_push_animation_paces_frames(self, m):
        m.post("/post", json={"error_code": 0, "PicId": 0})
        pixoo = Pixoo(IP_ADDRESS)
        pixoo.frame_pause = 0.15
        m.reset_mock()

        with mock.patch.object(_pixoo_mod.time, "sleep") as sleep:
            pixoo.push_animation([pixoo.get_buffer(), pixoo.get_buffer(), pixoo.get_buffer()])
        self.assertEqual([mock.call(0.15), mock.call(0.15)], sleep.call_args_list)

    def test_persistent_failure_falls_back_to_static(self, m):
        m.post("/post", [
            {"json": {"error_code": 0, "PicId": 0}},  # __init__ handshake
            {"json": {"error_code": 0}},  # ResetHttpGifId
            {"exc": ConnectionResetError(104, "Connection reset by peer")},
            {"exc": ConnectionResetError(104, "Connection reset by peer")},
            {"json": {"error_code": 0}},  # static fallback push
        ])
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        with self.assertLogs(level="WARNING"):
            pixoo.push_animation([pixoo.get_buffer(), pixoo.get_buffer()])

        commands = posted_commands(m)
        kinds = [(cmd["Command"], cmd.get("PicNum")) for cmd in commands]
        self.assertIn(("Draw/ResetHttpGifId", None), kinds)
        # Frame 0 failed twice: zero frames sent, no partial-animation
        # fallback; the next scheduled page draw recovers.
        self.assertEqual(1, len([k for k in kinds if k[0] == "Draw/ResetHttpGifId"]))
        self.assertEqual([], [k for k in kinds if k == ("Draw/SendHttpGif", 1)])

    def test_partial_failure_falls_back_to_static(self, m):
        m.post("/post", [
            {"json": {"error_code": 0, "PicId": 0}},  # __init__ handshake
            {"json": {"error_code": 0}},  # ResetHttpGifId
            {"json": {"error_code": 0}},  # frame 0 ok
            {"exc": ConnectionResetError(104, "Connection reset by peer")},
            {"exc": ConnectionResetError(104, "Connection reset by peer")},
            {"json": {"error_code": 0}},  # static fallback push
        ])
        pixoo = Pixoo(IP_ADDRESS)
        m.reset_mock()

        with self.assertLogs(level="WARNING"):
            pixoo.push_animation([pixoo.get_buffer(), pixoo.get_buffer()])

        commands = posted_commands(m)
        kinds = [(cmd["Command"], cmd.get("PicNum")) for cmd in commands]
        # Partial animation (1/2) re-pushes the first frame as a static page.
        self.assertEqual(("Draw/SendHttpGif", 1), kinds[-1])


class TestExtractFrames(unittest.TestCase):

    def test_static_image_yields_one_frame(self):
        img = Image.new("RGB", (8, 8), (255, 0, 0))
        frames, speed = extract_frames(img)
        self.assertEqual(1, len(frames))
        self.assertEqual(200, speed)

    def test_unsized_static_frame_survives_source_close(self):
        img = Image.new("RGB", (8, 8), (255, 0, 0))
        frames, _ = extract_frames(img)
        img.close()  # sensor closes the source after decode; frame must live on
        self.assertEqual((255, 0, 0), frames[0].convert("RGB").getpixel((0, 0)))

    def test_animated_gif_yields_all_frames_with_mean_delay(self):
        img = make_gif([(255, 0, 0), (0, 255, 0), (0, 0, 255)],
                       durations=[100, 200, 400])
        frames, speed = extract_frames(img)
        self.assertEqual(3, len(frames))
        self.assertEqual(233, speed)  # mean of 100/200/400, not the 200 default

    def test_speed_override_wins_and_clamps(self):
        img = make_gif([(255, 0, 0), (0, 255, 0)], durations=[100, 100])
        _, speed = extract_frames(img, speed_override=5)
        self.assertEqual(50, speed)  # clamped to floor
        _, speed = extract_frames(img, speed_override=99999)
        self.assertEqual(2000, speed)  # clamped to ceiling

    def test_frame_cap(self):
        img = make_gif([(i % 256, 0, 0) for i in range(MAX_ANIMATION_FRAMES + 5)])
        frames, _ = extract_frames(img)
        self.assertEqual(MAX_ANIMATION_FRAMES, len(frames))

    def test_resize_applies_to_every_frame(self):
        img = make_gif([(255, 0, 0), (0, 255, 0)], size=(16, 16))
        frames, _ = extract_frames(img, width=8, height=8)
        self.assertEqual(2, len(frames))
        for frame in frames:
            self.assertEqual((8, 8), frame.size)

    def test_clamp_pic_speed_rejects_garbage(self):
        self.assertEqual(200, clamp_pic_speed(None))
        self.assertEqual(200, clamp_pic_speed("fast"))
        self.assertEqual(50, clamp_pic_speed(-10))
        self.assertEqual(2000, clamp_pic_speed(10**9))


if __name__ == "__main__":
    unittest.main()
