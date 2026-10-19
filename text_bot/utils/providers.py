"""Account clients plus the two additional HTTP services supplied by the owner.

Gemini: gemini_complete.py (StreamGenerate + bard-storage).
GPT: chatgpt_android_interactive.py (Android backend + file upload).
HTTP sessions are request-local; the owner-selected server conversations are
durable in MongoDB. Requests to each conversation are serialized.
Dark GPT-OCR is stateless; DeepSeek uses the supplied worker's session API.
"""

import base64
import io
import json
import os
import random
import re
import time
import uuid
import threading
import mimetypes
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PIL import Image
from utils.storage import (load_processing_config, load_provider_thread, save_provider_thread,
                           select_provider_thread, list_provider_threads)


PROVIDER_LABELS = {"qwen": "Qwen", "gemini": "Gemini", "gpt": "GPT", "dark": "GPT-OCR", "deepseek": "DeepSeek"}
PROVIDER_LOCKS = {name: threading.RLock() for name in ("gemini", "gpt", "dark", "deepseek")}


class ProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingConfig:
    provider: str = "qwen"
    chatgpt_model: str = "auto"
    gemini_cookie_header: str = field(default="", repr=False)
    chatgpt_access_token: str = field(default="", repr=False)
    ocr_scope: str = "all"
    dark_system_prompt: str = "Follow the current task precisely. Preserve original text when performing OCR."
    deepseek_token: str = field(default="", repr=False)
    deepseek_model: str = ""
    deepseek_thinking: bool = False


def get_processing_config():
    saved = load_processing_config()
    return ProcessingConfig(
        provider=saved.get("provider") or os.getenv("AI_PROVIDER", "qwen"),
        chatgpt_model=saved.get("chatgpt_model") or os.getenv("CHATGPT_MODEL", "auto"),
        gemini_cookie_header=saved.get("gemini_cookie_header") or os.getenv("GEMINI_COOKIE_HEADER", ""),
        chatgpt_access_token=saved.get("chatgpt_access_token") or os.getenv("CHATGPT_ACCESS_TOKEN", ""),
        ocr_scope=saved.get("ocr_scope", "all"),
        dark_system_prompt=saved.get("dark_system_prompt", ProcessingConfig.dark_system_prompt),
        deepseek_token=saved.get("deepseek_token") or os.getenv("DEEPSEEK_TOKEN", ""),
        deepseek_model=saved.get("deepseek_model", os.getenv("DEEPSEEK_MODEL", "")),
        deepseek_thinking=bool(saved.get("deepseek_thinking", False)),
    )


def import_owner_cookie_file():
    """One-time import of the cookie export supplied by the owner for this repair."""
    import hashlib
    from utils.storage import save_processing_config
    marker = os.getenv("ZEUS_IMPORT_GEMINI_COOKIES", "")
    header = os.getenv("GEMINI_COOKIE_HEADER", "")
    if not marker or not header:
        return False
    saved = load_processing_config()
    if saved.get("gemini_cookie_import") == marker:
        return False
    clean = parse_cookie_file(header.encode())
    if hashlib.sha256(clean.encode()).hexdigest() != marker:
        raise ProviderConfigurationError("ملف استيراد الكوكيز غير متطابق.")
    save_processing_config(gemini_cookie_header=clean, gemini_cookie_import=marker)
    return True


def processing_config(settings=None):
    config = (settings or {}).get("_processing_config")
    return config if isinstance(config, ProcessingConfig) else get_processing_config()


def snapshot_processing_settings(settings):
    # Always replace any stale per-user choice with the owner's configuration.
    result = {k: v for k, v in (settings or {}).items() if k not in ("provider", "chatgpt_model", "_processing_config")}
    result["_processing_config"] = get_processing_config()
    return result


def selected_provider(settings=None):
    value = processing_config(settings).provider
    value = str(value).strip().lower()
    if value not in PROVIDER_LABELS:
        raise ProviderConfigurationError("إعداد المعالجة غير صالح. يرجى التواصل مع إدارة البوت.")
    return value


