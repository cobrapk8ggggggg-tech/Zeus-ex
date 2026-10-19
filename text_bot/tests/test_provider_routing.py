import asyncio
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from PIL import Image
from utils.storage import _normalize_profile
from utils.providers import ProcessingConfig, selected_provider, snapshot_processing_settings


class RoutingTests(unittest.TestCase):
    def setUp(self):
        # Stub only the account/network boundary; keep real extraction,
        # translation, fallback, and UI paths executing.
        validator = patch.object(bot, "validate_provider", side_effect=selected_provider)
        validator.start()
        self.addCleanup(validator.stop)
        config_patch = patch("utils.providers.load_processing_config", return_value={"provider": "qwen"})
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_existing_profiles_inherit_default(self):
        profile = _normalize_profile({"settings": {"include_sfx": True}})
        self.assertNotIn("provider", profile["settings"])
        self.assertTrue(profile["settings"]["include_sfx"])

    def test_single_image_and_text_route_without_qwen_accounts(self):
        with patch.object(bot, "complete_external", return_value="Hello source") as external, patch.object(bot, "get_valid_qwen_token", side_effect=AssertionError("Qwen must not run")):
            for provider in ("gemini", "gpt"):
                settings = {"_processing_config": ProcessingConfig(provider=provider)}
                result, chat_id = bot._do_single_extraction(1, b"image", "01.png", False, settings, "old-qwen-id")
                self.assertEqual(result, "Hello source")
                self.assertIsNone(chat_id)
                self.assertEqual(external.call_args.args[0], provider)
                bot.request_text_completion(1, "translate", settings=settings)
                self.assertEqual(external.call_args.args[0], provider)

    def test_qwen_default_still_uses_existing_text_client(self):
        with patch.object(bot, "request_qwen_text_completion", return_value="translation") as qwen, patch.object(bot, "complete_external") as external:
            self.assertEqual(bot.request_text_completion(1, "prompt"), "translation")
            qwen.assert_called_once()
            external.assert_not_called()

    def test_qwen_obeys_the_same_16000_threshold_and_three_part_batch(self):
        settings = {"_processing_config": ProcessingConfig(provider="qwen")}
        for height in (16000, 16001):
            output = io.BytesIO()
            Image.new("RGB", (32, height), "white").save(output, format="PNG")
            with patch.object(bot, "_extract_tiles_batched", return_value="batched", autospec=True) as batch, \
                    patch.object(bot, "_do_single_extraction", return_value=("whole", None)) as whole:
                result = bot.extract_text_from_single_image(1, output.getvalue(), "page.png", False, settings)
            if height == 16000:
                whole.assert_called_once()
                batch.assert_not_called()
                self.assertEqual(result, "whole")
            else:
                whole.assert_not_called()
                batch.assert_called_once()
                self.assertEqual(len(batch.call_args.args[1]), 3)
                self.assertEqual(result, "batched")

    def test_failed_qwen_batch_does_not_fall_back_to_extra_full_or_subdivided_images(self):
        output = io.BytesIO()
        Image.new("RGB", (32, 18000), "white").save(output, format="PNG")
        with patch.object(bot, "_extract_tiles_batched", side_effect=RuntimeError("failed"), autospec=True), \
                patch.object(bot, "_do_single_extraction") as whole:
            with self.assertRaises(RuntimeError):
                bot.extract_text_from_single_image(1, output.getvalue(), "page.png", False,
                                                   {"_processing_config": ProcessingConfig(provider="qwen")})
        whole.assert_not_called()

    def test_batch_routes_images_in_order(self):
        images = [(b"first", "01.png"), (b"second", "02.png")]
        with patch.object(bot, "complete_external", return_value="answer") as external:
            bot._do_vision_request(1, images, "prompt", thinking_enabled=True, settings={"_processing_config": ProcessingConfig(provider="gpt")})
            self.assertEqual(external.call_args.args[:3], ("gpt", "prompt", images))

    def test_external_failure_never_falls_back_to_qwen(self):
        with patch.object(bot, "complete_external", side_effect=RuntimeError("failed")), patch.object(bot, "request_qwen_text_completion") as qwen:
            with self.assertRaises(RuntimeError):
                bot.request_text_completion(1, "prompt", settings={"_processing_config": ProcessingConfig(provider="gemini")})
            qwen.assert_not_called()

    def test_text_translation_and_foreign_review_keep_provider(self):
        settings = {"_processing_config": ProcessingConfig(provider="gpt"), "translate_target": "ar"}
        with patch.object(bot, "request_text_completion", return_value="مرحبا") as completion, patch.object(bot, "_review_foreign_text", side_effect=lambda user, text, **kw: text) as review:
            bot.translate_chapter_text(1, "Hello", settings)
            self.assertEqual(completion.call_args.kwargs["settings"], settings)
            self.assertEqual(review.call_args.kwargs["settings"], settings)
        with patch.object(bot, "request_text_completion", return_value='{"1":"مرحبا"}') as completion, patch.object(bot, "FOREIGN_REVIEW_ENABLED", True):
            self.assertEqual(bot._review_foreign_text(1, "Hello", settings=settings), "مرحبا")
            self.assertEqual(completion.call_args.kwargs["settings"], settings)

    def test_vision_fallback_preserves_provider(self):
        settings = {"_processing_config": ProcessingConfig(provider="gemini"), "translate_target": "ar"}
        with patch.object(bot, "_do_vision_request", side_effect=RuntimeError("failed")) as vision, patch.object(bot, "translate_chapter_text", return_value="مرحبا") as text, patch.object(bot, "_review_foreign_text", side_effect=lambda user, value, **kw: value), patch.object(bot.time, "sleep"):
            bot.vision_translate_chapter(1, [(b"image", "01.png")], settings, extracted_texts={"01.png": "Hello"})
            self.assertEqual(vision.call_args.kwargs["settings"], settings)
            self.assertEqual(selected_provider(text.call_args.args[2]), "gemini")

    def test_batched_tiles_keep_provider(self):
        settings = {"_processing_config": ProcessingConfig(provider="gpt")}
        reply = "<<<part1>>>\nHello\n<<<part2>>>\nWorld"
        with patch.object(bot, "_do_vision_request", return_value=reply) as request, patch.object(bot, "_parse_marker_sections", return_value=["Hello", "World"]):
            result = bot._extract_tiles_batched(1, [(b"first", "a", 10), (b"second", "b", 10)], True, settings, progress_cb=None, display_name="page", label_prefix="")
            self.assertIn("Hello", result)
            self.assertEqual(request.call_args.kwargs["settings"], settings)


class UIAndTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_settings_never_show_provider_menu_or_names(self):
        user = SimpleNamespace(id=1)
        with patch.object(bot, "get_user_profile", return_value=_normalize_profile({"settings": {"provider": "gpt"}})):
            panel = bot.SettingsPanel(user)
        menus = [item for item in panel.walk_children() if isinstance(item, bot.discord.ui.Select)]
        values = {option.value for menu in menus for option in menu.options}
        self.assertFalse(values & {"qwen", "gemini", "gpt"})
        visible = str(panel.to_components()) + str(bot.help_sections()) + str(bot.mode_selection_sections())
        for name in ("Gemini", "GPT", "Qwen", "مزود"):
            self.assertNotIn(name, visible)
        self.assertFalse(hasattr(panel, "on_provider_select"))

    async def test_snapshot_ignores_old_user_choice_and_survives_global_change(self):
        with patch("utils.providers.load_processing_config", return_value={"provider": "gemini"}):
            settings = snapshot_processing_settings({"provider": "gpt", "include_sfx": True})
        with patch("utils.providers.load_processing_config", return_value={"provider": "qwen"}):
            self.assertEqual(selected_provider(settings), "gemini")
            self.assertEqual(selected_provider({"provider": "gpt"}), "qwen")
        self.assertTrue(settings["include_sfx"])
        self.assertNotIn("provider", settings)
        self.assertNotIn("provider", _normalize_profile({"settings": {"provider": "gpt"}})["settings"])

    async def test_translation_command_worker_keeps_settings_for_text_and_images(self):
        settings = {"_processing_config": ProcessingConfig(provider="gpt"), "include_sfx": True}
        # Stop after translation: this test must never debit a real account.
        for images, worker in ((None, "translate_chapter_text"), ([(b"image", "01.png")], "vision_translate_chapter")):
            with self.subTest(worker=worker), patch.object(bot, worker, return_value="مرحبا") as completion, patch.object(bot, "edit_status", new_callable=AsyncMock), patch.object(bot, "resolve_and_consume_point", new_callable=AsyncMock, return_value=(False, {}, None)):
                await bot.run_translation_with_status(None, None, 1, None, "Hello", "chapter", "ar", "txt", images=images, settings=settings)
                self.assertEqual(selected_provider(completion.call_args.args[2]), "gpt")
                self.assertTrue(completion.call_args.args[2]["include_sfx"])


if __name__ == "__main__":
    unittest.main()
