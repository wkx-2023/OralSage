from __future__ import annotations

from dataclasses import dataclass
import re


MAX_MONTH_WINDOW = 120


TERM_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("牙周炎", "牙周病", "牙周"), ("periodontitis", "periodontal disease")),
    (("龋病", "龋齿", "蛀牙"), ("dental caries", "tooth decay")),
    (("根尖周炎", "根尖周病变", "根尖病变"), ("apical periodontitis", "periapical lesion")),
    (("阻生牙", "阻生齿", "智齿", "第三磨牙"), ("impacted tooth", "impacted third molar")),
    (("种植体周围炎", "种植周围炎"), ("peri-implantitis",)),
    (("口腔鳞癌", "鳞状细胞癌", "口腔癌"), ("oral squamous cell carcinoma", "oral cancer")),
    (("口腔黏膜病", "口腔粘膜病"), ("oral mucosal disease",)),
    (("正畸", "矫治", "矫正"), ("orthodontics",)),
    (("牙髓炎",), ("pulpitis",)),
    (("牙髓",), ("dental pulp",)),
    (("拔牙", "拔除"), ("tooth extraction",)),
    (("下颌骨",), ("mandible",)),
    (("颞下颌关节", "关节紊乱"), ("temporomandibular joint disorder",)),
    (("唇腭裂", "腭裂", "唇裂"), ("cleft lip and palate",)),
    (("牙槽骨", "骨丧失"), ("alveolar bone loss",)),
    (("囊肿",), ("cyst",)),
    (("根管",), ("root canal treatment",)),
    (("种植",), ("dental implant",)),
)


CONCEPT_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("治疗效果", "疗效", "效果", "结局", "预后"), ("treatment outcomes", "clinical efficacy")),
    (("并发症", "风险", "不良反应"), ("complications", "risk factors")),
    (("疼痛", "镇痛"), ("postoperative pain", "pain management")),
    (("感染",), ("infection",)),
    (("诊断", "识别", "检测"), ("diagnosis", "detection")),
    (("影像", "全景片", "CBCT", "X线", "x线"), ("radiography", "cone-beam computed tomography")),
    (("手术", "术式"), ("surgery", "surgical procedure")),
    (("指南", "共识"), ("guideline", "consensus")),
    (("机制", "发病机制", "病因"), ("pathogenesis", "etiology")),
    (("管理", "处理"), ("management",)),
)


TIME_RULES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("最近一个月", "近一个月", "近1个月", "近 1 个月", "1个月", "一月内"), 1),
    (("最近三个月", "近三个月", "近3个月", "近 3 个月", "三个月", "近季"), 3),
    (("最近半年", "近半年", "近6个月", "近 6 个月", "半年", "6个月"), 6),
    (("最近一年", "近一年", "近12个月", "近 12 个月", "一年", "12个月"), 12),
    (("最近两年", "近两年", "近24个月", "近 24 个月", "两年", "2年"), 24),
    (("最近三年", "近三年", "近36个月", "近 36 个月", "三年", "3年"), 36),
    (("最近五年", "近五年", "近60个月", "近 60 个月", "五年", "5年", "近几年", "多年"), 60),
)


@dataclass(frozen=True)
class TermsResult:
    english_terms: list[str]
    recommended_keywords: str
    recommended_months: int
    source: str = "rules"

    def to_dict(self) -> dict[str, object]:
        return {
            "english_terms": self.english_terms,
            "recommended_keywords": self.recommended_keywords,
            "recommended_months": self.recommended_months,
            "source": self.source,
        }


def infer_months(task: str, explicit_months: int | None = None) -> int:
    if explicit_months is not None:
        return max(1, min(int(explicit_months), MAX_MONTH_WINDOW))

    normalized = re.sub(r"\s+", "", task)
    for patterns, months in TIME_RULES:
        if any(pattern in normalized for pattern in patterns):
            return months
    return MAX_MONTH_WINDOW


def _match_terms(task: str, rules: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]) -> list[str]:
    matched: list[str] = []
    lowered = task.lower()
    for triggers, terms in rules:
        if any(trigger.lower() in lowered for trigger in triggers):
            for term in terms:
                if term not in matched:
                    matched.append(term)
    return matched


def _extract_english_query(task: str) -> str | None:
    ascii_chars = re.findall(r"[A-Za-z0-9][A-Za-z0-9\s\"'()/+-]{2,}", task)
    if not ascii_chars:
        return None
    query = " ".join(part.strip() for part in ascii_chars if part.strip())
    query = re.sub(r"\s+", " ", query).strip()
    return query or None


def _quote_group(terms: list[str]) -> str:
    if not terms:
        return ""
    return "(" + " OR ".join(f'"{term}"' for term in terms) + ")"


def resolve_terms(task: str, explicit_months: int | None = None) -> TermsResult:
    task = task.strip()
    if not task:
        return TermsResult([], "", infer_months(task, explicit_months))

    disease_terms = _match_terms(task, TERM_RULES)
    concept_terms = _match_terms(task, CONCEPT_RULES)

    groups: list[str] = []
    if disease_terms:
        groups.append(_quote_group(disease_terms[:3]))
    if concept_terms:
        groups.append(_quote_group(concept_terms[:3]))

    english_terms = list(dict.fromkeys(disease_terms + concept_terms))
    english_query = _extract_english_query(task)

    if groups:
        recommended_keywords = " AND ".join(groups)
    elif english_query:
        recommended_keywords = english_query
        english_terms = [english_query]
    else:
        # Keep the pipeline executable for unseen Chinese topics. The LLM
        # summarizer can be enabled later to replace this conservative fallback.
        recommended_keywords = f'"{task}"'
        english_terms = [task]

    return TermsResult(
        english_terms=english_terms,
        recommended_keywords=recommended_keywords,
        recommended_months=infer_months(task, explicit_months),
    )
