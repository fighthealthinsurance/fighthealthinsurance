"""Template <img> tags carry width and height that match the image file, so
the browser reserves the right space before the image loads."""

import re
from pathlib import Path

from django.test import SimpleTestCase
from PIL import Image

import fighthealthinsurance

APP_DIR = Path(fighthealthinsurance.__file__).resolve().parent
TEMPLATES = APP_DIR / "templates"
STATIC = APP_DIR / "static"

IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
STATIC_SRC = re.compile(r"\{%\s*static\s+'(images/[^']+)'\s*%\}")
WIDTH = re.compile(r'\swidth="(\d+)"')
HEIGHT = re.compile(r'\sheight="(\d+)"')

# Pages whose logos had no size before; every <img> on them must have one.
FULLY_SIZED = ["partials/featured_section.html", "about_us.html"]


def _img_tags(template: Path) -> list[str]:
    return IMG_TAG.findall(template.read_text(encoding="utf-8"))


class TestTemplateImageSizes(SimpleTestCase):
    def test_press_logo_templates_size_every_image(self):
        for name in FULLY_SIZED:
            for tag in _img_tags(TEMPLATES / name):
                with self.subTest(template=name, tag=tag):
                    self.assertRegex(tag, WIDTH)
                    self.assertRegex(tag, HEIGHT)

    def test_sized_static_images_match_their_file_ratio(self):
        checked = 0
        for template in sorted(TEMPLATES.rglob("*.html")):
            for tag in _img_tags(template):
                src = STATIC_SRC.search(tag)
                width = WIDTH.search(tag)
                height = HEIGHT.search(tag)
                if not (src and width and height) or src.group(1).endswith(".svg"):
                    continue
                with Image.open(STATIC / src.group(1)) as image:
                    file_w, file_h = image.size
                declared = int(width.group(1)) / int(height.group(1))
                actual = file_w / file_h
                with self.subTest(template=template.name, src=src.group(1)):
                    self.assertAlmostEqual(declared / actual, 1.0, delta=0.01)
                checked += 1
        self.assertGreater(checked, 20)

    def test_logo_row_keeps_its_ratio_when_shrunk(self):
        css = (STATIC / "css" / "custom.css").read_text(encoding="utf-8")
        rule = re.search(r"\.fhi-logo-row img \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("height: auto", rule.group(1))
