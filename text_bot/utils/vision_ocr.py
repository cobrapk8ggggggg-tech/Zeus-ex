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


def parse_regions(raw):
    raw = re.sub(r"^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$", "", raw.strip(), flags=re.I)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise RuntimeError("بنية رد الاستخراج غير مكتملة؛ ستعاد محاولة الطلب.") from None
    regions = payload.get("regions") if isinstance(payload, dict) else None
    if not isinstance(regions, list):
        raise RuntimeError("رد الاستخراج لا يحتوي قائمة المقاطع المطلوبة.")
    clean = []
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("text"), str):
            raise RuntimeError("أحد مقاطع الاستخراج غير صالح.")
        box = region.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(
            isinstance(n, bool) or not isinstance(n, (float, int)) or not 0 <= n <= 1000 for n in box
        ) or box[2] <= box[0] or box[3] <= box[1]:
            raise RuntimeError("إحداثيات النص غير صالحة.")
        text = " ".join(region["text"].split())
        marked = bool(re.fullmatch(r"[\[⟦<]\s*(?:illegible|unreadable|غير\s+مقروء)\s*[\]⟧>]", text, re.I))
        if not text and not region.get("illegible"):
            continue
        clean.append({"text": "⟦غير مقروء⟧" if marked or not text else text, "box": box,
                      "kind": str(region.get("kind", "speech")), "illegible": bool(region.get("illegible")) or marked})
    return clean


def parse_parts(raw, count):
    raw = re.sub(r"^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$", "", raw.strip(), flags=re.I)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise RuntimeError("رد الأجزاء غير مكتمل.") from None
    # Accept the old one-image shape without spending a retry on a harmless format difference.
    if count == 1 and isinstance(payload, dict) and "regions" in payload:
        return [parse_regions(json.dumps(payload, ensure_ascii=False))]
    parts = payload.get("parts") if isinstance(payload, dict) else None
    if not isinstance(parts, list) or len(parts) != count:
        raise RuntimeError("رد الاستخراج ناقص: يجب أن يحتوي كل أجزاء الصورة.")
    indexed = {}
    for part in parts:
        number = part.get("part") if isinstance(part, dict) else None
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= count or number in indexed:
            raise RuntimeError("ترتيب أجزاء الرد غير صالح.")
        indexed[number] = parse_regions(json.dumps(part, ensure_ascii=False))
    return [indexed[i] for i in range(1, count + 1)]


def _overlap(a, b):
    intersection = max(0, min(a[3], b[3]) - max(a[1], b[1])) * max(0, min(a[2], b[2]) - max(a[0], b[0]))
    area = min((a[3] - a[1]) * (a[2] - a[0]), (b[3] - b[1]) * (b[2] - b[0]))
    return intersection / max(1, area)


def extract_page(data, name, config, *, bubble_spacing=True, progress=None, diagnostics=None, thinking_enabled=False):
    crops = make_crops(data, name)
    if progress and len(crops) > 1:
        progress("split_batch", name, len(crops), len(crops), "")
    prompt = transcription_prompt(crops, config.ocr_scope)
    images = [(crop.data, crop.name) for crop in crops]
    # No automatic audit/detail requests. Retry only a failed or malformed batch.
    for attempt in range(2):
        if diagnostics:
            diagnostics({"event": "batch_request", "page": name, "attachments": len(crops),
                         "source_height": crops[-1].core_bottom, "attempt": attempt + 1})
        try:
            raw = complete_external(config.provider, prompt, images, config=config, max_attempts=1,
                                    thinking_enabled=thinking_enabled)
            sections = parse_parts(raw, len(crops))
            break
        except Exception as exc:
            if attempt:
                raise RuntimeError("تعذر استخراج الصورة كاملة؛ لم يتم قبول رد ناقص.") from exc
            prompt += "\nThe previous request failed or returned incomplete JSON. Return all manifest parts."
    records = []
    for crop, regions in zip(crops, sections):
        scale = (crop.bottom - crop.top) / 1000
        for item in regions:
            left, top, right, bottom = item["box"]
            absolute = [left, crop.top + top * scale, right, crop.top + bottom * scale]
            if not crop.core_top <= (absolute[1] + absolute[3]) / 2 < crop.core_bottom:
                continue
            if any(_overlap(r["box"], absolute) > .35 and
                   SequenceMatcher(None, r["text"], item["text"]).ratio() > .85 for r in records):
                continue
            records.append({**item, "box": absolute})
    records.sort(key=lambda r: (r["box"][1], r["box"][0]))
    return ("\n\n" if bubble_spacing else "\n").join(r["text"] for r in records)
