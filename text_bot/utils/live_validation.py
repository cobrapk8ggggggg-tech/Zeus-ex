"""Opt-in, one-time validation on the deployed service using its saved sessions.

No credential values are exported. Reports are stored in MongoDB and available
only through the owner's private /zx console.
"""
import io
import json
import re
import time
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from utils.providers import get_processing_config, inspect_account, provider_thread, complete_external
import uuid
import requests
from utils.storage import _get_db
from utils.vision_ocr import extract_page


GOLDEN = {
    "1.jpg": ["엄마는 산에서의 일을 기억 못해", "이름 강진혁", "보유한 코인 1,283,288", "이걸 얻게 될 줄이야", "거처를 마련해 주든가"],
    "2.jpg": ["주위 병사들은 그 병사를 보고 비웃기 시작했지"],
    "3.jpg": ["I USED IT BACK AT MY CASTLE DURING THE TUTORIAL", "WHEN IT WAS AT MAX LEVEL, I COULD EVEN READ THE OTHER PERSON'S MIND",
              "恭喜你解锁第一个技能栏", "没想到神话级职业者", "连升3级", "物理防御", "魔法防御"],
    "4.jpg": ["매그너스 네가 그렇게 말해도", "영애와 잘 됐으면 좋겠어", "나는 너에게", "내 마음이 변함없다는 걸 계속 증명할게"],
}


def normalized(text):
    return re.sub(r"[^\w]", "", text, flags=re.UNICODE).lower()


