"""Private, provider-specific Discord control console. Only /zx opens it."""
import asyncio
import io
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timezone

import discord

from utils.providers import (PROVIDER_LABELS, ProviderConfigurationError, get_processing_config,
    validate_provider, inspect_account, token_details, parse_cookie_file, provider_thread,
    choose_thread, new_thread, complete_external, list_provider_threads, ChatGPTClient)
from utils.storage import save_processing_config


_UPLOADS = set()
_PENDING = {"gpt": [], "gemini": [], "dark": [], "deepseek": []}


def safe(value, limit=500):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value or "—")))[:limit]


def stamp(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OverflowError):
        return "غير متوفر"


async def guard(api, interaction):
    if interaction.user.id != api.OWNER_ID:
        await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
        return False
    return True


class RecordPicker(discord.ui.LayoutView):
    def __init__(self, console, records, action, page=0):
        super().__init__(timeout=600)
        self.console, self.records, self.action, self.page = console, records, action, page
        box = discord.ui.Container(accent_colour=discord.Colour(0xD4AF37))
        box.add_item(discord.ui.TextDisplay(f"## اختر من {len(records)} عنصرًا\nالصفحة {page + 1} من {max(1, (len(records) + 24) // 25)}"))
        chunk = records[page * 25:(page + 1) * 25]
        select = discord.ui.Select(placeholder="اختر العنصر لتطبيقه", options=[
            discord.SelectOption(label=str(r.get("title") or r.get("name") or r.get("slug") or r.get("conversation_id") or "بدون عنوان")[:100],
                                 value=str(page * 25 + i), description=str(r.get("id") or r.get("slug") or r.get("conversation_id") or "")[:100] or None)
            for i, r in enumerate(chunk)])
        select.callback = self.selected
        select_row = discord.ui.ActionRow()
        select_row.add_item(select)
        box.add_item(select_row)
        nav_row = discord.ui.ActionRow()
        for label, target in (("السابق", page - 1), ("التالي", page + 1)):
            if 0 <= target and target * 25 < len(records):
                button = discord.ui.Button(label=label)
                async def callback(interaction, target=target):
                    if await guard(self.console.api, interaction):
                        await interaction.response.edit_message(view=RecordPicker(console, records, action, target))
                button.callback = callback
                nav_row.add_item(button)
        if nav_row.children:
            box.add_item(nav_row)
        back = discord.ui.Button(label="العودة لإعدادات المزود")
        back.callback = self.back
        back_row = discord.ui.ActionRow()
        back_row.add_item(back)
        box.add_item(back_row)
        self.add_item(box)

    async def interaction_check(self, interaction):
        return await guard(self.console.api, interaction)

    async def back(self, interaction):
        if await guard(self.console.api, interaction):
            await interaction.response.defer(ephemeral=True)
            await self.console.refresh(interaction)

    async def selected(self, interaction):
        if not await guard(self.console.api, interaction):
            return
        await interaction.response.defer()
        try:
            record = self.records[int(interaction.data["values"][0])]
            if self.action == "models":
                key = "deepseek_model" if self.console.provider == "deepseek" else "chatgpt_model"
                await asyncio.to_thread(save_processing_config, **{key: record["slug"]})
            elif self.action == "projects":
                await asyncio.to_thread(choose_thread, "gpt", {"gizmo_id": record["id"], "project_name": record["name"], "title": "محادثة جديدة"})
            elif self.action == "chats":
                state = await asyncio.to_thread(provider_thread, "gpt")
                client = ChatGPTClient(self.console.config)
                try:
                    detail = await asyncio.to_thread(client.conversation, record["id"])
                finally:
                    client.session.close()
                await asyncio.to_thread(choose_thread, "gpt", {
                    **{k: state[k] for k in ("gizmo_id", "project_name") if k in state},
                    "conversation_id": record["id"], "title": record.get("title", "بدون عنوان"),
                    "parent_message_id": detail.get("current_node", ""),
                })
            elif self.action == "gemini_chats":
                await asyncio.to_thread(choose_thread, "gemini", record)
            elif self.action == "deepseek_chats":
                await asyncio.to_thread(choose_thread, "deepseek", record)
            await self.console.refresh(interaction)
            await interaction.followup.send("تم تطبيق الاختيار وتحديث اللوحة.", ephemeral=True)
        except Exception:
            await interaction.followup.send("تعذر تطبيق الاختيار. تحقق من الجلسة ثم حاول مجددًا.", ephemeral=True)