def supports_thinking(provider):
    # Only these supplied protocols expose a real thinking switch.
    return provider in ("qwen", "deepseek")


def _gemini_cookies(config=None):
    value = (config.gemini_cookie_header if config else os.getenv("GEMINI_COOKIE_HEADER", "")).strip()
    if not value and os.getenv("GEMINI_COOKIE_FILE"):
        try:
            value = Path(os.environ["GEMINI_COOKIE_FILE"]).read_text().strip()
        except OSError:
            raise ProviderConfigurationError("تعذر قراءة ملف جلسة المعالجة.") from None
    if not value or "=" not in value or "\n" in value or "\r" in value:
        raise ProviderConfigurationError("جلسة المعالجة غير معدة. يرجى التواصل مع إدارة البوت.")
    return value


def parse_cookie_file(data):
    """Accept Cookie headers, exported cookie JSON, and Netscape cookie files."""
    if len(data) > 2 * 1024 * 1024:
        raise ProviderConfigurationError("ملف الكوكيز أكبر من الحد المسموح (2 MB).")
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeError:
        raise ProviderConfigurationError("ارفع ملف كوكيز نصيًا بترميز UTF-8.") from None
    pairs = []
    if text.startswith(("[", "{")):
        try:
            content = json.loads(text)
            if isinstance(content, dict):
                content = content.get("cookies", [{"name": k, "value": v} for k, v in content.items()])
            for cookie in content:
                domain = str(cookie.get("domain", "google.com")).lstrip(".")
                if domain == "google.com" or domain.endswith(".google.com"):
                    pairs.append((cookie["name"], cookie["value"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderConfigurationError("بنية ملف كوكيز JSON غير صالحة.") from None
    elif "\t" in text:
        for line in text.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and (parts[0].lstrip(".") == "google.com" or parts[0].endswith(".google.com")):
                pairs.append((parts[5], parts[6]))
    else:
        if re.search(r"(?m)^\s*(?:export\s+)?(?:BROWSER_COOKIE_HEADER|GEMINI_COOKIE_HEADER)=", text):
            try:
                # Parse shell quoting as data. Never execute an exported file.
                tokens = shlex.split(text, comments=True)
                text = next(item.split("=", 1)[1] for item in tokens if item.startswith(("BROWSER_COOKIE_HEADER=", "GEMINI_COOKIE_HEADER=")))
            except (ValueError, StopIteration):
                raise ProviderConfigurationError("ملف ترويسة الكوكيز غير صالح.") from None
        text = re.sub(r"^cookie\s*:\s*", "", text, flags=re.I)
        for part in text.replace("\r", "").replace("\n", ";").split(";"):
            if "=" in part:
                pairs.append(tuple(part.strip().split("=", 1)))
    cleaned = {}
    for key, value in pairs:
        key, value = str(key).strip(), str(value).strip()
        if not key or re.search(r"[\s;=\r\n]", key) or re.search(r"[;\r\n]", value):
            raise ProviderConfigurationError("يحتوي ملف الكوكيز قيما غير صالحة.")
        cleaned[key] = value
    if not cleaned:
        raise ProviderConfigurationError("لم يعثر على كوكيز Google صالحة في الملف.")
    return "; ".join(k + "=" + v for k, v in cleaned.items())


def token_details(config):
    token, account = _chatgpt_credentials(config)
    encoded = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    auth = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})
    return {"account_id": account, "email": claims.get("email") or profile.get("email"),
            "plan": auth.get("chatgpt_plan_type"), "user_id": auth.get("chatgpt_user_id") or claims.get("sub"),
            "issued_at": claims.get("iat"), "expires_at": claims.get("exp")}


def _chatgpt_credentials(config=None):
    token = (config.chatgpt_access_token if config else os.getenv("CHATGPT_ACCESS_TOKEN", "")).strip()
    if not token:
        raise ProviderConfigurationError("جلسة المعالجة غير معدة. يرجى التواصل مع إدارة البوت.")
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        if not isinstance(account, str) or not account:
            raise ValueError()
        if "exp" in claims and float(claims["exp"]) <= time.time():
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderConfigurationError("جلسة المعالجة غير صالحة أو منتهية. يرجى تحديثها لدى الإدارة.") from None
    return token, account


def validate_provider(settings=None):
    config = processing_config(settings)
    provider = selected_provider(settings)
    if provider == "gemini":
        _gemini_cookies(config)
    elif provider == "gpt":
        _chatgpt_credentials(config)
    elif provider == "deepseek" and not config.deepseek_token.strip():
        raise ProviderConfigurationError("جلسة المعالجة غير معدة. يرجى التواصل مع إدارة البوت.")
    return provider


def provider_thread(provider):
    return load_provider_thread(provider).get("state") or {}


def choose_thread(provider, state):
    with PROVIDER_LOCKS[provider]:
        select_provider_thread(provider, state)


def new_thread(provider, *, ordinary=False, config=None):
    with PROVIDER_LOCKS[provider]:
        old = provider_thread(provider)
        state = {k: old[k] for k in ("gizmo_id", "project_name") if k in old} if provider == "gpt" and not ordinary else {}
        state["title"] = "محادثة جديدة"
        if provider == "gpt":
            state["account_id"] = _chatgpt_credentials(config or get_processing_config())[1]
        select_provider_thread(provider, state)


def inspect_account(provider, action, config):
    if provider == "dark":
        if action != "account":
            raise ProviderConfigurationError("الخدمة لا توفر قوائم حساب أو محادثات.")
        client = DarkOCRClient(config)
        try:
            return {"connected": client.complete("Reply with ZEUS_OK only.", (), ()) == "ZEUS_OK"}
        finally:
            client.session.close()
    client = ChatGPTClient(config) if provider == "gpt" else DeepSeekClient(config) if provider == "deepseek" else GeminiClient(config)
    try:
        if provider == "gpt":
            if action == "account":
                info = token_details(config)
                try:
                    info["available_models"] = len(client.models())
                    info["model_list_available"] = True
                except RuntimeError as exc:
                    info["available_models"] = 0
                    info["model_list_available"] = False
                    info["model_list_error"] = str(exc)
                return info
            return getattr(client, action)()
        if provider == "deepseek":
            if action == "account":
                return {"connected": True, "conversations": len(client.chats())}
            if action == "chats":
                return list_provider_threads("deepseek")
            return getattr(client, action)()
        if action == "account":
            return client.connection_status()
        if action == "chats":
            return list_provider_threads(provider)
        raise ProviderConfigurationError("هذه الوظيفة غير متاحة في بروتوكول الجلسة المرفق.")
    finally:
        client.session.close()


def _check_response(response):
    # Never pass response bodies or request headers (which can contain sessions)
    # into Discord errors or logs.
    if not response.ok:
        # Expose validation field names, never request inputs/header values.
        validation = []
        if response.status_code == 422:
            try:
                detail = response.json().get("detail")
                if isinstance(detail, list):
                    for item in detail[:5]:
                        if isinstance(item, dict):
                            validation.append({"loc": item.get("loc"), "type": item.get("type")})
            except (ValueError, AttributeError, TypeError):
                pass
        suffix = " " + json.dumps(validation) if validation else ""
        error = ProviderConfigurationError if response.status_code == 422 else RuntimeError
        raise error(f"فشل الاتصال بمحرك المعالجة (HTTP {response.status_code})." + suffix)


def _image_info(data, name):
    with Image.open(io.BytesIO(data)) as img:
        mime = Image.MIME.get(img.format, "image/png")
        width, height = img.size
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime)
    if suffix is None:
        with Image.open(io.BytesIO(data)) as img:
            output = io.BytesIO()
            img.convert("RGB").save(output, format="PNG")
            data = output.getvalue()
        mime, suffix = "image/png", ".png"
    filename = Path(name or "image").stem + suffix
    return data, filename, mime, width, height


