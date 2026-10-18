"""Account clients adapted only from the two user-supplied Python files.

Gemini: gemini_complete.py (StreamGenerate + bard-storage).
GPT: chatgpt_android_interactive.py (Android backend + file upload).
Each completion owns its session and starts a new conversation, so parallel
pages and different Discord users never share conversation state.
"""

import base64
import io
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PIL import Image
from utils.storage import load_processing_config


PROVIDER_LABELS = {"qwen": "Qwen", "gemini": "Gemini", "gpt": "GPT"}


class ProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingConfig:
    provider: str = "qwen"
    chatgpt_model: str = "auto"
    gemini_cookie_header: str = field(default="", repr=False)
    chatgpt_access_token: str = field(default="", repr=False)


def get_processing_config():
    saved = load_processing_config()
    return ProcessingConfig(
        provider=saved.get("provider") or os.getenv("AI_PROVIDER", "qwen"),
        chatgpt_model=saved.get("chatgpt_model") or os.getenv("CHATGPT_MODEL", "auto"),
        gemini_cookie_header=saved.get("gemini_cookie_header") or os.getenv("GEMINI_COOKIE_HEADER", ""),
        chatgpt_access_token=saved.get("chatgpt_access_token") or os.getenv("CHATGPT_ACCESS_TOKEN", ""),
    )


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
    return provider


def _check_response(response):
    # Never pass response bodies or request headers (which can contain sessions)
    # into Discord errors or logs.
    if not response.ok:
        raise RuntimeError(f"فشل الاتصال بمحرك المعالجة (HTTP {response.status_code}).")


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

    def upload_image(self, data, name):
        data, name, mime, _, _ = _image_info(data, name)
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

    def complete(self, prompt, images):
        # The attachment uses account /u/1 and the exact same request indices.
        with self.session.get("https://gemini.google.com/u/1/app", timeout=60) as response:
            _check_response(response)
            at = re.search(r'"SNlM0e":"(.*?)"', response.text)
            sid = re.search(r'"FdrFJe":"([\d-]+)"', response.text)
        if not at or not sid:
            raise ProviderConfigurationError("جلسة المعالجة تحتاج تحديثًا لدى الإدارة.")
        attachments = [self.upload_image(data, name) for data, name in images]
        body = [[prompt, 0, None, attachments, None, None, 0], ["ar"],
                ["", "", "", None, None, None, None, None, None, ""],
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
                        value = payload[4][0][1][0]
                    except (ValueError, IndexError, TypeError, KeyError):
                        continue
                    if isinstance(value, str) and value:
                        answer = value  # full snapshot, including revisions
        return answer


class ChatGPTClient:
    BASE = "https://android.chat.openai.com"

    def __init__(self, config=None):
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

    def complete(self, prompt, images):
        parts, attachments = [], []
        for data, name in images:
            pointer, attachment = self.upload_image(data, name)
            parts.append(pointer)
            attachments.append(attachment)
        parts.append(prompt)
        message = {
            "id": str(uuid.uuid4()), "author": {"role": "user"},
            "content": {"content_type": "multimodal_text" if images else "text", "parts": parts},
            "status": "finished_successfully", "recipient": "all",
            "metadata": {"attachments": attachments} if images else {},
        }
        body = {
            "action": "next", "messages": [message], "parent_message_id": str(uuid.uuid4()),
            "model": self.model, "timezone": "Asia/Baghdad", "timezone_offset_min": 180,
            "history_and_training_disabled": False, "force_use_sse": True, "stream": True,
            "websocket_request_id": str(uuid.uuid4()),
        }
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
                if msg.get("author", {}).get("role") == "assistant":
                    value = "".join(part for part in msg.get("content", {}).get("parts", []) if isinstance(part, str))
                    if value:
                        answer = value  # the Android stream sends accumulated text
        return answer


def complete_external(provider, prompt, images=(), *, max_attempts=2, thinking_enabled=False, config=None):
    if provider not in ("gemini", "gpt"):
        raise ProviderConfigurationError("إعداد المعالجة غير صالح. يرجى التواصل مع إدارة البوت.")
    config = config or ProcessingConfig(
        provider=provider, chatgpt_model=os.getenv("CHATGPT_MODEL", "auto"),
        gemini_cookie_header=os.getenv("GEMINI_COOKIE_HEADER", ""),
        chatgpt_access_token=os.getenv("CHATGPT_ACCESS_TOKEN", ""),
    )
    if validate_provider({"_processing_config": config}) != provider:
        raise ProviderConfigurationError("إعداد المعالجة غير متطابق.")
    # Neither attachment establishes an API switch for thinking. Accuracy mode
    # adds a prompt instruction only; never invent provider protocol fields.
    if thinking_enabled:
        prompt += "\n\nCheck every line carefully against the source before returning the final answer only."
    for attempt in range(max(1, max_attempts)):
        client = GeminiClient(config) if provider == "gemini" else ChatGPTClient(config)
        try:
            answer = client.complete(prompt, images).strip()
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
            client.session.close()
