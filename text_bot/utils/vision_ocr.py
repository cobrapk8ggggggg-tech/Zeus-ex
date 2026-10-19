"""One request per page; only strips over 16000px become three ordered parts."""
import io
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from PIL import Image
from utils.providers import complete_external

SPLIT_THRESHOLD = 16000
MAX_PARTS = 3


@dataclass
class Crop:
    data: bytes
    name: str
    top: int
    bottom: int
    core_top: int
    core_bottom: int
    width: int


def make_crops(data, name, overlap=120):
    """Keep ordinary images byte-for-byte; cover giant strips in exactly 3 parts."""
    with Image.open(io.BytesIO(data)) as original:
        width, height = original.size
        if height <= SPLIT_THRESHOLD:
            return [Crop(data, name, 0, height, 0, height, width)]
        image = original.convert("RGB")
    cuts = [0, height // 3, height * 2 // 3, height]
    result = []
    for i, (core_top, core_bottom) in enumerate(zip(cuts, cuts[1:]), 1):
        top, bottom = max(0, core_top - overlap), min(height, core_bottom + overlap)
        output = io.BytesIO()
        image.crop((0, top, width, bottom)).save(output, format="PNG")
        result.append(Crop(output.getvalue(), f"{name}.part-{i:03}.png",
                           top, bottom, core_top, core_bottom, width))
    return result


def transcription_prompt(crops, scope="all"):
    scope_rule = (
        "Include every readable word: dialogue, thoughts, narration, side comments, sound effects, "
        "status panels, names, numbers, skills and UI labels."
        if scope == "all" else
        "Include dialogue, thoughts, narration, captions and status/UI panels; omit pure sound effects and watermarks."
    )
    descriptions = []
    for i, crop in enumerate(crops, 1):
        size = crop.bottom - crop.top
        core = [round((crop.core_top - crop.top) * 1000 / size),
                round((crop.core_bottom - crop.top) * 1000 / size)]
        descriptions.append({"part": i, "filename": crop.name,
                             "width": crop.width, "height": size,
                             "owned_vertical_range": core})
    return (
        "CURRENT TASK: literal OCR of THIS page, NOT translation. Ignore earlier images, language choices, "
        "translation tasks and story knowledge. The attached images are consecutive parts of ONE long "
        "manhwa page, attached in TOP-TO-BOTTOM order. Read every part completely in one response. "
        "Part 1 comes first, then 2, then 3, if present. Never merge or skip different parts.\n"
        + scope_rule + "\n"
        "Copy printed text in its ORIGINAL language and script, including mixed English/Korean/Chinese. "
        "Never translate, paraphrase, restore an imagined original language, correct spelling or invent words. "
        "Keep capitalization, punctuation and every digit. Distinguish printed characters from background art. "
        "Read each part from top to bottom. Merge wrapped lines within ONE bubble into a single string; "
        "use one record per bubble/caption or status-panel row. Mark unreadable text illegible instead of guessing.\n"
        "Neighbouring parts have a small context overlap; include a block only in the part whose owned range "
        "contains its vertical centre. Coordinates refer to the FULL attached part, including its overlap.\n"
        "Return ONLY complete JSON, no Markdown:\n"
        '{"parts":[{"part":1,"regions":[{"text":"literal source text","box":[left,top,right,bottom],'
        '"kind":"speech|thought|caption|ui|sfx","illegible":false}]}]}\n'
        "Return exactly one parts entry for EVERY attachment, even if its regions list is empty. "
        "Boxes must be normalized to 0..1000 relative to that part and cover the text, not the whole panel. "
        "Sort bubbles by their vertical reading order; on the same row use their visual reading flow.\n"
        "ATTACHMENT MANIFEST:\n" + json.dumps(descriptions, ensure_ascii=False)
    )


def unwrap_response(raw):
    """Accept Markdown fences of any language and harmless JSON wrappers."""
    text = str(raw or "").strip().lstrip("\ufeff")
    match = re.fullmatch(r"```[^\n`]*\n([\s\S]*?)\n?```", text)
    if match:
        text = match.group(1).strip()
    decoder = json.JSONDecoder()
    for candidate in (text, *re.findall(r"```[^\n`]*\n([\s\S]*?)```", text)):
        try:
            payload = json.loads(candidate)
        except ValueError:
            if candidate.strip().startswith(("{", "[")):
                raise RuntimeError("رد الاستخراج يحتوي JSON غير مكتمل.") from None
            # Narrative around a complete JSON object is harmless; partial JSON is not.
            payload = None
            for start in re.finditer(r"[\[{]", candidate):
                try:
                    value, _ = decoder.raw_decode(candidate[start.start():])
                    if isinstance(value, dict) and any(k in value for k in ("parts", "regions", "text")):
                        payload = value
                        break
                except ValueError:
                    pass
            if payload is None:
                continue
        for _ in range(4):
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                    continue
                except ValueError:
                    return payload.strip()
            if isinstance(payload, dict) and not any(k in payload for k in ("parts", "regions")):
                wrapped = next((payload[k] for k in ("response", "content", "result", "text")
                                if isinstance(payload.get(k), (str, dict, list))), None)
                if wrapped is not None:
                    if isinstance(wrapped, str):
                        return unwrap_response(wrapped)
                    payload = wrapped
                    continue
            break
        return payload
    if not text or text.startswith(("{", "[")):
        raise RuntimeError("رد الاستخراج فارغ أو JSON غير مكتمل.")
    if re.search(r"(?:I (?:cannot|can't|am unable)|unable to (?:read|view|access)|لا (?:أستطيع|يمكنني)|تعذر (?:عرض|قراءة))", text, re.I):
        raise RuntimeError("لم يتمكن المزود من قراءة المرفق.")
    return text


def _clean_regions(payload):
    if isinstance(payload, str):
        regions = [{"text": line} for line in payload.splitlines() if line.strip()]
    elif isinstance(payload, list):
        regions = payload
    else:
        regions = payload.get("regions") if isinstance(payload, dict) else None
    if not isinstance(regions, list):
        raise RuntimeError("رد الاستخراج لا يحتوي قائمة المقاطع المطلوبة.")
    clean = []
    for index, region in enumerate(regions):
        if isinstance(region, str):
            region = {"text": region}
        if not isinstance(region, dict) or not isinstance(region.get("text"), str):
            raise RuntimeError("أحد مقاطع الاستخراج غير صالح.")
        box = region.get("box")
        synthetic = box is None
        if synthetic:
            top = index * 1000 / max(1, len(regions))
            box = [0, top, 1000, top + 500 / max(1, len(regions))]
        if not isinstance(box, list) or len(box) != 4 or any(
            isinstance(n, bool) or not isinstance(n, (float, int)) or not 0 <= n <= 1000 for n in box
        ) or box[2] <= box[0] or box[3] <= box[1]:
            raise RuntimeError("إحداثيات النص غير صالحة.")
        text = " ".join(region["text"].split())
        marked = bool(re.fullmatch(r"[\[⟦<]\s*(?:illegible|unreadable|غير\s+مقروء)\s*[\]⟧>]", text, re.I))
        if not text and not region.get("illegible"):
            continue
        clean.append({"text": "⟦غير مقروء⟧" if marked or not text else text, "box": box,
                      "kind": str(region.get("kind", "speech")), "synthetic_box": synthetic,
                      "illegible": bool(region.get("illegible")) or marked})
    return clean


def parse_regions(raw):
    return _clean_regions(unwrap_response(raw))


def parse_parts(raw, count):
    payload = unwrap_response(raw)
    if count == 1 and (not isinstance(payload, dict) or "parts" not in payload):
        return [_clean_regions(payload)]
    if isinstance(payload, str):
        # Plain fenced text is usable for batches only with every explicit part marker.
        matches = list(re.finditer(r"(?im)^\s*(?:<<<|===|##?\s*)\s*(?:part|image)\s*(\d+)\s*(?:>>>|===|:)?\s*$", payload))
        parts = [{"part": int(m.group(1)), "regions": [
            line for line in payload[m.end():matches[i+1].start() if i+1 < len(matches) else len(payload)].splitlines() if line.strip()
        ]} for i, m in enumerate(matches)]
    else:
        parts = payload.get("parts") if isinstance(payload, dict) else payload
    if not isinstance(parts, list) or len(parts) != count:
        raise RuntimeError("رد الاستخراج ناقص: يجب أن يحتوي كل أجزاء الصورة.")
    indexed = {}
    for part in parts:
        number = part.get("part") if isinstance(part, dict) else None
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= count or number in indexed:
            raise RuntimeError("ترتيب أجزاء الرد غير صالح.")
        indexed[number] = _clean_regions(part)
    return [indexed[i] for i in range(1, count + 1)]


def _overlap(a, b):
    intersection = max(0, min(a[3], b[3]) - max(a[1], b[1])) * max(0, min(a[2], b[2]) - max(a[0], b[0]))
    area = min((a[3] - a[1]) * (a[2] - a[0]), (b[3] - b[1]) * (b[2] - b[0]))
    return intersection / max(1, area)


def extract_page(data, name, config, *, bubble_spacing=True, progress=None, diagnostics=None, thinking_enabled=False, response_observer=None):
    crops = make_crops(data, name)
    if progress and len(crops) > 1:
        progress("split_batch", name, len(crops), len(crops), "")
    prompt = transcription_prompt(crops, config.ocr_scope)
    images = [(crop.data, crop.name) for crop in crops]
    # Conversation resets and provider fallback belong to the chapter coordinator.
    if diagnostics:
        diagnostics({"event": "batch_request", "page": name, "attachments": len(crops),
                     "source_height": crops[-1].core_bottom, "attempt": 1})
    raw = complete_external(config.provider, prompt, images, config=config, max_attempts=1,
                            thinking_enabled=thinking_enabled)
    if response_observer:
        response_observer(raw)
    sections = parse_parts(raw, len(crops))
    records = []
    for crop, regions in zip(crops, sections):
        scale = (crop.bottom - crop.top) / 1000
        for item in regions:
            left, top, right, bottom = item["box"]
            absolute = [left, crop.top + top * scale, right, crop.top + bottom * scale]
            if config.ocr_scope == "dialogue" and item["kind"].lower() in ("sfx", "sound", "sound_effect"):
                continue
            if not item["synthetic_box"] and not crop.core_top <= (absolute[1] + absolute[3]) / 2 < crop.core_bottom:
                continue
            if any(_overlap(r["box"], absolute) > .35 and
                   SequenceMatcher(None, r["text"], item["text"]).ratio() > .85 for r in records):
                continue
            records.append({**item, "box": absolute})
    records.sort(key=lambda r: (r["box"][1], r["box"][0]))
    return ("\n\n" if bubble_spacing else "\n").join(r["text"] for r in records)