class GeminiClient:
    MODEL_HEADER = '[1,null,null,null,"fbb127bbb056c959",null,null,0,[4,5,6,8,4,5,6,8],null,null,1,null,null,1,1,"036033AF-386B-4A1C-A8B6-F563586CF2B9"]'
    BUILD = "boq_assistant-bard-web-server_20260817.02_p0"
    UPLOAD_AUTH = "Basic c2F2ZXM6cyNMdGhlNmxzd2F2b0RsN3J1d1U="

    def __init__(self, config=None):
        self.state = {}
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "*/*", "accept-language": "en-US,en;q=0.9",
            "origin": "https://gemini.google.com", "referer": "https://gemini.google.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "x-same-domain": "1",
        })
        for item in _gemini_cookies(config).split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                self.session.cookies.set(key, value, domain=".google.com")

    def upload_file(self, data, name, mime=None):
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = {
            "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Tenant-Id": "bard-storage", "Push-ID": "feeds/mcudyrk2a4khkz",
            "X-Client-Pctx": "CgcSBWjK7pYx", "Authorization": self.UPLOAD_AUTH,
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        }
        with self.session.post("https://push.clients6.google.com/upload/", headers=headers,
                               data=f"File name: {name}", timeout=60) as response:
            _check_response(response)
            url = response.headers.get("X-Goog-Upload-Url") or response.headers.get("X-Goog-Upload-Control-Url")
        if not url:
            raise RuntimeError("لم يصل رابط رفع الصورة من محرك المعالجة.")
        headers.update({"X-Goog-Upload-Command": "upload, finalize",
                        "X-Goog-Upload-Offset": "0", "Content-Type": mime})
        with self.session.post(url, headers=headers, data=data, timeout=300) as response:
            _check_response(response)
            uri = response.text.strip()
        if not uri:
            raise RuntimeError("لم يصل معرّف الصورة من محرك المعالجة.")
        return [[uri, 1], name]

    def upload_image(self, data, name):
        data, name, mime, _, _ = _image_info(data, name)
        return self.upload_file(data, name, mime)

    def connection_status(self):
        with self.session.get("https://gemini.google.com/u/1/app", timeout=60) as response:
            _check_response(response)
            if not re.search(r'"SNlM0e":"(.*?)"', response.text):
                raise ProviderConfigurationError("جلسة المعالجة تحتاج تحديثًا لدى الإدارة.")
        return {"connected": True, "account_index": 1, "model": "adaptive"}

    def complete(self, prompt, images, files=()):
        # The attachment uses account /u/1 and the exact same request indices.
        with self.session.get("https://gemini.google.com/u/1/app", timeout=60) as response:
            _check_response(response)
            at = re.search(r'"SNlM0e":"(.*?)"', response.text)
            sid = re.search(r'"FdrFJe":"([\d-]+)"', response.text)
        if not at or not sid:
            raise ProviderConfigurationError("جلسة المعالجة تحتاج تحديثًا لدى الإدارة.")
        attachments = [self.upload_image(data, name) for data, name in images]
        attachments.extend(self.upload_file(data, name) for data, name in files)
        state = self.state
        context = [state.get("conversation_id", ""), state.get("response_id", ""), state.get("choice_id", ""),
                   None, None, None, None, None, None, state.get("at_token", "")]
        body = [[prompt, 0, None, attachments, None, None, 0], ["ar"],
                context,
                None, None, None, [], 0, [], [], 1, 0]
        answer = ""
        with self.session.post(
            "https://gemini.google.com/u/1/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            params={"bl": self.BUILD, "hl": "en", "_reqid": random.randint(10000, 99999), "rt": "c", "f.sid": sid.group(1)},
            data={"at": at.group(1), "f.req": json.dumps([None, json.dumps(body)])},
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "x-goog-ext-525001261-jspb": self.MODEL_HEADER},
            stream=True, timeout=300,
        ) as response:
            _check_response(response)
            for raw in response.iter_lines():
                if not raw:
                    continue
                try:
                    rows = json.loads(raw)
                except (ValueError, TypeError):
                    continue  # XSSI prefix / framing length lines
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    try:
                        payload = json.loads(row[2])
                        try:
                            if self.state.get("conversation_id") and payload[1][0] != self.state["conversation_id"]:
                                raise ProviderConfigurationError("الخادم لم يتابع المحادثة المختارة؛ اختر محادثة صالحة من الإدارة.")
                            self.state.update({"conversation_id": payload[1][0], "response_id": payload[1][1],
                                               "choice_id": payload[4][0][0],
                                               "at_token": payload[3].get("26", "") if isinstance(payload[3], dict) else ""})
                        except (IndexError, TypeError, KeyError):
                            pass
                        value = payload[4][0][1][0]
                    except (ValueError, IndexError, TypeError, KeyError):
                        continue
                    if isinstance(value, str) and value:
                        answer = value  # full snapshot, including revisions
        return answer


