"""Ordered, lossless-image transcription for the account-based providers.

Qwen's existing extraction path deliberately does not use this module.
"""
import io
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from PIL import Image, ImageStat

from utils.providers import complete_external


@dataclass
class Crop:
    data: bytes
    name: str
    top: int
    bottom: int
    core_top: int
    core_bottom: int
    width: int


def make_crops(data, name, target=1700, overlap=240):
    """Cover every original pixel. Crop borders carry context on both sides."""
    with Image.open(io.BytesIO(data)) as original:
        original.load()
        image = original.convert("RGB")
    width, height = image.size
    cuts = [0]
    while height - cuts[-1] > target + 300:
        desired = cuts[-1] + target
        # Prefer a quiet horizontal gutter over cutting through dialogue/art.
        candidates = range(max(cuts[-1] + 900, desired - 220), min(height - 500, desired + 220), 16)
        def score(y):
            strip = image.crop((0, y - 3, width, y + 4)).convert("L")
            return ImageStat.Stat(strip).stddev[0] + abs(y - desired) / 80
        cut = min(candidates, key=score, default=desired)
        cuts.append(cut)
    cuts.append(height)
    result = []
    for i, (core_top, core_bottom) in enumerate(zip(cuts, cuts[1:]), 1):
        top, bottom = max(0, core_top - overlap), min(height, core_bottom + overlap)
        output = io.BytesIO()
        image.crop((0, top, width, bottom)).save(output, format="PNG")
        result.append(Crop(output.getvalue(), f"{name}.region-{i:03}.png", top, bottom, core_top, core_bottom, width))
    return result


def transcription_prompt(crop, scope="all", *, draft=None):
    task = "AUDIT AND CORRECT the draft by reading the attached image again" if draft is not None else "TRANSCRIBE the attached image"
    scope_rule = (
        "Include EVERY visible readable word: dialogue, thoughts, narration, small side comments, sound effects, "
        "status windows, names, numbers, skills and UI labels. Do not discard a status panel as background."
        if scope == "all" else
        "Include all dialogue, thoughts, narration, captions and status/UI windows. Omit only pure sound effects and watermarks."
    )
    prompt = (
        f"CURRENT TASK: {task}. This is literal OCR, NOT translation.\n"
        "Previous messages may have contained translation tasks or other images. IGNORE their language and their content. "
        "Only the ONE image attached to THIS message is the source of truth.\n"
        f"Original image crop size: {crop.width} x {crop.bottom - crop.top} pixels.\n"
        f"{scope_rule}\n"
        "Read the entire crop from its TOP to its BOTTOM, including its very first and last panels. "
        "Then scan it once more for missed small text. Never summarize, paraphrase, fix spelling, invent a name, "
        "restore a supposed original language, or use story knowledge. English printed text MUST stay English; "
        "Korean MUST stay Korean; Arabic MUST stay Arabic. Mixed languages remain mixed exactly where printed. "
        "Preserve capitalization, punctuation and digits. Merge wrapped lines WITHIN ONE bubble into one text string. "
        "Use one record per bubble/caption or per row of a status panel; never combine different bubbles. "
        "Do not guess unreadable text: mark that region illegible.\n"
        'Return ONLY a JSON object: {"regions":[{"text":"literal text",'
        '"box":[left,top,right,bottom],"kind":"speech|thought|caption|ui|sfx","illegible":false}]} . '
        "Coordinates MUST be normalized to 0..1000 relative to THIS crop. "
        "Boxes cover the text block, not the whole panel. Sort by vertical reading position; "
        "for bubbles on the same row follow their visual reading flow, not a blanket manga right-to-left rule. "
        'If there is no text, return {"regions":[]}. No Markdown or commentary.\n'
    )
    if draft is not None:
        prompt += "DRAFT TO CHECK (not source text):\n" + json.dumps({"regions": draft}, ensure_ascii=False)
        prompt += "\nCheck every letter and all omitted regions. Return the complete corrected JSON, not a list of changes."
    return prompt


def parse_regions(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise RuntimeError("بنية رد الاستخراج غير مكتملة؛ سيعاد تدقيق المقطع.") from None
    regions = payload.get("regions") if isinstance(payload, dict) else None
    if not isinstance(regions, list):
        raise RuntimeError("رد الاستخراج لا يحتوي قائمة المقاطع المطلوبة.")
    clean = []
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("text"), str):
            raise RuntimeError("أحد مقاطع الاستخراج غير صالح.")
        box = region.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(isinstance(n, bool) or not isinstance(n, (float, int)) or not 0 <= n <= 1000 for n in box):
            raise RuntimeError("إحداثيات النص غير صالحة؛ سيعاد التدقيق.")
        if box[2] <= box[0] or box[3] <= box[1]:
            raise RuntimeError("إحداثيات النص غير مرتبة.")
        text = " ".join(region["text"].split())
        if not text and not region.get("illegible"):
            continue
        clean.append({"text": text if text else "⟦غير مقروء⟧", "box": box,
                      "kind": str(region.get("kind", "speech")), "illegible": bool(region.get("illegible"))})
    return clean


def _overlap(a, b):
    intersection = max(0, min(a[3], b[3]) - max(a[1], b[1])) * max(0, min(a[2], b[2]) - max(a[0], b[0]))
    area = min((a[3] - a[1]) * (a[2] - a[0]), (b[3] - b[1]) * (b[2] - b[0]))
    return intersection / max(1, area)


def reconcile_audit(first, audited):
    # The audit may correct a block, but must not silently drop unrelated blocks.
    result = list(audited)
    for item in first:
        if not any(_overlap(item["box"], other["box"]) > .45 for other in audited):
            result.append(item)
    return sorted(result, key=lambda r: (r["box"][1], r["box"][0]))


def extract_page(data, name, config, *, bubble_spacing=True, progress=None):
    crops = make_crops(data, name, config.ocr_crop_height)
    records = []
    for index, crop in enumerate(crops, 1):
        if progress:
            progress("split_tile", name, index, len(crops), crop.name)
        error = None
        for attempt in range(2):
            try:
                prompt = transcription_prompt(crop, config.ocr_scope)
                if attempt:
                    prompt += "\nThe previous response was incomplete or malformed. Return valid complete JSON."
                raw = complete_external(config.provider, prompt, [(crop.data, crop.name)], config=config, max_attempts=1)
                regions = parse_regions(raw)
                if config.ocr_double_check:
                    audited = parse_regions(complete_external(
                        config.provider, transcription_prompt(crop, config.ocr_scope, draft=regions),
                        [(crop.data, crop.name)], config=config, max_attempts=1,
                    ))
                    regions = reconcile_audit(regions, audited)
                error = None
                break
            except Exception as exc:
                error = exc
        if error is not None:
            raise RuntimeError(f"تعذر تدقيق المقطع {index} من {len(crops)}؛ لم يتم قبول صفحة ناقصة.") from error
        for item in regions:
            left, top, right, bottom = item["box"]
            scale = (crop.bottom - crop.top) / 1000
            absolute = [left, crop.top + top * scale, right, crop.top + bottom * scale]
            center = (absolute[1] + absolute[3]) / 2
            if not crop.core_top <= center < crop.core_bottom:
                continue
            record = {**item, "box": absolute}
            duplicate = next((r for r in records if _overlap(r["box"], absolute) > .35 and
                              SequenceMatcher(None, r["text"], item["text"]).ratio() > .85), None)
            if duplicate is None:
                records.append(record)
    records.sort(key=lambda r: (r["box"][1], r["box"][0]))
    joiner = "\n\n" if bubble_spacing else "\n"
    return joiner.join(r["text"] for r in records)
