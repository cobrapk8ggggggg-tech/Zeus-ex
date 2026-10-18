import json
import re
import base64
import hashlib
import time
from pathlib import Path
from typing import Any

import requests
import discord

BASE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BASE_DIR.parent
SYSTEM_EMOJIS_JSON = REPO_DIR / "utils" / "emojis.json"
EMOJIS_JSON = BASE_DIR / "utils" / "emojis.json"
ASSETS_DIR = BASE_DIR / "assets" / "emojis"
PLACEHOLDER_RE = re.compile(r"\{emoji:([A-Za-z0-9_]+)\}")
CUSTOM_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{17,22}>$")
LEGACY_COLON_RE = re.compile(r"^:[A-Za-z0-9_]{2,32}:$")
THEMES = {
    "blue": {"prefix": "b", "color": 0x5865F2},
    "red": {"prefix": "r", "color": 0xED4245},
    "green": {"prefix": "g", "color": 0x57F287},
    "purple": {"prefix": "p", "color": 0x9B59B6},
    "gold": {"prefix": "y", "color": 0xF1C40F},
    "pink": {"prefix": "pk", "color": 0xFF73FA},
}
FALLBACKS = {
    "circlecheck": "✅", "circlex": "❌", "alerttriangle": "⚠️", "infocircle": "ℹ️",
    "clock": "⏳", "settings": "⚙️", "photo": "🖼️", "user": "👤", "chartpie": "📊",
    "mail": "✉️", "trash": "🗑️", "lock": "🔒", "shield": "🛡️", "list": "📋",
    "confetti": "🎉", "message": "💬", "star": "⭐", "crown": "👑", "ticket": "🎫",
    "adjustments": "🛠️", "folderopen": "📂", "folder": "📁", "gift": "🎁", "music_play": "▶️",
}