class ChatGPTClient:
    BASE = "https://android.chat.openai.com"

    def __init__(self, config=None):
        self.state = {}
        token, account = _chatgpt_credentials(config)
        self.model = config.chatgpt_model if config else os.getenv("CHATGPT_MODEL", "auto")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Bearer " + token, "ChatGPT-Account-ID": account,
            "User-Agent": "ChatGPT/1.2026.195 (Android 16; 24117RN76G; build 2619512)",
            "oai-package-name": "com.openai.chatgpt", "oai-client-type": "android",
            "Accept": "text/event-stream,application/json", "Content-Type": "application/json",
            "oai-device-id": str(uuid.uuid4()), "accept-language": "en-US,en;q=0.9",
            "chatgpt-residency-region": "no_constraint", "x-device-tier": "mid",
        })

    def get(self, path):
        for attempt in range(5):
            try:
                with self.session.get(self.BASE + path, timeout=60) as response:
                    if response.status_code >= 500 and attempt < 4:
                        time.sleep(2)
                        continue
                    _check_response(response)
                    return response.json()
            except requests.RequestException:
                if attempt == 4:
                    raise RuntimeError("تعذر الاتصال بالحساب.") from None
                time.sleep(2)

    def models(self):
        return self.get("/backend-api/models").get("models", [])

    def chats(self, offset=0):
        return self.get(f"/backend-api/conversations?offset={offset}&limit=50&order=updated&is_archived=false").get("items", [])

    def projects(self):
        data = self.get("/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5&limit=50")
        result = []
        for item in data.get("items", []):
            project = item.get("gizmo", {}).get("gizmo", {})
            result.append({"id": project.get("id"), "name": project.get("display", {}).get("name", "بدون اسم"),
                           "chats": item.get("conversations", {}).get("items", [])})
        return result

    def conversation(self, cid):
        from urllib.parse import quote
        return self.get("/backend-api/conversation/" + quote(cid, safe=""))

    def upload_file(self, data, name):
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with self.session.post(self.BASE + "/backend-api/files", json={
            "file_name": name, "file_size": len(data), "mime_type": mime, "use_case": "multimodal",
        }, timeout=60) as response:
            _check_response(response)
            info = response.json()
        with requests.put(info["upload_url"], data=data, headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime}, timeout=180) as response:
            _check_response(response)
        with self.session.post(self.BASE + "/backend-api/files/" + info["file_id"] + "/uploaded", json={}, timeout=60) as response:
            _check_response(response)
        return {"id": info["file_id"], "name": name, "mime_type": mime, "size": len(data)}

    def upload_image(self, data, name):
        data, name, mime, width, height = _image_info(data, name)
        with self.session.post(self.BASE + "/backend-api/files", json={
            "file_name": name, "file_size": len(data), "mime_type": mime, "use_case": "multimodal",
        }, timeout=60) as response:
            _check_response(response)
            info = response.json()
        # A separate PUT carries only blob headers, never the account bearer.
        with requests.put(info["upload_url"], data=data,
                          headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime}, timeout=180) as response:
            _check_response(response)
        with self.session.post(self.BASE + "/backend-api/files/" + info["file_id"] + "/uploaded",
                               json={}, timeout=60) as response:
            _check_response(response)
        return ({"content_type": "image_asset_pointer", "asset_pointer": "file-service://" + info["file_id"],
                 "size_bytes": len(data), "width": width, "height": height},
                {"id": info["file_id"], "name": name, "mime_type": mime, "size": len(data)})

    def complete(self, prompt, images, files=()):
        parts, attachments = [], []
        for data, name in images:
            pointer, attachment = self.upload_image(data, name)
            parts.append(pointer)
            attachments.append(attachment)
        parts.append(prompt)
        attachments.extend(self.upload_file(data, name) for data, name in files)
        message = {
            "id": str(uuid.uuid4()), "author": {"role": "user"},
            "content": {"content_type": "multimodal_text" if images else "text", "parts": parts},
            "status": "finished_successfully", "recipient": "all",
            "metadata": {"attachments": attachments} if attachments else {},
        }
        cid, gid = self.state.get("conversation_id"), self.state.get("gizmo_id")
        parent = str(uuid.uuid4())
        if cid:
            conversation = self.conversation(cid)
            if conversation.get("title"):
                self.state["title"] = conversation["title"]
            parent = conversation.get("current_node")
            if not parent:
                raise RuntimeError("المحادثة المحددة لا تحتوي عقدة متابعة صالحة؛ اخترها مجددًا من الإدارة.")
        if gid:
            message["metadata"]["gizmo_id"] = gid
        body = {
            "action": "next", "messages": [message], "parent_message_id": parent,
            "model": self.model, "timezone": "Asia/Baghdad", "timezone_offset_min": 180,
            "history_and_training_disabled": False, "force_use_sse": True, "stream": True,
            "websocket_request_id": str(uuid.uuid4()),
        }
        if cid:
            body["conversation_id"] = cid
        if gid:
            body.update({"conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": gid}, "conversation_origin": "tpp"})
        answer = ""
        with self.session.post(self.BASE + "/backend-api/conversation", json=body, stream=True, timeout=300) as response:
            _check_response(response)
            for raw in response.iter_lines():
                if not raw or not raw.startswith(b"data: "):
                    continue
                raw = raw[6:]
                if raw == b"[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    raise RuntimeError("تعذر إكمال الطلب لدى محرك المعالجة.")
                msg = event.get("message") or {}
                if event.get("conversation_id"):
                    if cid and cid != event["conversation_id"]:
                        raise ProviderConfigurationError("الخادم لم يتابع المحادثة المختارة؛ اختر محادثة صالحة من الإدارة.")
                    self.state["conversation_id"] = event["conversation_id"]
                if msg.get("id"):
                    self.state["parent_message_id"] = msg["id"]
                if msg.get("author", {}).get("role") == "assistant":
                    value = "".join(part for part in msg.get("content", {}).get("parts", []) if isinstance(part, str))
                    if value:
                        answer = value  # the Android stream sends accumulated text
        return answer


