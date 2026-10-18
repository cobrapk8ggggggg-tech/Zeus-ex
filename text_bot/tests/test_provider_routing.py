import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
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

    async def test_owner_can_save_global_choice(self):
        config = ProcessingConfig()
        panel = bot.AdminProviderView(config)
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), data={"values": ["gpt"]}, response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        with patch.object(bot, "get_processing_config", return_value=config), patch.object(bot, "validate_provider"), patch.object(bot, "save_processing_config") as save:
            await panel.on_provider_select(interaction)
        save.assert_called_once_with(provider="gpt")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once()

    async def test_nonowner_cannot_change_provider_even_with_callback(self):
        panel = bot.AdminProviderView(ProcessingConfig())
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID + 1), data={"values": ["gpt"]}, response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "save_processing_config") as save:
            await panel.on_provider_select(interaction)
        save.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_nonowner_cannot_submit_session_modal(self):
        modal = bot.ProcessingSessionModal("gpt", ProcessingConfig())
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID + 1), response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "save_processing_config") as save:
            await modal.on_submit(interaction)
        save.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_owner_model_change_keeps_existing_session_and_replies_privately(self):
        config = ProcessingConfig(provider="gpt", chatgpt_access_token="private-token")
        modal = bot.ProcessingSessionModal("gpt", config)
        modal.model._value = "owner-model"
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, "get_processing_config", return_value=config), patch.object(bot, "validate_provider") as validate, patch.object(bot, "save_processing_config") as save:
            await modal.on_submit(interaction)
        save.assert_called_once_with(chatgpt_model="owner-model")
        self.assertEqual(validate.call_args.args[0]["_processing_config"].chatgpt_access_token, "private-token")
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.assertNotIn("private-token", str(interaction.followup.send.call_args))

    async def test_zx_is_private_and_has_no_prefix_alias(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), response=SimpleNamespace(send_message=AsyncMock()))
        await bot.zx.callback(interaction)
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertIsNone(bot.bot.get_command("admin"))
        self.assertIsNone(bot.bot.get_command("لوحة"))
        denied = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID + 1), response=SimpleNamespace(send_message=AsyncMock()))
        await bot.zx.callback(denied)
        self.assertNotIn("view", denied.response.send_message.call_args.kwargs)

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