class EmojiManager:
    def __init__(self):
        self.application_id = None
        self.map = {"__color__": "gold"}
        self.load()

    @property
    def theme(self):
        return self.map.get("__color__", "gold")

    def _load_json(self, path):
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[EmojiManager] failed to load {path}: {exc}")
            return {}

    def load_system_emojis(self):
        return {k: v for k, v in self._load_json(SYSTEM_EMOJIS_JSON).items() if not k.startswith("__")}

    def _fallback_map(self):
        # Do not send copied root/custom emoji IDs before verifying that they belong
        # to this bot application; Discord renders unavailable custom emojis as
        # literal :name: text such as :b_settings:.
        return {"__color__": "gold"}

    def load(self, application_id=None):
        if application_id is not None:
            self.application_id = str(application_id)
        local = self._load_json(EMOJIS_JSON)
        local_app_id = str(local.get("__application_id__", "")) if local else ""
        if self.application_id and local_app_id == self.application_id:
            self.map = local
        else:
            self.map = self._fallback_map()
        return self.map

    def save(self):
        ordered = {"__color__": self.theme}
        if self.application_id:
            ordered["__application_id__"] = self.application_id
        # حفظ المفاتيح الوصفية الإضافية (مثل __asset_hash__) لتفادي إعادة الرفع في كل تشغيل
        for key in sorted(k for k in self.map if k.startswith("__") and k not in {"__color__", "__application_id__"}):
            ordered[key] = self.map.get(key, "")
        for key in sorted(k for k in self.map if not k.startswith("__")):
            ordered[key] = self.map.get(key, "")
        EMOJIS_JSON.parent.mkdir(parents=True, exist_ok=True)
        EMOJIS_JSON.write_text(json.dumps(ordered, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        self.map = ordered

    def _format_custom_emoji(self, value):
        if isinstance(value, dict):
            emoji_id = str(value.get("id", "")).strip()
            emoji_name = str(value.get("name", "")).strip()
            animated = bool(value.get("animated"))
            if emoji_id and emoji_name:
                return f"<{'a' if animated else ''}:{emoji_name}:{emoji_id}>"
        if isinstance(value, str):
            value = value.strip()
            if CUSTOM_RE.match(value):
                return value
            if LEGACY_COLON_RE.match(value):
                return ""
            return value
        return ""


    def _fallback_name(self, name):
        for prefix in ("b_", "r_", "g_", "p_", "y_", "pk_"):
            if name.startswith(prefix):
                return name[len(prefix):]
        return name

    def get(self, name):
        lookup_name = self._fallback_name(name)
        value = self._format_custom_emoji(self.map.get(lookup_name) or self.map.get(name))
        return value or FALLBACKS.get(lookup_name, "")

    def partial(self, name):
        value = self.get(name)
        if isinstance(value, str) and CUSTOM_RE.match(value):
            return discord.PartialEmoji.from_str(value)
        return value or None

    def placeholder(self, name):
        return self.get(name) or ""

    def replace(self, value: Any):
        if isinstance(value, str):
            def refresh_custom(match):
                emoji_name = match.group(2)
                fallback_name = self._fallback_name(emoji_name)
                return self.map.get(fallback_name) or self.map.get(emoji_name) or FALLBACKS.get(fallback_name) or match.group(0)
            value = re.sub(r"<(a)?:([A-Za-z0-9_]{2,32}):(\d{17,22})>", refresh_custom, value)
            return PLACEHOLDER_RE.sub(lambda m: self.get(m.group(1)) or "", value)
        if isinstance(value, list):
            return [self.replace(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.replace(v) for v in value)
        if isinstance(value, dict):
            return {k: self.replace(v) for k, v in value.items()}
        return value

    def asset_dir(self):
        return ASSETS_DIR

    def validate_value(self, value):
        return bool(isinstance(value, str) and (CUSTOM_RE.match(value.strip()) or value.strip()))

    def asset_source_dir(self):
        """مجلد أصول الإيموجي — النسخة الذهبية أولًا لأنها هوية البوت."""
        gold_dir = ASSETS_DIR / "gold"
        if gold_dir.exists():
            try:
                if any(p.suffix.lower() in {".png", ".gif"} for p in gold_dir.iterdir()):
                    return gold_dir
            except OSError:
                pass
        return ASSETS_DIR

    @staticmethod
    def _hash_assets(directory):
        hasher = hashlib.sha256()
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in {".png", ".gif"}:
                continue
            hasher.update(path.stem.encode("utf-8"))
            hasher.update(path.read_bytes())
        return hasher.hexdigest()

    def sync_application_emojis(self, bot_user_id: int, token: str):
        """
        يرفع/يحدّث إيموجيات التطبيق من أصول النسخة الذهبية (assets/emojis/gold).
        - إن كان الإيموجي موجودًا بصورة قديمة (مثل النسخة البنفسجية السابقة) يتم تحديث صورته.
        - تخزين __asset_hash__ يجعل التشغيلات التالية تخطي المزامنة بالكامل بدون أي اتصال بالـ API.
        """
        if not token:
            raise RuntimeError("DISCORD_TOKEN is required to sync application emojis.")
        source_dir = self.asset_source_dir()
        if not source_dir.exists():
            raise RuntimeError(f"Emoji assets directory not found: {source_dir}")
        asset_hash = self._hash_assets(source_dir)
        self.application_id = str(bot_user_id)

        # مسار سريع — لا اتصال بالـ API إذا كانت النسخة المحلية مطابقة تمامًا لهذه الأصول
        local = self._load_json(EMOJIS_JSON)
        if (
            str(local.get("__application_id__", "")) == self.application_id
            and local.get("__asset_hash__") == asset_hash
            and any(not k.startswith("__") for k in local)
        ):
            self.map = local
            return {"ok": True, "skipped": True, "uploaded": 0, "updated": 0,
                    "mapped": len([k for k in local if not k.startswith("__")]),
                    "theme": "gold", "source": str(source_dir)}

        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        api = f"https://discord.com/api/v10/applications/{bot_user_id}/emojis"
        current = requests.get(api, headers=headers, timeout=30)
        current.raise_for_status()
        items = current.json()
        existing = {item["name"]: item for item in (items if isinstance(items, list) else items.get("items", []))}
        theme = "gold"
        prefix = THEMES[theme]["prefix"]
        fresh = {"__color__": theme, "__application_id__": self.application_id, "__asset_hash__": asset_hash}
        uploaded = 0
        updated = 0
        for path in sorted(source_dir.iterdir()):
            if path.suffix.lower() not in {".png", ".gif"}:
                continue
            base_name = path.stem
            discord_name = f"{prefix}_{base_name}"
            mime = "image/gif" if path.suffix.lower() == ".gif" else "image/png"
            image_uri = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
            item = existing.get(discord_name)
            if item is not None:
                # حدّث صورة الإيموجي الموجود إلى النسخة الذهبية (مثل تحويل البنفسجي القديم)
                patched = None
                for attempt in range(3):
                    try:
                        patched = requests.patch(f"{api}/{item['id']}", headers=headers, json={"image": image_uri}, timeout=30)
                        if patched.status_code == 429:
                            retry_after = float(patched.json().get("retry_after", 2.0) or 2.0)
                            time.sleep(min(retry_after, 10.0))
                            patched = None
                            continue
                        break
                    except requests.RequestException:
                        time.sleep(1.5 * (attempt + 1))
                if patched is not None and patched.ok:
                    item = patched.json()
                    existing[discord_name] = item
                    updated += 1
                else:
                    # بديل احتياطي — حذف وإعادة إنشاء بنفس الاسم
                    try:
                        requests.delete(f"{api}/{item['id']}", headers=headers, timeout=30)
                    except requests.RequestException:
                        pass
                    item = None
            if item is None:
                payload = {"name": discord_name, "image": image_uri}
                created = None
                last_error = None
                for attempt in range(3):
                    try:
                        created = requests.post(api, headers=headers, json=payload, timeout=30)
                        if created.status_code == 429:
                            retry_after = float(created.json().get("retry_after", 2.0) or 2.0)
                            time.sleep(min(retry_after, 10.0))
                            created = None
                            continue
                        created.raise_for_status()
                        break
                    except requests.RequestException as exc:
                        last_error = exc
                        created = None
                        time.sleep(1.5 * (attempt + 1))
                if created is None:
                    raise last_error or RuntimeError(f"failed to upload emoji {discord_name}")
                item = created.json()
                existing[discord_name] = item
                uploaded += 1
            animated = path.suffix.lower() == ".gif" or item.get("animated")
            fresh[base_name] = f"<{'a' if animated else ''}:{item['name']}:{item['id']}>"
        self.map = fresh
        self.save()
        return {"ok": True, "uploaded": uploaded, "updated": updated,
                "mapped": len([k for k in fresh if not k.startswith("__")]),
                "theme": theme, "source": str(source_dir)}

emoji_manager = EmojiManager()

def emojize(value: Any):
    return emoji_manager.replace(value)

def markdown_block(title: str, body: str) -> str:
    return emojize(f"**{title}**\n\n---\n\n{body}")

def themed_embed(title=None, description=None, color_name=None):
    selected = "red" if color_name == "red" else "gold"
    color = THEMES[selected]["color"]
    embed = discord.Embed(color=color, title=emojize(title) if title else None, description=emojize(description) if description else None)
    embed.timestamp = discord.utils.utcnow()
    return embed