class TextModal(discord.ui.Modal):
    def __init__(self, console, action):
        titles = {"token": "تحديث جلسة GPT", "deepseek_token": "تحديث جلسة DeepSeek",
                  "system_prompt": "تعليمات GPT-OCR", "urls": "صور أو PDF عبر روابط",
                  "message": "إرسال طلب", "open_chat": "اختيار محادثة GPT برابط", "model": "تحديد النموذج"}
        super().__init__(title=titles[action])
        self.console, self.action = console, action
        self.value = discord.ui.TextInput(label={"token": "Access token لحساب ChatGPT", "deepseek_token": "توكن DeepSeek",
            "system_prompt": "تعليمات النظام", "urls": "طلب الاستخراج أو السؤال", "message": "نص الرسالة",
            "open_chat": "رابط المحادثة أو المعرّف", "model": "معرّف النموذج (default لافتراضي DeepSeek)"}[action],
            default=console.config.dark_system_prompt if action == "system_prompt" else None,
            style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.value)
        if action == "urls":
            self.urls = discord.ui.TextInput(label="رابط HTTP/HTTPS في كل سطر — عشرة كحد أقصى",
                                            style=discord.TextStyle.paragraph, max_length=4000)
            self.add_item(self.urls)

    async def interaction_check(self, interaction):
        return await guard(self.console.api, interaction)

    async def on_submit(self, interaction):
        if not await guard(self.console.api, interaction):
            return
        await interaction.response.defer()
        try:
            config = await asyncio.to_thread(get_processing_config)
            value = self.value.value.strip()
            if self.action == "token":
                candidate = replace(config, provider="gpt", chatgpt_access_token=value)
                details = token_details(candidate)
                try:
                    old = token_details(config)
                except ProviderConfigurationError:
                    old = None
                await asyncio.to_thread(save_processing_config, chatgpt_access_token=value)
                if old and old["account_id"] != details["account_id"]:
                    await asyncio.to_thread(choose_thread, "gpt", {"title": "محادثة جديدة", "account_id": details["account_id"]})
                    _PENDING["gpt"].clear()
            elif self.action == "deepseek_token":
                candidate = replace(config, provider="deepseek", deepseek_token=value)
                await asyncio.to_thread(inspect_account, "deepseek", "account", candidate)
                await asyncio.to_thread(save_processing_config, deepseek_token=value)
                if value != config.deepseek_token:
                    await asyncio.to_thread(choose_thread, "deepseek", {"title": "محادثة جديدة"})
                    _PENDING["deepseek"].clear()
            elif self.action == "system_prompt":
                await asyncio.to_thread(save_processing_config, dark_system_prompt=value)
            elif self.action == "urls":
                urls = [line.strip() for line in self.urls.value.splitlines() if line.strip()]
                if not 1 <= len(urls) <= 10:
                    raise ValueError()
                answer = await asyncio.to_thread(complete_external, "dark", value, file_urls=urls,
                                                config=replace(config, provider="dark"), max_attempts=1)
                await interaction.followup.send(file=discord.File(io.BytesIO(answer.encode()), filename="url-response.txt"), ephemeral=True)
            elif self.action == "model":
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                    raise ValueError()
                key = "deepseek_model" if self.console.provider == "deepseek" else "chatgpt_model"
                await asyncio.to_thread(save_processing_config, **{key: "" if key == "deepseek_model" and value == "default" else value})
            elif self.action == "open_chat":
                match = re.search(r"(?:/c/)?([a-zA-Z0-9-]{16,})/?(?:\?.*)?$", value)
                if not match:
                    raise ValueError()
                cid = match.group(1)
                client = ChatGPTClient(config)
                try:
                    detail = await asyncio.to_thread(client.conversation, cid)
                finally:
                    client.session.close()
                state = await asyncio.to_thread(provider_thread, "gpt")
                await asyncio.to_thread(choose_thread, "gpt", {**{k: state[k] for k in ("gizmo_id", "project_name") if k in state},
                    "conversation_id": cid, "title": detail.get("title", "محادثة مختارة"), "parent_message_id": detail.get("current_node", "")})
            else:
                candidate = replace(config, provider=self.console.provider)
                pending = list(_PENDING[self.console.provider])
                answer = await asyncio.to_thread(complete_external, self.console.provider, value,
                    [(p[0], p[1]) for p in pending if p[2]], files=[(p[0], p[1]) for p in pending if not p[2]], config=candidate)
                _PENDING[self.console.provider].clear()
                await interaction.followup.send("نتيجة الطلب:", file=discord.File(io.BytesIO(answer.encode()), filename="response.txt"), ephemeral=True)
            await self.console.refresh(interaction)
            if self.action not in ("message", "urls"):
                await interaction.followup.send("تم حفظ الإعداد وتحديث اللوحة.", ephemeral=True)
        except Exception:
            await interaction.followup.send("تعذر تنفيذ الطلب. تحقق من البيانات والجلسة. لم يتم عرض بيانات الاتصال.", ephemeral=True)


