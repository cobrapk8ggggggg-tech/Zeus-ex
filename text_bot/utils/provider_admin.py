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
_PENDING = {"gpt": [], "gemini": []}


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


class RecordPicker(discord.ui.View):
    def __init__(self, console, records, action, page=0):
        super().__init__(timeout=600)
        self.console, self.records, self.action, self.page = console, records, action, page
        chunk = records[page * 25:(page + 1) * 25]
        select = discord.ui.Select(placeholder="اختر العنصر لتطبيقه", options=[
            discord.SelectOption(label=str(r.get("title") or r.get("name") or r.get("slug") or r.get("conversation_id") or "بدون عنوان")[:100],
                                 value=str(page * 25 + i), description=str(r.get("id") or r.get("slug") or r.get("conversation_id") or "")[:100] or None)
            for i, r in enumerate(chunk)])
        select.callback = self.selected
        self.add_item(select)
        for label, target in (("السابق", page - 1), ("التالي", page + 1)):
            if 0 <= target and target * 25 < len(records):
                button = discord.ui.Button(label=label, row=1)
                async def callback(interaction, target=target):
                    if await guard(self.console.api, interaction):
                        await interaction.response.edit_message(view=RecordPicker(console, records, action, target))
                button.callback = callback
                self.add_item(button)
        back = discord.ui.Button(label="العودة لإعدادات المزود", row=2)
        back.callback = self.back
        self.add_item(back)

    async def interaction_check(self, interaction):
        return await guard(self.console.api, interaction)

    async def back(self, interaction):
        if await guard(self.console.api, interaction):
            await interaction.response.defer(ephemeral=True)
            await self.console.refresh(interaction)

    async def selected(self, interaction):
        if not await guard(self.console.api, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = self.records[int(interaction.data["values"][0])]
            if self.action == "models":
                await asyncio.to_thread(save_processing_config, chatgpt_model=record["slug"])
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
            await self.console.refresh(interaction)
        except Exception:
            await interaction.followup.send("تعذر تطبيق الاختيار. تحقق من الجلسة ثم حاول مجددًا.", ephemeral=True)


class TextModal(discord.ui.Modal):
    def __init__(self, console, action):
        titles = {"token": "تحديث جلسة GPT", "message": "رسالة في المحادثة المختارة", "open_chat": "اختيار محادثة GPT برابط", "quality": "جودة استخراج الصور"}
        super().__init__(title=titles[action])
        self.console, self.action = console, action
        if action == "quality":
            self.value = discord.ui.TextInput(label="ارتفاع المقطع بالبكسل (900–2400)", default=str(console.config.ocr_crop_height))
        else:
            self.value = discord.ui.TextInput(label={"token": "Access token لحساب ChatGPT", "message": "نص الرسالة", "open_chat": "رابط المحادثة أو المعرّف"}[action],
                                             style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.value)

    async def interaction_check(self, interaction):
        return await guard(self.console.api, interaction)

    async def on_submit(self, interaction):
        if not await guard(self.console.api, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
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
                    await asyncio.to_thread(new_thread, "gpt", ordinary=True)
            elif self.action == "quality":
                height = int(value)
                if not 900 <= height <= 2400:
                    raise ValueError()
                await asyncio.to_thread(save_processing_config, ocr_crop_height=height)
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
                await interaction.followup.send("رد المحادثة المختارة:", file=discord.File(io.BytesIO(answer.encode()), filename="response.txt"), ephemeral=True)
            await self.console.refresh(interaction)
        except Exception:
            await interaction.followup.send("تعذر تنفيذ الطلب. تحقق من البيانات والجلسة. لم يتم عرض بيانات الاتصال.", ephemeral=True)


class ProviderConsole(discord.ui.LayoutView):
    def __init__(self, api, config, state=None, provider=None):
        super().__init__(timeout=900)
        self.api, self.config = api, config
        self.provider = provider or config.provider
        self.state = state or {}
        self.build()

    async def interaction_check(self, interaction):
        return await guard(self.api, interaction)

    def text(self, container, value):
        container.add_item(discord.ui.TextDisplay(self.api.emojize(value)))

    def buttons(self, container, actions):
        for start in range(0, len(actions), 3):
            row = discord.ui.ActionRow()
            for label, action, emoji in actions[start:start + 3]:
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, emoji=self.api.emoji_manager.partial(emoji))
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
        else:
            has_session = bool(self.config.chatgpt_access_token) if self.provider == "gpt" else bool(self.config.gemini_cookie_header)
            cid = self.state.get("conversation_id")
            model = self.config.chatgpt_model if self.provider == "gpt" else "adaptive"
            self.text(box, f"## {{emoji:shieldcheck}} الجلسة والنموذج\nالجلسة: **{'محفوظة' if has_session else 'غير مضافة بعد'}** · النموذج: `{safe(model)}`\n"
                          f"المشروع: **{safe(self.state.get('project_name') or 'محادثة عادية')}**\n"
                          f"المحادثة: **{safe(self.state.get('title') or ('المحادثة المحفوظة' if cid else 'جديدة عند أول رسالة'))}**\n"
                          f"المعرّف: `{safe(cid)}`\nالمرفقات الجاهزة للرسالة التالية: `{len(_PENDING[self.provider])}`\n-# كل الصور والأجزاء تتابع هذه المحادثة. لا تتغير تلقائيًا ولا تُحذف بعد الاستخراج.")
            if self.provider == "gpt":
                actions = [("معلومات الحساب والتوكن", "account", "infocircle"), ("تحديث التوكن", "token", "lock"),
                           ("نماذج الحساب", "models", "adjustments"), ("مشاريع الحساب", "projects", "folderopen"),
                           ("اختيار محادثة", "chats", "message"), ("فتح برابط", "open_chat", "bookmark")]
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
            self.text(box, f"## {{emoji:photo}} دقة الاستخراج\nارتفاع المقطع: `{self.config.ocr_crop_height}px` · التدقيق المزدوج: **{'مفعّل' if self.config.ocr_double_check else 'معطّل'}**\n"
                          f"النطاق: **{'كل النصوص بما فيها مربعات الحالة والأصوات' if self.config.ocr_scope == 'all' else 'الحوار والسرد ومربعات الحالة بدون الأصوات'}**\n"
                          "حفظ اللغة والحروف الأصلية • ترتيب رأسي • إزالة تكرار التداخل فقط")
            self.buttons(box, [("حجم المقاطع", "quality", "adjustments"), ("تبديل التدقيق المزدوج", "audit", "circlecheck"), ("نطاق النصوص", "scope", "list"), ("نتيجة آخر اختبار", "report", "chartpie")])
        self.buttons(box, [("تفعيل هذا المزود", "activate", "circlecheck"), ("تحديث اللوحة", "refresh", "clock"), ("المستخدمون والنقاط", "users", "user")])
        self.add_item(box)

    async def refresh(self, interaction):
        config = await asyncio.to_thread(get_processing_config)
        state = await asyncio.to_thread(provider_thread, self.provider) if self.provider != "qwen" else {}
        view = ProviderConsole(self.api, config, state, self.provider)
        # This message is always an ephemeral /zx response.
        await interaction.edit_original_response(content=None, embeds=[], attachments=[], view=view)

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
        await interaction.edit_original_response(content=f"اختر من {len(records)} عنصرًا — الصفحات تضم 25 عنصرًا.", embeds=[], view=RecordPicker(self, records, action))

    async def action(self, interaction, action):
        if not await guard(self.api, interaction):
            return
        if action in ("token", "message", "open_chat", "quality"):
            await interaction.response.send_modal(TextModal(self, action))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            config = await asyncio.to_thread(get_processing_config)
            candidate = replace(config, provider=self.provider)
            if action == "activate":
                await asyncio.to_thread(validate_provider, {"_processing_config": candidate})
                await asyncio.to_thread(save_processing_config, provider=self.provider)
            elif action in ("new", "ordinary"):
                await asyncio.to_thread(new_thread, self.provider, ordinary=action == "ordinary")
                _PENDING[self.provider].clear()
            elif action == "clear":
                _PENDING[self.provider].clear()
            elif action in ("audit", "scope"):
                changes = {"ocr_double_check": not config.ocr_double_check} if action == "audit" else {"ocr_scope": "dialogue" if config.ocr_scope == "all" else "all"}
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
                                f"النماذج المتاحة: `{records['available_models']}`\nتم التحقق من الاتصال، ولا يتم عرض التوكن نفسه.")
                    else:
                        body = "جلسة Google متصلة • الحساب `/u/1` • النموذج `adaptive`\nمعرّفات المحادثة محفوظة، والكوكيز لا تُعرض."
                    await interaction.followup.send(embed=self.api.themed_embed("معلومات الحساب الخاصة", body), ephemeral=True)
                else:
                    await self.picker(interaction, records, "gemini_chats" if self.provider == "gemini" else action)
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
        except Exception:
            await interaction.followup.send("تعذر إكمال الإجراء. تحقق من الجلسة والاتصال. لم تُغيّر المحادثة تلقائيًا.", ephemeral=True)

    async def receive_file(self, interaction, action, config):
        key = (self.api.OWNER_ID, self.provider, action)
        if key in _UPLOADS:
            await interaction.followup.send("يوجد انتظار ملف قائم بالفعل؛ أرسل الملف في الخاص أو انتظر انتهاء المهلة.", ephemeral=True)
            return
        _UPLOADS.add(key)
        try:
            dm = await interaction.user.create_dm()
            instructions = {"cookies": "أرسل ملف كوكيز Google (.txt أو .json أو Netscape)، بحد أقصى 2 MB.",
                            "import_thread": "أرسل ملف حالة Gemini JSON وفيه conversation_id وresponse_id وchoice_id وat_token.",
                            "attachment": "أرسل صورة أو ملفًا. مع تعليق: يُرسل السؤال فورًا في المحادثة المحددة. بدون تعليق: يُحفظ المرفق للرسالة التجريبية التالية."}
            await dm.send(instructions[action] + "\nالمهلة: خمس دقائق. أرسل إلغاء لإيقاف الانتظار.")
            await interaction.followup.send("أرسلت لك تعليمات الرفع في الخاص. أنتظر الملف هناك فقط لمدة خمس دقائق.", ephemeral=True)
            message = await self.api.bot.wait_for("message", timeout=300, check=lambda m: m.author.id == self.api.OWNER_ID and m.channel.id == dm.id and (m.attachments or (m.content or "").strip() == "إلغاء"))
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
                await dm.send("تم حفظ ملف الكوكيز. يمكنك حذف رسالة الملف الآن. افحص الاتصال من /zx.")
            elif action == "import_thread":
                state = json.loads(data.decode("utf-8-sig"))
                if not isinstance(state, dict) or not all(isinstance(state.get(k), str) and state[k] for k in ("conversation_id", "response_id", "choice_id")):
                    raise ValueError()
                clean = {k: state.get(k, "") for k in ("conversation_id", "response_id", "choice_id", "at_token")}
                clean["title"] = "محادثة Gemini مستوردة"
                await asyncio.to_thread(choose_thread, "gemini", clean)
            else:
                is_image = (attachment.content_type or "").startswith("image/") or attachment.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
                prompt = (message.content or "").strip()
                if not prompt:
                    if len(_PENDING[self.provider]) >= 5:
                        raise ValueError("خمسة مرفقات كحد أقصى")
                    _PENDING[self.provider].append((data, attachment.filename, is_image))
                    await dm.send("أضيف المرفق. أرسل سؤالك من زر رسالة تجريبية في /zx، أو امسح المرفقات من اللوحة.")
                    return
                answer = await asyncio.to_thread(complete_external, self.provider, prompt,
                    [(data, attachment.filename)] if is_image else (), files=() if is_image else [(data, attachment.filename)], config=config)
                await interaction.followup.send(file=discord.File(io.BytesIO(answer.encode()), filename="attachment-response.txt"), ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("انتهت مهلة رفع الملف. يمكنك بدء الرفع مجددًا من اللوحة.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("تعذر فتح الخاص. اسمح برسائل البوت الخاصة ثم أعد محاولة الرفع؛ لا ترسل الكوكيز في قناة عامة.", ephemeral=True)
        finally:
            _UPLOADS.discard(key)