class DarkOCRClient:
    BASE = "https://dark.ps/v2/gpt-ocr/completions"

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

    def _request(self, prompt, urls):
        with self.session.post(self.BASE, json={"prompt": prompt,
                "system_prompt": self.config.dark_system_prompt, "file_urls": urls},
                stream=True, timeout=(15, 300), allow_redirects=False) as response:
            _check_response(response)
            if 300 <= response.status_code < 400:
                raise RuntimeError("وجهة المعالجة أعادت توجيهًا غير متوقع.")
            pieces, pending, done = [], [], False
            started = time.monotonic()
            def consume(raw):
                nonlocal done
                if raw == b"[DONE]":
                    done = True
                    return
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    raise RuntimeError("بث المعالجة غير صالح.") from None
                if not isinstance(event, dict) or event.get("error"):
                    raise RuntimeError("تعذر إكمال طلب المعالجة.")
                if isinstance(event.get("text"), str):
                    pieces.append(event["text"])
                done = event.get("done") is True
            for line in response.iter_lines():
                if time.monotonic() - started > 300:
                    raise RuntimeError("انتهت مهلة المعالجة.")
                if line.startswith(b"data:"):
                    pending.append(line[5:].lstrip())
                elif not line and pending:
                    consume(b"\n".join(pending))
                    pending = []
                    if done:
                        break
            if pending and not done:
                consume(b"\n".join(pending))
            if not done:
                raise RuntimeError("انقطع بث المعالجة قبل اكتماله.")
            answer = "".join(pieces).strip()
            if not answer:
                raise RuntimeError("استجابة فارغة من محرك المعالجة.")
            if re.fullmatch(r"Something went wrong\.?\s*Please try again\.?", answer, re.I):
                raise RuntimeError("تعذر إكمال طلب GPT-OCR لدى الخدمة؛ ستُستخدم إعادة المحاولة.")
            return answer

    @staticmethod
    def pdf_pages(data, name, remaining):
        # The endpoint currently returns an upstream error for scanned PDFs.
        # Render their original pages locally, then submit ordinary image URLs.
        import pypdfium2 as pdfium
        pages = []
        with pdfium.PdfDocument(data) as document:
            if not 0 < len(document) <= remaining:
                raise ProviderConfigurationError("PDF المصوّر يحتاج أكثر من عشرة مرفقات صور. قسّمه إلى ملفات أصغر.")
            for index in range(len(document)):
                page = document[index]
                try:
                    bitmap = page.render(scale=2)
                    try:
                        image = bitmap.to_pil()
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                pages.append((output.getvalue(), f"{Path(name).stem}.page-{index + 1:04}.png"))
        return pages

    def complete(self, prompt, images, files=(), file_urls=()):
        from urllib.parse import urlsplit
        urls = list(file_urls)
        if len(images) + len(files) + len(urls) > 10:
            raise ProviderConfigurationError("الخدمة تقبل عشرة مرفقات كحد أقصى في الطلب.")
        for url in urls:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                raise ProviderConfigurationError("رابط المرفق غير صالح.")
        attachments = []
        for data, name in images:
            data, name, _, _, _ = _image_info(data, name)
            attachments.append((data, name))
        for data, name in files:
            if data.lstrip().startswith(b"%PDF-"):
                attachments.extend(self.pdf_pages(data, name, 10 - len(attachments) - len(urls)))
            else:
                attachments.append((data, name))
        if not attachments:
            return self._request(prompt, urls)
        from utils.file_links import temporary_links
        with temporary_links(attachments) as generated:
            return self._request(prompt, generated + urls)