class ProviderConsole(discord.ui.LayoutView):
    def __init__(self, api, config, state=None, provider=None, message=None):
        super().__init__(timeout=900)
        self.api, self.config = api, config
        self.provider = provider or config.provider
        self.state = state or {}
        # Keep the original followup WebhookMessage, including for modal submits.
        # An ephemeral channel Message cannot be edited through the bot API.
        self.message = message
        self.build()

    async def interaction_check(self, interaction):
        return await guard(self.api, interaction)

    def text(self, container, value):
        container.add_item(discord.ui.TextDisplay(self.api.emojize(value)))

    def buttons(self, container, actions):
        for start in range(0, len(actions), 3):
            row = discord.ui.ActionRow()
            for label, action, emoji in actions[start:start + 3]:
                style = discord.ButtonStyle.success if action == "activate" else discord.ButtonStyle.secondary
                button = discord.ui.Button(label=label, style=style, emoji=self.api.emoji_manager.partial(emoji))
                async def callback(interaction, action=action):
                    await self.action(interaction, action)
                button.callback = callback
                row.add_item(button)
            container.add_item(row)

    def build(self):
        self.clear_items()
        box = discord.ui.Container(accent_colour=discord.Colour(0xD4AF37))
        label = PROVIDER_LABELS[self.provider]
        active = "نشط لجميع المستخدمين" if self.provider == self.config.provider else "إعدادات هذا المزود — غير مفعّل بعد"
        self.text(box, f"# {{emoji:layoutdashboard}} مركز ZEUS الخاص\n### {label} · {active}\n-# المالك فقط • كل الردود خاصة • لا تظهر هذه الإعدادات للمستخدمين")
        row = discord.ui.ActionRow()
        menu = discord.ui.Select(placeholder="اختر المزود لعرض إعداداته فقط", options=[
            discord.SelectOption(label=name, value=key, default=key == self.provider)
            for key, name in PROVIDER_LABELS.items()])
        menu.callback = self.provider_selected
        row.add_item(menu)
        box.add_item(row)
        box.add_item(discord.ui.Separator())
        if self.provider == "qwen":
            self.text(box, "## {emoji:user} حسابات Qwen\nالنموذج: `qwen3.8-max`\nحسابات المعالجة والتبديل والحدود تبقى وفق المسار الأصلي.")
            actions = [("إدارة الحسابات", "qwen_accounts", "user"), ("الحالة والاستخدام", "qwen_status", "chartpie")]
        elif self.provider == "dark":
            self.text(box, "## {emoji:photo} GPT-OCR\nصور وPDF بروابط، حتى 10 مرفقات في الطلب.\n"
                          f"المرفقات الجاهزة: `{len(_PENDING['dark'])}`\n"
                          "القطع الثلاث تُرسل معًا بالترتيب. الخدمة تعمل بطلبات مستقلة ولا تعيد معرّف محادثة.\n"
                          "روابط المرفقات التي ينشئها البوت مؤقتة وتُحذف عند انتهاء الطلب.")
            actions = [("فحص الاتصال", "account", "shieldcheck"), ("تعليمات النظام", "system_prompt", "adjustments"),
                       ("روابط صور أو PDF", "urls", "folderopen"), ("طلب نصي", "message", "message"),
                       ("إرفاق صورة أو ملف", "attachment", "photo"), ("مسح المرفقات", "clear", "trash")]
        else:
            has_session = bool(self.config.chatgpt_access_token) if self.provider == "gpt" else bool(self.config.deepseek_token) if self.provider == "deepseek" else bool(self.config.gemini_cookie_header)
            cid = self.state.get("conversation_id")
            model = self.config.chatgpt_model if self.provider == "gpt" else (self.config.deepseek_model or "افتراضي الخدمة") if self.provider == "deepseek" else "adaptive"
            title = self.state.get('title')
            if cid and title == "محادثة جديدة":
                title = "المحادثة الحالية"
            project_line = f"المشروع: **{safe(self.state.get('project_name') or 'محادثة عادية')}**\n" if self.provider == "gpt" else ""
            self.text(box, f"## {{emoji:shieldcheck}} الجلسة والنموذج\nالجلسة: **{'محفوظة' if has_session else 'غير مضافة بعد'}** · النموذج: `{safe(model)}`\n"
                          f"{project_line}"
                          f"المحادثة: **{safe(title or ('المحادثة المحفوظة' if cid else 'جديدة عند أول رسالة'))}**\n"
                          f"المعرّف: `{safe(cid)}`\nالمرفقات الجاهزة للرسالة التالية: `{len(_PENDING[self.provider])}`\n-# الصور تتابع المحادثة نفسها. عند فشل صورة: محادثة جديدة في المشروع نفسه، ثم مزود احتياطي إذا لزم.")
            if self.provider == "gpt":
                actions = [("معلومات الحساب والتوكن", "account", "infocircle"), ("تحديث التوكن", "token", "lock"),
                           ("نماذج الحساب", "models", "adjustments"), ("تحديد نموذج يدويًا", "model", "adjustments"), ("مشاريع الحساب", "projects", "folderopen"),
                           ("اختيار محادثة", "chats", "message"), ("فتح برابط", "open_chat", "bookmark")]
            elif self.provider == "deepseek":
                self.text(box, "-# يستمر session_id وparent_message_id بين الطلبات. قائمة المحادثات تعرض ما حفظه البوت. النماذج من قائمة الخدمة المعلنة.")
                actions = [("فحص جلسة DeepSeek", "account", "shieldcheck"), ("تحديث توكن DeepSeek", "deepseek_token", "lock"),
                           ("نماذج الخدمة", "models", "adjustments"), ("تحديد نموذج يدويًا", "model", "adjustments"),
                           ("المحادثات المحفوظة", "chats", "message"),
                           (f"تفكير التجربة: {'مفعّل' if self.config.deepseek_thinking else 'معطّل'}", "thinking", "adjustments")]
            else:
                self.text(box, "-# النموذج adaptive وحساب Google رقم 1 هما المثبتان في الملف المرفق. قائمة المحادثات هنا تعرض ما حُفظ بهذا البوت أو استوردته بملف حالة.")
                actions = [("رفع ملف الكوكيز", "cookies", "folderopen"), ("فحص الاتصال", "account", "shieldcheck"),
                           ("المحادثات المحفوظة", "chats", "message"), ("استيراد حالة محادثة", "import_thread", "folder")]
            actions += [("محادثة جديدة", "new", "message"), ("رسالة تجريبية", "message", "message"),
                        ("إرفاق صورة أو ملف", "attachment", "photo"), ("مسح المرفقات", "clear", "trash")]
            if self.provider == "gpt":
                actions.append(("خارج المشاريع", "ordinary", "folder"))
        self.buttons(box, actions)
        box.add_item(discord.ui.Separator())
        if self.provider != "qwen":
            self.text(box, "## {emoji:photo} سياسة الصور\nحتى 16000 بكسل: الصورة كاملة كما هي.\n"
                          "فوق 16000 بكسل: ثلاثة أجزاء فقط، تُرسل معًا في طلب واحد مرتب.\n"
                          "طلب استخراج واحد عند النجاح؛ إعادة المحاولة تكون عند الفشل فقط.\n"
                          f"النطاق الافتراضي للتجربة: **{'كل النصوص بما فيها الأصوات' if self.config.ocr_scope == 'all' else 'الحوار والسرد ومربعات الحالة بدون الأصوات'}**\n"
                          "استخراج المستخدم يطبّق خيار المؤثرات والترجمة واللغة من /setting.\n"
                          "حفظ اللغة والحروف الأصلية • ترتيب رأسي • إزالة تكرار التداخل فقط")
            self.buttons(box, [("نطاق النصوص", "scope", "list"), ("نتيجة آخر اختبار", "report", "chartpie")])
        self.buttons(box, [("تفعيل هذا المزود", "activate", "circlecheck"), ("تحديث اللوحة", "refresh", "clock"), ("المستخدمون والنقاط", "users", "user")])
        self.add_item(box)

    async def refresh(self, interaction):
        config = await asyncio.to_thread(get_processing_config)
        state = await asyncio.to_thread(provider_thread, self.provider) if self.provider != "qwen" else {}
        view = ProviderConsole(self.api, config, state, self.provider, self.message)
        await self.edit_panel(interaction, view)

    async def edit_panel(self, interaction, view):
        kwargs = dict(content=None, embeds=[], attachments=[], view=view)
        if getattr(interaction, "message", None) is not None:
            # Every component/modal was deferred as a message UPDATE. Use its
            # fresh webhook token so an old /zx token cannot break later clicks.
            message = await interaction.edit_original_response(**kwargs)
            self.message = message
            if isinstance(view, ProviderConsole):
                view.message = message
            elif isinstance(view, RecordPicker):
                view.console.message = message
        elif self.message is not None:
            await self.message.edit(**kwargs)
        else:
            # Compatibility with an already-open panel from an older deployment.
            await interaction.edit_original_response(**kwargs)

    async def provider_selected(self, interaction):
        if not await guard(self.api, interaction):
            return
        self.provider = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await self.refresh(interaction)

    async def picker(self, interaction, records, action):
        if not records:
            await interaction.followup.send("لا توجد عناصر في هذا القسم للحساب الحالي.", ephemeral=True)
            return
        await self.edit_panel(interaction, RecordPicker(self, records, action))

    async def action(self, interaction, action):
        if not await guard(self.api, interaction):
            return
        if action in ("token", "deepseek_token", "system_prompt", "urls", "message", "open_chat", "model"):
            await interaction.response.send_modal(TextModal(self, action))
            return
        await interaction.response.defer()
        try:
            config = await asyncio.to_thread(get_processing_config)
            candidate = replace(config, provider=self.provider)
            if action == "activate":
                await asyncio.to_thread(validate_provider, {"_processing_config": candidate})
                await asyncio.to_thread(save_processing_config, provider=self.provider)
            elif action in ("new", "ordinary"):
                from utils.providers import PROVIDER_LOCKS
                def create():
                    with PROVIDER_LOCKS[self.provider]:
                        new_thread(self.provider, ordinary=action == "ordinary", config=candidate)
                        complete_external(self.provider,
                            "This is a new OCR workspace. Future images must be transcribed verbatim in original language and reading order. Reply READY only.",
                            config=candidate, max_attempts=1)
                await asyncio.to_thread(create)
                _PENDING[self.provider].clear()
            elif action == "clear":
                _PENDING[self.provider].clear()
            elif action in ("thinking", "scope"):
                changes = {"deepseek_thinking": not config.deepseek_thinking} if action == "thinking" else {"ocr_scope": "dialogue" if config.ocr_scope == "all" else "all"}
                await asyncio.to_thread(save_processing_config, **changes)
            elif action in ("cookies", "attachment", "import_thread"):
                await self.receive_file(interaction, action, candidate)
            elif action == "users":
                await interaction.followup.send(embed=self.api.admin_panel_embed(), view=self.api.AdminPanelView(), ephemeral=True)
            elif action == "report":
                from utils.live_validation import latest_report
                report = await asyncio.to_thread(latest_report)
                if report:
                    await interaction.followup.send(file=discord.File(io.BytesIO(json.dumps(report, ensure_ascii=False, indent=2).encode()), filename="validation-report.json"), ephemeral=True)
                else:
                    await interaction.followup.send("لا يوجد اختبار مسجل بعد.", ephemeral=True)
            elif action in ("account", "models", "projects", "chats"):
                state = await asyncio.to_thread(provider_thread, self.provider)
                if action == "chats" and self.provider == "gpt" and state.get("gizmo_id"):
                    projects = await asyncio.to_thread(inspect_account, "gpt", "projects", candidate)
                    records = next((p["chats"] for p in projects if p["id"] == state["gizmo_id"]), [])
                else:
                    records = await asyncio.to_thread(inspect_account, self.provider, action, candidate)
                if action == "account":
                    if self.provider == "gpt":
                        body = (f"الحساب: `{safe(records['account_id'])}`\nالبريد: {safe(records.get('email'))}\n"
                                f"الخطة: **{safe(records.get('plan'))}**\nالمستخدم: `{safe(records.get('user_id'))}`\n"
                                f"إصدار التوكن: {stamp(records.get('issued_at'))}\nانتهاؤه: {stamp(records.get('expires_at'))}\n"
                                f"النماذج المتاحة: `{records['available_models']}`\n"
                                f"قائمة النماذج: {'متصلة' if records.get('model_list_available') else 'تعذر جلبها؛ يمكن تحديد نموذج يدويًا'}\nلا يتم عرض التوكن نفسه.")
                    elif self.provider == "deepseek":
                        body = "جلسة DeepSeek متصلة.\nالنماذج من قائمة الخدمة المعلنة.\nالتوكن محفوظ ولا يُعرض."
                    elif self.provider == "dark":
                        body = "الخدمة استجابت للطلب التجريبي." if records.get("connected") else "الخدمة أعادت ردًا غير متوقع."
                    else:
                        body = "جلسة Google متصلة • الحساب `/u/1` • النموذج `adaptive`\nمعرّفات المحادثة محفوظة، والكوكيز لا تُعرض."
                    await interaction.followup.send(embed=self.api.themed_embed("معلومات الحساب الخاصة", body), ephemeral=True)
                else:
                    picker_action = self.provider + "_chats" if action == "chats" and self.provider in ("gemini", "deepseek") else action
                    await self.picker(interaction, records, picker_action)
                    return
            elif action == "qwen_accounts":
                data = await asyncio.to_thread(self.api.load_accounts_data, self.api.OWNER_ID)
                if not data["accounts"]:
                    await asyncio.to_thread(self.api.create_and_save_new_account, self.api.OWNER_ID)
                await interaction.followup.send(embed=self.api.themed_embed("حسابات Qwen", "الحسابات وحدودها واستخداماتها"), view=self.api.AccountsView(self.api.OWNER_ID), ephemeral=True)
            elif action == "qwen_status":
                data = await asyncio.to_thread(self.api.load_accounts_data, self.api.OWNER_ID)
                accounts = data["accounts"]
                await interaction.followup.send(embed=self.api.themed_embed("حالة Qwen", f"الحسابات: {len(accounts)}\nالنشطة: {sum(a.get('ocr_limit_until', 0) <= time.time() for a in accounts)}\nالاستخدامات: {sum(a.get('ocr_count', 0) for a in accounts)}"), ephemeral=True)
            await self.refresh(interaction)
            receipts = {"activate": f"تم تفعيل {PROVIDER_LABELS[self.provider]} وتحديث اللوحة.",
                        "new": "تم إنشاء محادثة جديدة وتحديث معرّفها في اللوحة، مع الاحتفاظ بالمشروع إن كان مختارًا.",
                        "ordinary": "تم إنشاء محادثة جديدة خارج المشاريع وتحديث اللوحة.",
                        "clear": "تم مسح المرفقات المعلّقة.",
                        "thinking": "تم تحديث تفكير الرسالة التجريبية. /extract يطبّق الوضع الذي يختاره المستخدم.",
                        "scope": "تم تحديث نطاق التجربة. استخراج المستخدم يطبّق إعداد المؤثرات من /setting."}
            if action in receipts:
                await interaction.followup.send(receipts[action], ephemeral=True)
        except Exception:
            await self.refresh(interaction)
            await interaction.followup.send("تعذر إكمال الإجراء. تحقق من الجلسة والاتصال ثم حاول مجددًا.", ephemeral=True)

    async def receive_file(self, interaction, action, config):
        provider = config.provider
        # Accept the attachment in the same channel where /zx was opened.
        key = self.api.OWNER_ID
        if key in _UPLOADS:
            await interaction.followup.send("يوجد انتظار ملف قائم بالفعل في دردشة أخرى أو في هذه الدردشة؛ أكمله أو أرسل إلغاء.", ephemeral=True)
            return
        _UPLOADS.add(key)
        try:
            channel_id = interaction.channel.id
            instructions = {"cookies": "أرسل ملف كوكيز Google (.txt أو .json أو Netscape)، بحد أقصى 2 MB.",
                            "import_thread": "أرسل ملف حالة Gemini JSON وفيه conversation_id وresponse_id وchoice_id وat_token.",
                            "attachment": "أرسل صورة أو ملفًا. مع تعليق: يُرسل السؤال فورًا في المحادثة المحددة. بدون تعليق: يُحفظ المرفق للرسالة التجريبية التالية."}
            await interaction.followup.send(instructions[action] + "\nأرسل الملف هنا في هذه الدردشة. المهلة: خمس دقائق. أرسل إلغاء لإيقاف الانتظار.", ephemeral=True)
            message = await self.api.bot.wait_for("message", timeout=300, check=lambda m: m.author.id == self.api.OWNER_ID and m.channel.id == channel_id and (m.attachments or (m.content or "").strip() == "إلغاء"))
            if not message.attachments:
                await interaction.followup.send("تم إلغاء انتظار الملف.", ephemeral=True)
                return
            attachment = message.attachments[0]
            limit = 2 * 1024 * 1024 if action in ("cookies", "import_thread") else 20 * 1024 * 1024
            if attachment.size > limit:
                raise ValueError("حجم الملف")
            data = await attachment.read()
            if action == "cookies":
                header = parse_cookie_file(data)
                await asyncio.to_thread(save_processing_config, gemini_cookie_header=header)
                await interaction.followup.send("تم حفظ كوكيز Gemini من الملف وتحديث الجلسة. افحص الاتصال من اللوحة.", ephemeral=True)
            elif action == "import_thread":
                state = json.loads(data.decode("utf-8-sig"))
                if not isinstance(state, dict) or not all(isinstance(state.get(k), str) and state[k] for k in ("conversation_id", "response_id", "choice_id")):
                    raise ValueError()
                clean = {k: state.get(k, "") for k in ("conversation_id", "response_id", "choice_id", "at_token")}
                clean["title"] = "محادثة Gemini مستوردة"
                await asyncio.to_thread(choose_thread, "gemini", clean)
                await interaction.followup.send("تم استيراد المحادثة وتحديث اللوحة.", ephemeral=True)
            else:
                is_image = (attachment.content_type or "").startswith("image/") or attachment.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
                prompt = (message.content or "").strip()
                if not prompt:
                    if len(_PENDING[provider]) >= (10 if provider == "dark" else 5):
                        raise ValueError("عدد المرفقات تجاوز الحد المسموح")
                    _PENDING[provider].append((data, attachment.filename, is_image))
                    button_label = "طلب نصي" if provider == "dark" else "رسالة تجريبية"
                    await interaction.followup.send(f"أضيف المرفق. أرسل سؤالك من زر {button_label} في /zx، أو امسح المرفقات من اللوحة.", ephemeral=True)
                    return
                answer = await asyncio.to_thread(complete_external, provider, prompt,
                    [(data, attachment.filename)] if is_image else (), files=() if is_image else [(data, attachment.filename)], config=config)
                await interaction.followup.send(file=discord.File(io.BytesIO(answer.encode()), filename="attachment-response.txt"), ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("انتهت مهلة رفع الملف. يمكنك بدء الرفع مجددًا من اللوحة.", ephemeral=True)
        finally:
            _UPLOADS.discard(key)
