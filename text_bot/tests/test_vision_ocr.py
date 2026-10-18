import io
import json
import unittest
from unittest.mock import patch

from PIL import Image
from utils.providers import ProcessingConfig
from utils.vision_ocr import make_crops, parse_regions, extract_page, reconcile_audit


def sample(height=10195):
    output = io.BytesIO()
    Image.new("RGB", (800, height), "white").save(output, format="PNG")
    return output.getvalue()


def region(text, box):
    return {"text": text, "box": box, "kind": "speech"}


class VisionTests(unittest.TestCase):
    def test_long_strip_keeps_full_resolution_and_covers_every_pixel(self):
        crops = make_crops(sample(), "page.jpg")
        self.assertGreater(len(crops), 3)
        self.assertEqual(crops[0].top, 0)
        self.assertEqual(crops[-1].bottom, 10195)
        for first, second in zip(crops, crops[1:]):
            self.assertEqual(first.core_bottom, second.core_top)
            self.assertGreater(first.bottom, second.top)
        for crop in crops:
            self.assertEqual(Image.open(io.BytesIO(crop.data)).width, 800)

    def test_order_is_top_to_bottom_and_languages_are_preserved(self):
        payload = {"regions": [region("나는 너에게", [30, 700, 500, 760]), region("Keep English EXACTLY.", [40, 20, 750, 80]) ]}
        with patch("utils.vision_ocr.complete_external", return_value=json.dumps(payload)):
            text = extract_page(sample(1000), "test.png", ProcessingConfig(provider="gpt", ocr_double_check=False))
        self.assertEqual(text, "Keep English EXACTLY.\n\n나는 너에게")

    def test_audit_cannot_drop_a_separate_column_or_bubble(self):
        first = [region("Left", [0, 0, 400, 100]), region("Right", [600, 0, 1000, 100])]
        self.assertEqual(len(reconcile_audit(first, first[:1])), 2)

    def test_invalid_or_truncated_json_is_never_accepted(self):
        for raw in ('{"regions":[', 'hello', '{"regions":[{"text":"x","box":[1,2,1,2]}]}'):
            with self.assertRaises(RuntimeError):
                parse_regions(raw)

    def test_broken_region_aborts_page_instead_of_delivering_partial_text(self):
        with patch("utils.vision_ocr.complete_external", return_value="not JSON"):
            with self.assertRaises(RuntimeError):
                extract_page(sample(1000), "test.png", ProcessingConfig(provider="gpt", ocr_double_check=False))

    def test_illegible_text_gets_a_focused_rescue_before_keeping_marker(self):
        first = {"regions": [{**region("⟦illegible⟧", [100, 100, 500, 300]), "illegible": True}]}
        second = {"regions": [region("Clearly printed text", [100, 100, 900, 900])]}
        with patch("utils.vision_ocr.complete_external", side_effect=[json.dumps(first), json.dumps(second)]) as request:
            text = extract_page(sample(1000), "test.png", ProcessingConfig(provider="gpt", ocr_double_check=False))
        self.assertEqual(text, "Clearly printed text")
        self.assertEqual(request.call_count, 2)
