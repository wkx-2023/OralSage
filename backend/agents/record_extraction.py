from __future__ import annotations

import re

from .record_fields import MISSING, RecordFields


PUNCT = " ：:，,。；;\n\t"


def _clean(text: str) -> str:
    return re.sub(r"^(医生|患者)\s*[:：]\s*", "", text).strip(PUNCT)


def _first(patterns: list[str], text: str, default: str = MISSING) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            value = next((group for group in match.groups() if group), "").strip(PUNCT)
            if value:
                return _clean(value)
    return default


def _split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in re.split(r"\n+", text):
        sentences.extend(re.split(r"(?<=[。！？!?])\s*", line))
    return [_clean(sentence) for sentence in sentences if _clean(sentence)]


def _sentences_with(text: str, keywords: list[str]) -> list[str]:
    return [sentence for sentence in _split_sentences(text) if any(key in sentence for key in keywords)]


def _join_unique(items: list[str], default: str = MISSING) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = _clean(item)
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return "；".join(result) if result else default


def _extract_labeled_field(text: str, labels: list[str]) -> str:
    joined = "|".join(re.escape(label) for label in labels)
    next_labels = (
        "姓名|性别|年龄|主诉|现病史|既往史|药物过敏史|过敏史|专科检查|口腔检查|"
        "辅助检查|诊断|处理|治疗|医嘱|注意事项|就诊日期"
    )
    pattern = rf"(?:{joined})\s*[:：]\s*(.*?)(?=\n?\s*(?:{next_labels})\s*[:：]|$)"
    return _first([pattern], text)


def _text_after_marker(text: str, marker: str, stop_markers: list[str]) -> str:
    stop = "|".join(re.escape(item) for item in stop_markers)
    pattern = rf"{re.escape(marker)}\s*(.*?)(?=(?:{stop})|$)"
    return _first([pattern], text)


def _clean_diagnosis(value: str) -> str:
    if value == MISSING:
        return value
    value = _clean(value)
    value = re.split(r"[，,；;]\s*(?:建议|需要|处理|治疗|进行|给予|予以)", value, maxsplit=1)[0]
    value = re.sub(r"^诊断\s*[:：]?", "", value).strip(PUNCT)
    return value or MISSING


def _clean_treatment(value: str) -> str:
    if value == MISSING:
        return value
    value = _clean(value)
    match = re.search(r"^(?:建议|需要|处理|治疗|进行|给予|予以)\s*[:：]?\s*(.*)$", value)
    return (match.group(1) if match else value).strip(PUNCT) or MISSING


def _diagnosis_from_exam(exam: str) -> str:
    if exam == MISSING:
        return MISSING
    mapping = [
        ("龋", "龋齿"),
        ("冠周炎", "智齿冠周炎"),
        ("阻生", "阻生牙"),
        ("牙髓炎", "牙髓炎"),
        ("根尖周炎", "根尖周炎"),
        ("牙周炎", "牙周炎"),
    ]
    for keyword, diagnosis in mapping:
        if keyword in exam:
            return diagnosis
    return MISSING


def _extract_summary_parts(text: str) -> dict[str, str]:
    compact = re.sub(r"\s+", "", text)
    return {
        "chief": _first(
            [
                r"(?:自诉|主诉)(.*?)(?=，?检查|，?诊断|，?进行|，?建议|。|$)",
                r"((?:左|右|上|下|前|后|双侧|右下颌|左下颌|右上颌|左上颌)?[^，。；\n]{0,18}(?:牙|智齿)[^，。；\n]{0,24}(?:疼痛|痛|肿痛|敏感|不适)[^，。；\n]{0,12})",
            ],
            compact,
        ),
        "exam": _first(
            [
                r"(?:检查发现|检查见|查见)(.*?)(?=，?诊断|，?进行|，?建议|，?处理|。|$)",
            ],
            compact,
        ),
        "diagnosis": _first(
            [
                r"(?:诊断为|诊断)(.*?)(?=，?建议|，?进行|，?处理|，?治疗|。|$)",
            ],
            compact,
        ),
        "treatment": _first(
            [
                r"(?:进行|建议|处理为|予以|给予)(.*?治疗)(?=，|。|$)",
                r"(?:进行|建议|处理为|予以|给予)(.*?)(?=，?注意|，?术后|。|$)",
            ],
            compact,
        ),
    }