class DeepSeekClient:
    BASE = "https://deepseek-pow.contact-3mh.workers.dev"

    def __init__(self, config):
        self.config, self.state = config, {}
        self.thinking_enabled = config.deepseek_thinking
        self.session = requests.Session()

    def post(self, path, body):
        with self.session.post(self.BASE + path, json={"token": self.config.deepseek_token, **body},
                timeout=(15, 300), allow_redirects=False) as response:
            _check_response(response)
            if 300 <= response.status_code < 400:
                raise RuntimeError("وجهة المعالجة أعادت توجيهًا غير متوقع.")
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("error") or payload.get("success") is False:
            raise RuntimeError("تعذر تنفيذ الطلب لدى خدمة المعالجة.")
        return payload

    def chats(self):
        return self.post("/api/chat/sessions/list", {}).get("sessions", [])

    def models(self):
        with self.session.get(self.BASE + "/v1/models",
                headers={"x-deepseek-token": self.config.deepseek_token},
                timeout=(15, 60), allow_redirects=False) as response:
            _check_response(response)
            payload = response.json()
        return [{"slug": item["id"], "title": item["id"]} for item in payload.get("data", []) if isinstance(item.get("id"), str)]

    def complete(self, prompt, images, files=()):
        uploads = []
        for data, name in images:
            data, name, mime, _, _ = _image_info(data, name)
            uploads.append({"filename": name, "file_data": "data:" + mime + ";base64," + base64.b64encode(data).decode()})
        for data, name in files:
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            uploads.append({"filename": name, "file_data": "data:" + mime + ";base64," + base64.b64encode(data).decode()})
        body = {"prompt": prompt, "thinking_enabled": bool(self.thinking_enabled), "search_enabled": False}
        if uploads:
            body["files"] = uploads
        if self.config.deepseek_model:
            body["model"] = self.config.deepseek_model
        cid = self.state.get("conversation_id")
        if cid:
            body["session_id"] = cid
        if self.state.get("parent_message_id") is not None:
            body["parent_message_id"] = self.state["parent_message_id"]
        result = self.post("/api/chat", body)
        if cid and result.get("session_id") != cid:
            raise ProviderConfigurationError("الخادم لم يتابع المحادثة المختارة؛ اختر محادثة صالحة من الإدارة.")
        if result.get("session_id"):
            self.state["conversation_id"] = result["session_id"]
        if result.get("parent_message_id") is not None:
            self.state["parent_message_id"] = result["parent_message_id"]
        return result.get("response") or ""

