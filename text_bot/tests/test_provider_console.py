import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from utils.provider_admin import ProviderConsole, TextModal, RecordPicker, _UPLOADS
from utils.providers import ProcessingConfig, parse_cookie_file


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_selected_provider_settings_are_visible(self):
        for provider in ("qwen", "gpt", "gemini"):
            panel = ProviderConsole(bot, ProcessingConfig(provider=provider))
            labels = [i.label for i in panel.walk_children() if isinstance(i, bot.discord.ui.Button)]
            self.assertLessEqual(panel.total_children_count, 40)
            expected = {"qwen": "إدارة الحسابات", "gpt": "مشاريع الحساب", "gemini": "رفع ملف الكوكيز"}
            for key, label in expected.items():
                self.assertEqual(label in labels, key == provider)

    async def test_owner_can_activate_provider_privately(self):
        config = ProcessingConfig(provider="gpt")
        panel = ProviderConsole(bot, config)
        panel.refresh = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), response=SimpleNamespace(defer=AsyncMock()))
        with patch("utils.provider_admin.get_processing_config", return_value=config), patch("utils.provider_admin.validate_provider"), patch("utils.provider_admin.save_processing_config") as save:
            await panel.action(interaction, "activate")
        save.assert_called_once_with(provider="gpt")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_nonowner_cannot_change_provider_or_submit_token(self):
        panel = ProviderConsole(bot, ProcessingConfig())
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID + 1), response=SimpleNamespace(send_message=AsyncMock()))
        with patch("utils.provider_admin.save_processing_config") as save:
            await panel.action(interaction, "activate")
            await TextModal(panel, "token").on_submit(interaction)
        save.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_cookie_upload_is_owner_dm_only_and_accepts_more_than_4000_characters(self):
        panel = ProviderConsole(bot, ProcessingConfig(provider="gemini"))
        raw = b"SID=" + b"a" * 9000
        attachment = SimpleNamespace(size=len(raw), read=AsyncMock(return_value=raw), filename="cookies.txt")
        dm = SimpleNamespace(id=1234, send=AsyncMock())
        message = SimpleNamespace(author=SimpleNamespace(id=bot.OWNER_ID), channel=dm, content="", attachments=[attachment])
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID, create_dm=AsyncMock(return_value=dm)), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot.bot, "wait_for", new_callable=AsyncMock, return_value=message) as wait, patch("utils.provider_admin.save_processing_config") as save:
            await panel.receive_file(interaction, "cookies", panel.config)
        check = wait.call_args.kwargs["check"]
        self.assertTrue(check(message))
        self.assertFalse(check(SimpleNamespace(author=SimpleNamespace(id=1), channel=dm, content="", attachments=[attachment])))
        self.assertFalse(check(SimpleNamespace(author=message.author, channel=SimpleNamespace(id=999), content="", attachments=[attachment])))
        self.assertEqual(len(save.call_args.kwargs["gemini_cookie_header"]), len(raw))
        self.assertNotIn("a" * 100, str(interaction.followup.send.call_args_list))

    async def test_project_selection_updates_persistent_project(self):
        panel = ProviderConsole(bot, ProcessingConfig(provider="gpt"))
        panel.refresh = AsyncMock()
        picker = RecordPicker(panel, [{"id": "project-id", "name": "Manga"}], "projects")
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), data={"values": ["0"]}, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch("utils.provider_admin.choose_thread") as choose:
            await picker.selected(interaction)
        self.assertEqual(choose.call_args.args[1]["gizmo_id"], "project-id")
        self.assertNotIn("conversation_id", choose.call_args.args[1])

    async def test_model_and_project_pickers_keep_components_v2_compatible(self):
        panel = ProviderConsole(bot, ProcessingConfig(provider="gpt"))
        interaction = SimpleNamespace(edit_original_response=AsyncMock())
        await panel.picker(interaction, [{"slug": "auto", "title": "Auto"}], "models")
        kwargs = interaction.edit_original_response.call_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertIsInstance(kwargs["view"], bot.discord.ui.LayoutView)
        self.assertEqual(kwargs["view"].to_components()[0]["type"], 17)

    async def test_cookie_wait_cannot_overlap_an_attachment_wait(self):
        panel = ProviderConsole(bot, ProcessingConfig(provider="gpt"))
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        _UPLOADS.add(bot.OWNER_ID)
        try:
            with patch.object(bot.bot, "wait_for", new_callable=AsyncMock) as wait:
                await panel.receive_file(interaction, "attachment", panel.config)
            wait.assert_not_called()
            self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        finally:
            _UPLOADS.discard(bot.OWNER_ID)

    async def test_zx_is_private_and_has_no_prefix_alias(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, "get_processing_config", return_value=ProcessingConfig()):
            await bot.zx.callback(interaction)
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.assertIsInstance(interaction.followup.send.call_args.kwargs["view"], ProviderConsole)
        self.assertIsNone(bot.bot.get_command("admin"))
        denied = SimpleNamespace(user=SimpleNamespace(id=bot.OWNER_ID + 1), response=SimpleNamespace(send_message=AsyncMock()))
        await bot.zx.callback(denied)
        self.assertNotIn("view", denied.response.send_message.call_args.kwargs)


class CookieTests(unittest.TestCase):
    def test_raw_header_json_netscape_and_export_formats(self):
        samples = [b"Cookie: SID=a=b; HSID=two", b'[{"domain":".google.com","name":"SID","value":"a=b"}]',
                   b"# Netscape HTTP Cookie File\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tSID\ta=b",
                   b"export BROWSER_COOKIE_HEADER='SID=a=b; HSID=two'"]
        for sample in samples:
            self.assertIn("SID=a=b", parse_cookie_file(sample))

    def test_json_filters_other_domains(self):
        self.assertEqual(parse_cookie_file(b'[{"domain":".google.com","name":"SID","value":"ok"},{"domain":"evil.test","name":"x","value":"secret"}]'), "SID=ok")