def extract_record_fields(conversation: str) -> RecordFields:
    text = conversation.strip()
    if not text:
        return RecordFields()

    compact_parts = _extract_summary_parts(text)

    name = _first(
        [
            r"(?:患者姓名|姓名)\s*[:：]\s*([\u4e00-\u9fa5A-Za-z]{1,12})",
            r"患者信息[:：].*?姓名\s*([\u4e00-\u9fa5A-Za-z]{1,12})",
        ],
        text,
    )
    sex = _first(
        [
            r"(?:性别)\s*[:：]\s*(男|女)",
            r"(?:^|[：:，,、\s])(男|女)(?:性|患者|[，,、\s]|$)",
        ],
        text,
    )
    age = _first([r"(?:年龄)\s*[:：]?\s*(\d{1,3}\s*岁?)", r"(\d{1,3})\s*岁"], text)

    chief = _extract_labeled_field(text, ["主诉"])
    if chief == MISSING:
        chief = compact_parts["chief"]

    symptom_hits = _sentences_with(text, ["疼", "痛", "肿", "敏感", "冷热", "夜间", "进食", "喝冷水"])
    present = _extract_labeled_field(text, ["现病史"])
    if present == MISSING:
        present = chief if chief != MISSING else _join_unique(symptom_hits)

    past_history = _extract_labeled_field(text, ["既往史"])
    if past_history == MISSING:
        past_history = _first([r"(?:既往|以前).*?(无特殊|否认[^。！？\n]*)"], text)

    allergy = _extract_labeled_field(text, ["药物过敏史", "过敏史"])
    if allergy == MISSING:
        allergy = _first([r"(?:药物过敏史|过敏史).*?(否认|无|未提及|不清楚)", r"否认药物过敏史"], text)
        if allergy == "否认":
            allergy = "否认药物过敏史"

    oral_exam = _extract_labeled_field(text, ["专科检查", "口腔检查"])
    if oral_exam == MISSING:
        oral_exam = compact_parts["exam"]
    if oral_exam == MISSING:
        exam_hits = _sentences_with(text, ["检查", "探痛", "叩诊", "龋", "红肿", "阻生", "半萌出", "牙龈", "冠周", "压痛"])
        exam_hits = [
            item
            for item in exam_hits
            if not any(marker in item for marker in ["诊断", "建议", "需要", "术后", "处理", "治疗", "进行"])
        ]
        oral_exam = _join_unique(exam_hits)

    auxiliary_exam = _extract_labeled_field(text, ["辅助检查"])
    if auxiliary_exam == MISSING:
        auxiliary_exam = _join_unique(_sentences_with(text, ["X线", "X 线", "CBCT", "CT", "片示", "影像"]))

    diagnosis = _extract_labeled_field(text, ["诊断"])
    if diagnosis == MISSING:
        diagnosis = compact_parts["diagnosis"]
    if diagnosis == MISSING:
        diagnosis = _diagnosis_from_exam(oral_exam)
    if diagnosis == MISSING:
        diagnosis = _join_unique(_sentences_with(text, ["诊断", "牙髓炎", "根尖周炎", "冠周炎", "阻生牙", "龋", "牙周炎"]))
    diagnosis = _clean_diagnosis(diagnosis)

    treatment = _extract_labeled_field(text, ["处理", "治疗", "医嘱"])
    if treatment == MISSING:
        treatment = compact_parts["treatment"]
    if treatment == MISSING:
        treatment = _join_unique(
            _sentences_with(
                text,
                ["建议", "需要", "治疗", "进行", "拔除", "根管", "冲洗", "上药", "口服", "局部麻醉", "翻瓣", "缝合", "复诊"],
            )
        )
    treatment = _clean_treatment(treatment)

    advice = _extract_labeled_field(text, ["注意事项"])
    if advice == MISSING:
        advice = _join_unique(_sentences_with(text, ["术后", "24小时", "不漱口", "冰敷", "清淡", "口腔卫生", "加重", "及时复诊", "避免"]))

    visit_date = _extract_labeled_field(text, ["就诊日期"])

    return RecordFields(
        name=name,
        sex=sex,
        age=age,
        chief_complaint=chief,
        present_illness=present,
        past_history=past_history,
        allergy_history=allergy,
        oral_exam=oral_exam,
        auxiliary_exam=auxiliary_exam,
        diagnosis=diagnosis,
        treatment=treatment,
        advice=advice,
        visit_date=visit_date,
    )