def complete_external(provider, prompt, images=(), *, max_attempts=2, thinking_enabled=None, config=None, files=(), file_urls=()):
    if provider not in PROVIDER_LOCKS:
        raise ProviderConfigurationError("إعداد المعالجة غير صالح. يرجى التواصل مع إدارة البوت.")
    config = config or ProcessingConfig(
        provider=provider, chatgpt_model=os.getenv("CHATGPT_MODEL", "auto"),
        gemini_cookie_header=os.getenv("GEMINI_COOKIE_HEADER", ""),
        chatgpt_access_token=os.getenv("CHATGPT_ACCESS_TOKEN", ""),
        deepseek_token=os.getenv("DEEPSEEK_TOKEN", ""),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", ""),
    )
    if validate_provider({"_processing_config": config}) != provider:
        raise ProviderConfigurationError("إعداد المعالجة غير متطابق.")
    # Neither attachment establishes an API switch for thinking. Accuracy mode
    # adds a prompt instruction only; never invent provider protocol fields.
    if thinking_enabled:
        prompt += "\n\nCheck every line carefully against the source before returning the final answer only."
    with PROVIDER_LOCKS[provider]:
        if provider == "dark":
            client = DarkOCRClient(config)
            try:
                return client.complete(prompt, images, files, file_urls)
            finally:
                client.session.close()
        if file_urls:
            raise ProviderConfigurationError("الروابط المباشرة متاحة لخدمة GPT-OCR فقط.")
        return _complete_persistent(provider, prompt, images, files, config, max_attempts, thinking_enabled)


