import base64
import importlib.util
import json
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path

import requests_mock
from PIL import Image

# pixoo64/_pixoo.py pulls HA-bound modules (_colors chain) at import. Stub the
# HA surface, then load the real modules under synthetic package names so the
# relative imports resolve without a Home Assistant checkout.
REPO_ROOT = Path(__file__).resolve().parent.parent
PIXOO64_DIR = REPO_ROOT / "custom_components" / "divoom_pixoo" / "pixoo64"
PKG_NAME = "pixoo_anim_test_pkg"
SUBPKG_NAME = PKG_NAME + ".pixoo64"

pkg = types.ModuleType(PKG_NAME)
pkg.__path__ = []
sys.modules[PKG_NAME] = pkg
subpkg = types.ModuleType(SUBPKG_NAME)
subpkg.__path__ = [str(PIXOO64_DIR)]
sys.modules[SUBPKG_NAME] = subpkg


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(SUBPKG_NAME + "." + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[SUBPKG_NAME + "." + name] = module
    spec.loader.exec_module(module)
    return module


def _stub_ha_modules():
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    exceptions = types.ModuleType("homeassistant.exceptions")
    helpers = types.ModuleType("homeassistant.helpers")
    template_mod = types.ModuleType("homeassistant.helpers.template")

    class HomeAssistant:
        pass

    class TemplateError(Exception):
        pass

    class Template:
        def __init__(self, *args, **kwargs):
            pass

        def async_render(self, *args, **kwargs):
            return ""

    core.HomeAssistant = HomeAssistant
    exceptions.TemplateError = TemplateError
    template_mod.Template = Template
    template_mod.TemplateError = TemplateError
    helpers.template = template_mod
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.exceptions", exceptions)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.template", template_mod)


_stub_ha_modules()
_gif = _load_module("_gif", PIXOO64_DIR / "_gif.py")
_load_module("_colors", PIXOO64_DIR / "_colors.py")
_load_module("_font", PIXOO64_DIR / "_font.py")
_pixoo_mod = _load_module("_pixoo", PIXOO64_DIR / "_pixoo.py")
Pixoo = _pixoo_mod.Pixoo
MAX_ANIMATION_FRAMES = _gif.MAX_ANIMATION_FRAMES
clamp_pic_speed = _gif.clamp_pic_speed
extract_frames = _gif.extract_frames
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


@requests_mock.Mocker()
class TestExtractFrames(unittest.TestCase):
    def test_static_image_yields_one_frame(self, m):
        img = Image.new("RGB", (8, 8), (255, 0, 0))
        frames, speed = extract_frames(img)
        self.assertEqual(1, len(frames))
        self.assertEqual(200, speed)

    def test_animated_gif_yields_all_frames_with_mean_delay(self, m):
        img = make_gif([(255, 0, 0), (0, 255, 0), (0, 0, 255)],
                       durations=[100, 200, 300])
        frames, speed = extract_frames(img)
        self.assertEqual(3, len(frames))
        self.assertEqual(200, speed)  # mean of 100/200/300

    def test_speed_override_wins_and_clamps(self, m):
        img = make_gif([(255, 0, 0), (0, 255, 0)], durations=[100, 100])
        _, speed = extract_frames(img, speed_override=5)
        self.assertEqual(50, speed)  # clamped to floor
        _, speed = extract_frames(img, speed_override=99999)
        self.assertEqual(2000, speed)  # clamped to ceiling

    def test_frame_cap(self, m):
        img = make_gif([(i % 256, 0, 0) for i in range(MAX_ANIMATION_FRAMES + 5)])
        frames, _ = extract_frames(img)
        self.assertEqual(MAX_ANIMATION_FRAMES, len(frames))

    def test_resize_applies_to_every_frame(self, m):
        img = make_gif([(255, 0, 0), (0, 255, 0)], size=(16, 16))
        frames, _ = extract_frames(img, width=8, height=8)
        self.assertEqual(2, len(frames))
        for frame in frames:
            self.assertEqual((8, 8), frame.size)

    def test_clamp_pic_speed_rejects_garbage(self, m):
        self.assertEqual(200, clamp_pic_speed(None))
        self.assertEqual(200, clamp_pic_speed("fast"))
        self.assertEqual(50, clamp_pic_speed(-10))
        self.assertEqual(2000, clamp_pic_speed(10**9))


if __name__ == "__main__":
    unittest.main()
