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

class RecoveryTests(unittest.TestCase):
    def test_failed_batch_gets_new_gpt_thread_in_same_project_then_another_provider(self):
        from utils import providers
        from test_providers import account_token
        import json
        config = ProcessingConfig(provider="gpt", chatgpt_access_token=account_token(),
            gemini_cookie_header="SID=test", deepseek_token="test")
        state = {"gpt": {"conversation_id": "broken", "parent_message_id": "old-parent",
                          "gizmo_id": "project-123", "project_name": "Chapter"}}
        observed = []
        def choose(provider, value):
            state[provider] = value
        def completion(provider, prompt, images, **kw):
            observed.append((provider, dict(state.get(provider, {})), images, kw))
            if provider == "gpt":
                raise RuntimeError("service failed")
            return json.dumps({"parts":[{"part": i, "regions":[{"text": text, "box":[10,400,900,450]}]}
                for i,text in enumerate(("FIRST","SECOND","LAST"),1)]})
        out = io.BytesIO(); Image.new("RGB",(24,18000),"white").save(out,format="PNG")
        settings = {"_processing_config": config, "include_sfx": False}
        with patch("utils.providers.load_provider_thread", side_effect=lambda p:{"state": state.get(p,{})}), \
             patch("utils.providers.select_provider_thread", side_effect=choose), \
             patch.object(bot,"load_accounts_data",return_value={"accounts":[]}), \
             patch("utils.vision_ocr.complete_external",side_effect=completion), \
             patch("utils.storage.save_processing_config") as save:
            text = bot.extract_page_with_fallback(1,out.getvalue(),"page.png",False,settings)
        self.assertEqual(text,"FIRST\n\nSECOND\n\nLAST")
        self.assertEqual([o[0] for o in observed],["gpt","gpt","gemini"])
        self.assertEqual(observed[0][1]["conversation_id"],"broken")
        self.assertNotIn("conversation_id",observed[1][1])
        self.assertEqual(observed[1][1]["gizmo_id"],"project-123")
        self.assertEqual(observed[1][1]["project_name"],"Chapter")
        self.assertTrue(all(len(o[2])==3 for o in observed))
        self.assertEqual(observed[0][2],observed[1][2])
        self.assertTrue(all(o[3]["config"].ocr_scope=="dialogue" for o in observed))
        self.assertEqual(settings["_processing_config"].provider,"gpt")
        save.assert_not_called()

    def test_success_never_resets_a_conversation_or_calls_another_provider(self):
        config=ProcessingConfig(provider="deepseek",deepseek_token="test")
        with patch.object(bot,"load_accounts_data",return_value={"accounts":[]}), \
             patch.object(bot,"extract_text_from_single_image",return_value="SOURCE") as extract, \
             patch("utils.providers.new_thread") as new:
            self.assertEqual(bot.extract_page_with_fallback(1,b"image","page.png",True,
                {"_processing_config":config}),"SOURCE")
        extract.assert_called_once()
        self.assertEqual(extract.call_args.args[4]["_processing_config"].provider,"deepseek")
        self.assertTrue(extract.call_args.args[3])
        new.assert_not_called()

    def test_all_providers_failing_is_bounded_and_explicit(self):
        with patch.object(bot,"load_accounts_data",return_value={"accounts":[]}), \
             patch.object(bot,"extract_text_from_single_image",side_effect=RuntimeError("offline")) as extract, \
             patch("utils.providers.new_thread"):
            with self.assertRaisesRegex(RuntimeError,"الاحتياطيين"):
                bot.extract_page_with_fallback(1,b"image","page.png",False,
                    {"_processing_config":ProcessingConfig(provider="dark")})
        self.assertEqual(extract.call_count,2)


class CapabilityAndSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_protocols_with_a_real_thinking_switch_show_the_mode_step(self):
        for provider in ("gpt","gemini","dark","deepseek","qwen"):
            settings={"_processing_config":ProcessingConfig(provider=provider)}
            fake=SimpleNamespace(confirmed=True,thinking_enabled=True,wait=AsyncMock())
            with patch.object(bot,"ModeSelectView",return_value=fake) as view, \
                 patch.object(bot,"send_mode_selection",new_callable=AsyncMock) as send:
                result=await bot.select_processing_mode(SimpleNamespace(),1,settings)
            self.assertEqual(result,provider in ("qwen","deepseek"))
            self.assertEqual(view.call_count,int(provider in ("qwen","deepseek")))
            self.assertEqual(send.await_count,view.call_count)

    async def test_translation_toggle_and_target_reach_the_real_stage_for_every_provider(self):
        for provider in ("qwen","gpt","gemini","dark","deepseek"):
            for enabled in (False,True):
                settings={"_processing_config":ProcessingConfig(provider=provider),
                    "translate_enabled":enabled,"translate_target":"en","include_sfx":False}
                with patch.object(bot,"vision_translate_chapter",return_value="Translated") as translate, \
                     patch.object(bot,"edit_status",new_callable=AsyncMock):
                    result,error=await bot.run_chapter_translation_stage(None,1,[(b"img","page.png")],
                        {"page.png":"SOURCE"},"SOURCE",settings,False)
                self.assertIsNone(error)
                self.assertEqual(translate.call_count,int(enabled))
                if enabled:
                    self.assertEqual(result,"Translated")
                    self.assertEqual(translate.call_args.args[2]["translate_target"],"en")
                    self.assertFalse(translate.call_args.args[2]["include_sfx"])

    async def test_user_sfx_overrides_console_trial_scope_for_every_external_provider(self):
        for provider in ("gpt","gemini","dark","deepseek"):
            for enabled in (False,True):
                config=ProcessingConfig(provider=provider,ocr_scope="dialogue" if enabled else "all")
                with patch("utils.vision_ocr.extract_page",return_value="SOURCE") as extract:
                    bot.extract_text_from_single_image(1,b"img","page.png",False,
                        {"_processing_config":config,"include_sfx":enabled,"bubble_spacing":False})
                self.assertEqual(extract.call_args.args[2].ocr_scope,"all" if enabled else "dialogue")
                self.assertFalse(extract.call_args.kwargs["bubble_spacing"])

    async def test_bad_primary_credentials_do_not_block_an_available_image_backup(self):
        settings={"_processing_config":ProcessingConfig(provider="gpt",chatgpt_access_token="expired")}
        destination=SimpleNamespace(send=AsyncMock())
        self.assertTrue(await bot.ensure_provider_available(destination,settings))
        destination.send.assert_not_awaited()

    async def test_failed_page_is_marked_without_losing_later_page_order(self):
        def extract(user,data,name,*args,**kwargs):
            if name=="02.png":
                raise RuntimeError("all offline")
            return "FIRST" if name=="01.png" else "LAST"
        settings={"_processing_config":ProcessingConfig(provider="dark")}
        with patch.object(bot,"extract_page_with_fallback",side_effect=extract), \
             patch.object(bot,"edit_status",new_callable=AsyncMock):
            text,empty,failed,pages=await bot.run_extraction_pipeline(None,1,
                [(b"a","01.png"),(b"b","02.png"),(b"c","03.png")],False,settings)
        self.assertEqual(failed,["02.png"])
        self.assertEqual(empty,0)
        self.assertEqual(pages["02.png"],"")
        self.assertIn("تعذر استخراج",text)
        self.assertLess(text.index("FIRST"),text.index("02.png"))
        self.assertLess(text.index("02.png"),text.index("LAST"))

class ChapterRecoveryStateTests(unittest.TestCase):
    def test_rest_of_chapter_keeps_working_backup_without_changing_global_selection(self):
        config=ProcessingConfig(provider="deepseek",deepseek_token="test",gemini_cookie_header="SID=test")
        settings={"_processing_config":config,"_recovery_state":{"preferred":"deepseek"}}
        providers=[]
        def extract(user,data,name,thinking,current,**kw):
            p=current["_processing_config"].provider;providers.append(p)
            if p=="deepseek":raise RuntimeError("offline")
            return "SOURCE"
        with patch.object(bot,"load_accounts_data",return_value={"accounts":[]}), \
             patch.object(bot,"extract_text_from_single_image",side_effect=extract), \
             patch("utils.providers.new_thread"):
            for name in ("01.png","02.png"):
                self.assertEqual(bot.extract_page_with_fallback(1,b"image",name,False,settings),"SOURCE")
        self.assertEqual(providers,["deepseek","deepseek","gemini","gemini"])
        self.assertEqual(settings["_processing_config"].provider,"deepseek")
        self.assertEqual(settings["_recovery_state"]["preferred"],"gemini")
