import io
import json
import unittest
from unittest.mock import patch

from PIL import Image
from utils.providers import ProcessingConfig, get_processing_config
from utils.vision_ocr import make_crops, parse_regions, parse_parts, extract_page


def sample(height=10195, width=64):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def region(text, box=None):
    return {"text": text, "box": box or [10, 100, 900, 200], "kind": "speech"}


def batch(parts):
    return json.dumps({"parts": [{"part": n, "regions": records} for n, records in parts]}, ensure_ascii=False)


class VisionTests(unittest.TestCase):
    def test_images_up_to_16000_are_kept_byte_for_byte(self):
        for height in (360, 10195, 16000):
            data = sample(height)
            crops = make_crops(data, "page.png")
            self.assertEqual(len(crops), 1)
            self.assertIs(crops[0].data, data)
            self.assertEqual(crops[0].name, "page.png")

    def test_taller_images_have_exactly_three_full_width_parts_with_no_gaps(self):
        for height in (16001, 48001, 96001):
            crops = make_crops(sample(height), "page.png")
            self.assertEqual(len(crops), 3)
            self.assertEqual(crops[0].top, 0)
            self.assertEqual(crops[-1].bottom, height)
            for first, second in zip(crops, crops[1:]):
                self.assertEqual(first.core_bottom, second.core_top)
                self.assertGreater(first.bottom, second.top)
            for crop in crops:
                self.assertEqual(Image.open(io.BytesIO(crop.data)).width, 64)

    def test_one_successful_small_image_request_has_no_audit_or_detail_requests(self):
        raw = batch([(1, [region("Keep English EXACTLY.", [10, 10, 900, 40]),
                          region("⟦غير مقروء⟧", [10, 100, 900, 160]),
                          region("123456789012", [10, 200, 900, 260])])])
        data = sample()
        with patch("utils.vision_ocr.complete_external", return_value=raw) as request:
            text = extract_page(data, "page.png", ProcessingConfig(provider="gpt"))
        request.assert_called_once()
        self.assertIs(request.call_args.args[2][0][0], data)
        self.assertIn("123456789012", text)

    def test_three_parts_are_sent_together_and_reordered_by_part_and_coordinates(self):
        raw = batch([(3, [region("마지막")]), (1, [region("FIRST")]), (2, [
            region("第二", [10, 500, 900, 550]), region("MIDDLE", [10, 200, 900, 250])])])
        with patch("utils.vision_ocr.complete_external", return_value=raw) as request:
            text = extract_page(sample(18000), "page.png", ProcessingConfig(provider="gpt"))
        request.assert_called_once()
        self.assertEqual(len(request.call_args.args[2]), 3)
        self.assertEqual(text, "FIRST\n\nMIDDLE\n\n第二\n\n마지막")

    def test_partial_batch_is_retried_as_a_batch_never_as_separate_parts(self):
        partial = batch([(1, [region("first")]), (2, [])])
        complete = batch([(1, [region("first")]), (2, []), (3, [region("last")])])
        with patch("utils.vision_ocr.complete_external", side_effect=[partial, complete]) as request:
            text = extract_page(sample(18000), "page.png", ProcessingConfig(provider="deepseek", deepseek_token="test"))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(len(c.args[2]) == 3 for c in request.call_args_list))
        self.assertEqual(text, "first\n\nlast")

    def test_broken_batch_is_never_accepted_as_partial_page(self):
        with patch("utils.vision_ocr.complete_external", return_value=batch([(1, [])])) as request:
            with self.assertRaises(RuntimeError):
                extract_page(sample(18000), "page.png", ProcessingConfig(provider="gpt"))
        self.assertEqual(request.call_count, 2)

    def test_duplicate_or_missing_part_ids_are_rejected(self):
        for raw in (batch([(1, []), (1, []), (3, [])]), batch([(0, []), (2, []), (3, [])])):
            with self.assertRaises(RuntimeError):
                parse_parts(raw, 3)

    def test_context_overlap_is_not_repeated_and_other_text_is_not_dropped(self):
        raw = batch([(1, [region("edge", [10, 965, 900, 980])]),
                     (2, [region("edge", [10, 0, 900, 10]), region("middle", [10, 400, 900, 450])]),
                     (3, [region("last", [10, 500, 900, 550])])])
        with patch("utils.vision_ocr.complete_external", return_value=raw):
            text = extract_page(sample(18000), "page.png", ProcessingConfig(provider="gpt"))
        self.assertEqual(text.count("edge"), 1)
        self.assertIn("middle", text)
        self.assertIn("last", text)

    def test_invalid_or_truncated_json_is_rejected(self):
        for raw in ('{"regions":[', 'hello', '{"regions":[{"text":"x","box":[1,2,1,2]}]}'):
            with self.assertRaises(RuntimeError):
                parse_regions(raw)

    def test_obsolete_owner_crop_settings_cannot_reenable_many_small_parts(self):
        with patch("utils.providers.load_processing_config", return_value={
                "provider": "gpt", "ocr_crop_height": 1700, "ocr_double_check": True, "ocr_rescue_limit": 12}):
            config = get_processing_config()
        with patch("utils.vision_ocr.complete_external", return_value=batch([(1, [])])) as request:
            extract_page(sample(10000), "page.png", config)
        request.assert_called_once()
        self.assertEqual(len(request.call_args.args[2]), 1)