def english_fixture(api):
    image = Image.new("RGB", (760, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(api.BASE_DIR / "assets/fonts/Montserrat-Bold.ttf"), 24)
    lines = ["WE CANNOT CHANGE THE PAST.", "Keep every word in English.", "Name: ZEUS", "Level: 32", "Coins: 1,283,288"]
    for i, line in enumerate(lines):
        draw.text((30, 50 + 95 * i), line, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), lines


def run_validation(api, marker, folder, mode="full"):
    collection = _get_db().validation_runs
    previous = collection.find_one({"_id": marker})
    if previous and previous.get("status") != "running":
        return
    if previous:
        # One Railway replica: allow the draining predecessor to stop, then
        # resume only unfinished pages after a release/restart.
        time.sleep(20)
        previous = collection.find_one({"_id": marker})
        if previous.get("status") != "running":
            return
        report = previous
    else:
        report = {"_id": marker, "mode": mode, "started_at": int(time.time()), "status": "running", "pages": [], "accounts": {}}
        collection.insert_one(report)
    print("[ZEUS_VALIDATION] " + json.dumps({"run": marker, "event": "resumed" if previous else "started", "completed_pages": len(report["pages"])}), flush=True)
    try:
        config = get_processing_config()
        report["model"] = config.chatgpt_model
        for provider, configured in (("gpt", bool(config.chatgpt_access_token)), ("gemini", bool(config.gemini_cookie_header))):
            if not configured:
                report["accounts"][provider] = {"status": "not_configured"}
                continue
            try:
                candidate = replace(config, provider=provider)
                account = inspect_account(provider, "account", candidate)
                summary = {"status": "connected", "models": account.get("available_models", 1)}
                if provider == "gpt":
                    summary["model_list_available"] = account.get("model_list_available")
                    if account.get("model_list_error"):
                        summary["model_list_error"] = account["model_list_error"]
                    for action in ("projects", "chats"):
                        try:
                            summary[action] = len(inspect_account(provider, action, candidate))
                        except Exception as exc:
                            summary[action + "_error"] = api.sanitize_error(exc)
                report["accounts"][provider] = summary
            except Exception as exc:
                report["accounts"][provider] = {"status": "failed", "error": api.sanitize_error(exc)}
        print("[ZEUS_VALIDATION_ACCOUNTS] " + json.dumps({"accounts": report["accounts"], "model": config.chatgpt_model}, ensure_ascii=False), flush=True)
        if mode == "accounts":
            report["status"] = "completed"
            return
        if report["accounts"].get("gpt", {}).get("status") != "connected":
            raise RuntimeError("جلسة GPT المحفوظة غير متصلة؛ لم تُجر تجربة استخراج حية.")
        images = api.process_drive_link("https://drive.google.com/drive/folders/" + folder)
        images.sort(key=lambda item: api.natural_sort_key(item[1]))
        candidate = replace(config, provider="gpt")
        initial = provider_thread("gpt").get("conversation_id")
        conversation = initial
        report["same_conversation"] = True
        if mode == "focused":
            def diagnostics(detail):
                report.setdefault("diagnostics", []).append(detail)
                print("[ZEUS_VALIDATION_DETAIL] " + json.dumps(detail), flush=True)
            data = next(data for data, name in images if name == "3.jpg")
            with Image.open(io.BytesIO(data)) as image:
                crop = image.crop((0, max(0, image.height - 800), image.width, image.height))
                output = io.BytesIO()
                crop.save(output, format="PNG")
            text = extract_page(output.getvalue(), "3-small-numeric-ui.png", candidate, diagnostics=diagnostics)
            expected = "22106649亿5999万9075"
            report["fine_numeric_check"] = {"expected": expected, "found": normalized(expected) in normalized(text), "text": text}
            line = "ZEUS_FILE_VALIDATION_" + uuid.uuid4().hex
            answer = complete_external("gpt", "Read the attached text file and return its one line exactly.",
                                       files=[((line + "\n").encode(), "zeus-validation.txt")], config=candidate)
            report["file_attachment_check"] = {"found": line in answer, "output": answer}
            report["same_conversation"] = provider_thread("gpt").get("conversation_id") == conversation
            report["status"] = "completed"
            return
        for data, name in images:
            if any(page["name"] == name for page in report["pages"]):
                continue
            started = time.monotonic()
            def progress(stage, page, current=0, total=0, label=""):
                print("[ZEUS_VALIDATION_REGION] " + json.dumps({"page": page, "region": current, "total": total}), flush=True)
            text = extract_page(data, name, candidate, progress=progress)
            cid = provider_thread("gpt").get("conversation_id")
            conversation = conversation or cid
            report["same_conversation"] &= bool(cid and cid == conversation)
            checks = [{"reference": ref, "found": normalized(ref) in normalized(text)} for ref in GOLDEN.get(name, [])]
            positions = [normalized(text).find(normalized(c["reference"])) for c in checks]
            row = {"name": name, "text": text, "characters": len(text), "checks": checks,
                   "reference_order": all(a < b for a, b in zip(positions, positions[1:]) if a >= 0 and b >= 0),
                   "seconds": round(time.monotonic() - started, 1)}
            report["pages"].append(row)
            collection.update_one({"_id": marker}, {"$set": {k: v for k, v in report.items() if k != "_id"}})
            print("[ZEUS_VALIDATION] " + json.dumps({"page": name, "characters": len(text), "checks": checks, "order": row["reference_order"], "same_conversation": report["same_conversation"]}, ensure_ascii=False), flush=True)
        data, lines = english_fixture(api)
        text = extract_page(data, "english-fixture.png", candidate)
        report["english_language_lock"] = {"output": text, "all_lines_present": all(normalized(line) in normalized(text) for line in lines),
                                           "no_korean_conversion": not bool(re.search(r"[\uac00-\ud7a3]", text))}
        if any(name == "3.jpg" for _, name in images):
            data = next(data for data, name in images if name == "3.jpg")
            with Image.open(io.BytesIO(data)) as image:
                crop = image.crop((0, max(0, image.height - 800), image.width, image.height))
                output = io.BytesIO()
                crop.save(output, format="PNG")
            text = extract_page(output.getvalue(), "3-small-numeric-ui.png", candidate)
            expected = "22106649亿5999万9075"
            report["fine_numeric_check"] = {"expected": expected, "found": normalized(expected) in normalized(text), "text": text}
        report["same_conversation"] &= provider_thread("gpt").get("conversation_id") == conversation
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = "تعذر الاتصال بخادم الملفات." if isinstance(exc, requests.RequestException) else api.sanitize_error(exc)
        cause = exc.__cause__
        if cause is not None and "HTTP " in str(cause):
            report["server_error"] = api.sanitize_error(cause)
    finally:
        report["finished_at"] = int(time.time())
        collection.update_one({"_id": marker}, {"$set": {k: v for k, v in report.items() if k != "_id"}})
        # Page transcription is test data, never credentials.
        for page in report["pages"]:
            print("[ZEUS_VALIDATION_TEXT] " + json.dumps({"page": page["name"], "text": page["text"]}, ensure_ascii=False), flush=True)
        print("[ZEUS_VALIDATION_RESULT] " + json.dumps({k: v for k, v in report.items() if k not in ("pages", "_id")}, ensure_ascii=False), flush=True)


def latest_report():
    runs = _get_db().validation_runs
    report = runs.find_one({"pages.0": {"$exists": True}}, {"_id": 0}, sort=[("started_at", -1)])
    report = report or runs.find_one({}, {"_id": 0}, sort=[("started_at", -1)])
    if report:
        for page in report.get("pages", []):
            page["checks"] = [{"reference": ref, "found": normalized(ref) in normalized(page["text"])}
                              for ref in GOLDEN.get(page["name"], [])]
        recheck = runs.find_one({"mode": "accounts"}, {"_id": 0}, sort=[("started_at", -1)])
        if recheck and recheck.get("finished_at", 0) > report.get("finished_at", 0):
            report["account_recheck"] = recheck
        focus = runs.find_one({"mode": "focused"}, {"_id": 0}, sort=[("started_at", -1)])
        if focus:
            report["focused_recheck"] = focus
    return report