def _complete_persistent(provider, prompt, images, files, config, max_attempts, thinking_enabled=None):
    document = load_provider_thread(provider)
    state, revision = document.get("state") or {}, document.get("revision", 0)
    if provider == "gpt":
        account = _chatgpt_credentials(config)[1]
        if state.get("account_id") and state["account_id"] != account:
            raise ProviderConfigurationError("المحادثة محفوظة لحساب مختلف. اختر محادثة لهذا الحساب من الإدارة.")
        state["account_id"] = account
    if provider == "deepseek":
        import hashlib
        fingerprint = hashlib.sha256(config.deepseek_token.encode()).hexdigest()
        if state.get("credential_fingerprint") and state["credential_fingerprint"] != fingerprint:
            raise ProviderConfigurationError("المحادثة محفوظة لجلسة مختلفة؛ اختر محادثة جديدة من الإدارة.")
        state["credential_fingerprint"] = fingerprint
    for attempt in range(max(1, max_attempts)):
        client = GeminiClient(config) if provider == "gemini" else DeepSeekClient(config) if provider == "deepseek" else ChatGPTClient(config)
        if provider == "deepseek":
            client.thinking_enabled = config.deepseek_thinking if thinking_enabled is None else bool(thinking_enabled)
        client.state = dict(state)
        try:
            answer = client.complete(prompt, images, files).strip()
            if not client.state.get("conversation_id"):
                raise ProviderConfigurationError("لم يصل معرّف المحادثة. راجع المحادثات من الإدارة قبل إعادة الطلب.")
            if not answer:
                raise RuntimeError("استجابة فارغة من محرك المعالجة — سيُعاد المحاولة.")
            return answer
        except ProviderConfigurationError:
            raise
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError):
            if attempt + 1 >= max(1, max_attempts):
                raise RuntimeError("تعذر إكمال المعالجة. تحقق من الجلسة أو أعد المحاولة لاحقًا.") from None
            time.sleep(2 * (attempt + 1))
        finally:
            # Preserve an allocated server ID even on a later stream failure.
            if client.state.get("conversation_id"):
                state = dict(client.state)
                save_provider_thread(provider, state, revision)
            client.session.close()
