import base64
import io
import json
import os
import unittest
from copy import deepcopy
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from PIL import Image

from utils import providers


def account_token(**extra):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "test-account"}, **extra}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "test." + encoded + ".signature"


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (24, 48)).save(output, format=fmt)
    return output.getvalue()


def response(*, text="", lines=(), data=None, headers=None, status=200):
    obj = MagicMock()
    obj.__enter__.return_value = obj
    obj.ok = status < 400
    obj.status_code = status
    obj.text = text
    obj.headers = headers or {}
    obj.json.return_value = data
    obj.iter_lines.return_value = iter(lines)
    return obj


def gemini_snapshot(text):
    payload = [None, ["conversation", "response"], None, {}, [["choice", [text]]]]
    return ["wrb.fr", None, json.dumps(payload)]


def gpt_event(text, role="assistant"):
    return b"data: " + json.dumps({"conversation_id": "test-conversation", "message": {
        "author": {"role": role}, "content": {"parts": [text]},
    }}).encode()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AI_PROVIDER": "qwen", "GEMINI_COOKIE_HEADER": "session=test; another=value=two",
            "CHATGPT_ACCESS_TOKEN": account_token(), "CHATGPT_MODEL": "auto",
        })
        self.env.start()
        self.states = {}
        def load(provider):
            return {"revision": 0, "state": deepcopy(self.states.get(provider, {}))}
        def save(provider, state, revision):
            self.states[provider] = deepcopy(state)
            return True
        for name, side_effect in (("load_provider_thread", load), ("save_provider_thread", save)):
            handler = patch("utils.providers." + name, side_effect=side_effect)
            handler.start()
            self.addCleanup(handler.stop)
        config_patch = patch("utils.providers.load_processing_config", return_value={})
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(self.env.stop)

    def test_default_and_saved_choice(self):
        self.assertEqual(providers.selected_provider({}), "qwen")
        with patch.dict(os.environ, {"AI_PROVIDER": "gemini"}):
            self.assertEqual(providers.selected_provider({"provider": ""}), "gemini")
            self.assertEqual(providers.selected_provider({"provider": "gpt"}), "gemini")
            self.assertEqual(providers.selected_provider({"_processing_config": providers.ProcessingConfig(provider="gpt")}), "gpt")

    @patch("utils.providers.requests.Session")
    def test_owner_model_and_session_override_environment_without_leaking(self, factory):
        token = account_token()
        config = providers.ProcessingConfig(provider="gpt", chatgpt_model="owner-model", chatgpt_access_token=token, gemini_cookie_header="secret-cookie=value")
        self.assertNotIn(token, repr(config))
        self.assertNotIn("secret-cookie", repr(config))
        factory.return_value.post.return_value = response(lines=[gpt_event("answer"), b"data: [DONE]"])
        with patch.dict(os.environ, {"CHATGPT_ACCESS_TOKEN": "invalid-env-token", "CHATGPT_MODEL": "other-model"}):
            self.assertEqual(providers.complete_external("gpt", "prompt", config=config), "answer")
        self.assertEqual(factory.return_value.post.call_args.kwargs["json"]["model"], "owner-model")

    def test_reject_unknown_or_missing_sessions_without_http(self):
        with patch("utils.providers.requests.Session") as session:
            with self.assertRaises(providers.ProviderConfigurationError):
                providers.validate_provider({"_processing_config": providers.ProcessingConfig(provider="unknown")})
            with patch.dict(os.environ, {"GEMINI_COOKIE_HEADER": "", "GEMINI_COOKIE_FILE": ""}):
                with self.assertRaises(providers.ProviderConfigurationError):
                    providers.complete_external("gemini", "prompt")
            with patch.dict(os.environ, {"CHATGPT_ACCESS_TOKEN": ""}):
                with self.assertRaises(providers.ProviderConfigurationError):
                    providers.complete_external("gpt", "prompt")
            session.assert_not_called()

    def test_invalid_and_expired_tokens_do_not_leak(self):
        for token in ("secret-invalid-token", account_token(exp=1)):
            with patch.dict(os.environ, {"CHATGPT_ACCESS_TOKEN": token}):
                with self.assertRaises(providers.ProviderConfigurationError) as raised:
                    providers.validate_provider({"_processing_config": providers.ProcessingConfig(provider="gpt", chatgpt_access_token=token)})
                self.assertNotIn(token, str(raised.exception))

    @patch("utils.providers.requests.Session")
    def test_gemini_two_images_keep_order_and_use_source_protocol(self, factory):
        session = factory.return_value
        session.get.return_value = response(text='"SNlM0e":"csrf","FdrFJe":"123"')
        stream = [b")]}'", b"1234", json.dumps([gemini_snapshot("old"), gemini_snapshot("final answer")]).encode()]
        session.post.side_effect = [
            response(headers={"X-Goog-Upload-Url": "https://upload.test/1"}), response(text="uri-1"),
            response(headers={"X-Goog-Upload-Control-Url": "https://upload.test/2"}), response(text="uri-2"),
            response(lines=stream),
        ]
        result = providers.complete_external("gemini", "extract", [(image_bytes(), "01.png"), (image_bytes(), "02.png")])
        self.assertEqual(result, "final answer")
        call = session.post.call_args
        self.assertIn("/u/1/_/BardChatUi/", call.args[0])
        payload = json.loads(json.loads(call.kwargs["data"]["f.req"])[1])
        self.assertEqual(payload[0][0], "extract")
        self.assertEqual(payload[0][3], [[["uri-1", 1], "01.png"], [["uri-2", 1], "02.png"]])
        self.assertEqual(payload[2][:3], ["", "", ""])
        self.assertEqual(call.kwargs["headers"]["x-goog-ext-525001261-jspb"], providers.GeminiClient.MODEL_HEADER)
        self.assertNotIn("verify", call.kwargs)  # TLS verification keeps requests' secure default.
        session.close.assert_called_once()

    @patch("utils.providers.requests.Session")
    def test_gemini_expired_cookie_page_is_not_a_success(self, factory):
        session = factory.return_value
        session.get.return_value = response(text="Sign in")
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.complete_external("gemini", "prompt")
        session.post.assert_not_called()
        session.close.assert_called_once()

    @patch("utils.providers.requests.Session")
    def test_gemini_resumes_all_four_saved_context_fields(self, factory):
        self.states["gemini"] = {"conversation_id": "conversation", "response_id": "saved-response", "choice_id": "saved-choice", "at_token": "saved-at"}
        session = factory.return_value
        session.get.return_value = response(text='"SNlM0e":"csrf","FdrFJe":"123"')
        session.post.return_value = response(lines=[json.dumps([gemini_snapshot("answer")]).encode()])
        providers.complete_external("gemini", "continue")
        body = json.loads(json.loads(session.post.call_args.kwargs["data"]["f.req"])[1])
        self.assertEqual(body[2][:3], ["conversation", "saved-response", "saved-choice"])
        self.assertEqual(body[2][9], "saved-at")

    @patch("utils.providers.requests.Session")
    def test_gpt_project_and_parent_are_in_every_continuation(self, factory):
        self.states["gpt"] = {"conversation_id": "test-conversation", "gizmo_id": "project-id"}
        factory.return_value.get.return_value = response(data={"current_node": "current-parent"})
        factory.return_value.post.return_value = response(lines=[gpt_event("answer"), b"data: [DONE]"])
        providers.complete_external("gpt", "continue")
        body = factory.return_value.post.call_args.kwargs["json"]
        self.assertEqual(body["conversation_id"], "test-conversation")
        self.assertEqual(body["parent_message_id"], "current-parent")
        self.assertEqual(body["conversation_mode"], {"kind": "gizmo_interaction", "gizmo_id": "project-id"})
        self.assertEqual(body["messages"][0]["metadata"]["gizmo_id"], "project-id")

    @patch("utils.providers.requests.Session")
    def test_server_cannot_silently_replace_owner_selected_chat(self, factory):
        self.states["gpt"] = {"conversation_id": "owner-chosen-chat"}
        factory.return_value.get.return_value = response(data={"current_node": "current-parent"})
        factory.return_value.post.return_value = response(lines=[gpt_event("wrong conversation")])
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.complete_external("gpt", "continue")
        self.assertEqual(self.states["gpt"]["conversation_id"], "owner-chosen-chat")

    @patch("utils.providers.requests.Session")
    def test_projects_use_original_supported_limit_of_five_conversations(self, factory):
        factory.return_value.get.return_value = response(data={"items": [{"gizmo": {"gizmo": {"id": "project", "display": {"name": "Manga"}}}, "conversations": {"items": [{"id": "chat"}]}}]})
        client = providers.ChatGPTClient()
        self.assertEqual(client.projects()[0]["chats"], [{"id": "chat"}])
        self.assertIn("conversations_per_gizmo=5", factory.return_value.get.call_args.args[0])

    def test_parallel_requests_do_not_fork_the_shared_conversation(self):
        active = 0
        maximum = 0
        starts = []
        class Client:
            def __init__(inner, config):
                inner.session = MagicMock()
            def complete(inner, prompt, images, files):
                nonlocal active, maximum
                starts.append(inner.state.get("conversation_id"))
                active += 1
                maximum = max(maximum, active)
                time.sleep(.01)
                inner.state["conversation_id"] = "one-shared-conversation"
                active -= 1
                return "answer"
        with patch("utils.providers.ChatGPTClient", Client), ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(list(executor.map(lambda _: providers.complete_external("gpt", "prompt"), range(4))), ["answer"] * 4)
        self.assertEqual(maximum, 1)
        self.assertEqual(starts, [None] + ["one-shared-conversation"] * 3)

    def test_another_accounts_chat_is_never_used(self):
        self.states["gpt"] = {"account_id": "another-account", "conversation_id": "other-chat"}
        with patch("utils.providers.requests.Session") as session:
            with self.assertRaises(providers.ProviderConfigurationError):
                providers.complete_external("gpt", "prompt")
        session.assert_not_called()

    @patch("utils.providers.requests.put")
    @patch("utils.providers.requests.Session")
    def test_gpt_multiple_images_real_dimensions_and_full_snapshots(self, factory, put):
        session = factory.return_value
        put.return_value = response(status=201)
        session.post.side_effect = [
            response(data={"upload_url": "https://blob.test/1", "file_id": "file-1"}), response(),
            response(data={"upload_url": "https://blob.test/2", "file_id": "file-2"}), response(),
            response(lines=[gpt_event("user ignored", "user"), gpt_event("Hel"), gpt_event("Hello"), b"data: [DONE]"]),
        ]
        result = providers.complete_external("gpt", "read", [(image_bytes("JPEG"), "01.jpg"), (image_bytes(), "02.png")])
        self.assertEqual(result, "Hello")
        body = session.post.call_args.kwargs["json"]
        msg = body["messages"][0]
        self.assertEqual(msg["content"]["content_type"], "multimodal_text")
        self.assertEqual([x["asset_pointer"] for x in msg["content"]["parts"][:-1]], ["file-service://file-1", "file-service://file-2"])
        self.assertEqual(msg["content"]["parts"][0]["width"], 24)
        self.assertEqual(msg["content"]["parts"][0]["height"], 48)
        self.assertEqual(msg["content"]["parts"][-1], "read")
        self.assertEqual([x["name"] for x in msg["metadata"]["attachments"]], ["01.jpg", "02.png"])
        self.assertNotIn("conversation_id", body)
        for call in put.call_args_list:
            self.assertNotIn("Authorization", call.kwargs["headers"])
        session.close.assert_called_once()

    @patch("utils.providers.requests.Session")
    def test_text_requests_resume_one_persistent_conversation(self, factory):
        sessions = [MagicMock(), MagicMock()]
        factory.side_effect = sessions
        bodies = []
        for session in sessions:
            session.post.return_value = response(lines=[gpt_event("answer"), b"data: [DONE]"])
            session.get.return_value = response(data={"current_node": "server-current-node"})
        for _ in range(2):
            self.assertEqual(providers.complete_external("gpt", "translate"), "answer")
        for session in sessions:
            self.assertEqual(session.post.call_count, 1)
            bodies.append(session.post.call_args.kwargs["json"])
            self.assertEqual(bodies[-1]["messages"][0]["content"], {"content_type": "text", "parts": ["translate"]})
        self.assertNotIn("conversation_id", bodies[0])
        self.assertEqual(bodies[1]["conversation_id"], "test-conversation")
        self.assertEqual(bodies[1]["parent_message_id"], "server-current-node")
        self.assertEqual(self.states["gpt"]["conversation_id"], "test-conversation")

    @patch("utils.providers.requests.put")
    @patch("utils.providers.requests.Session")
    def test_non_image_file_attachment_uses_source_upload_and_ack(self, factory, put):
        put.return_value = response(status=201)
        factory.return_value.post.side_effect = [response(data={"upload_url": "https://blob.test/file", "file_id": "text-file"}), response(), response(lines=[gpt_event("file contents"), b"data: [DONE]"])]
        answer = providers.complete_external("gpt", "read file", files=[(b"file contents", "file.txt")])
        self.assertEqual(answer, "file contents")
        message = factory.return_value.post.call_args.kwargs["json"]["messages"][0]
        self.assertEqual(message["content"]["content_type"], "text")
        self.assertEqual(message["metadata"]["attachments"][0]["id"], "text-file")

    @patch("utils.providers.time.sleep")
    @patch("utils.providers.requests.Session")
    def test_failed_or_empty_stream_is_retried_then_raises(self, factory, sleep):
        session = factory.return_value
        session.post.side_effect = [response(status=429), response(lines=[gpt_event(""), b"data: [DONE]"])]
        with self.assertRaises(RuntimeError):
            providers.complete_external("gpt", "prompt", max_attempts=2)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(session.close.call_count, 2)

    @patch("utils.providers.requests.Session")
    def test_stream_error_after_text_is_not_returned_as_success(self, factory):
        factory.return_value.post.return_value = response(lines=[gpt_event("partial"), b'data: {"error":"private server detail"}'])
        with self.assertRaises(RuntimeError) as raised:
            providers.complete_external("gpt", "prompt", max_attempts=1)
        self.assertNotIn("private server detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
