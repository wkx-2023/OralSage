from __future__ import annotations

from backend.services.knowledge_search_format import format_knowledge_chunk
from backend.services.retrieval.models import EvidenceChunk


def _chunk(text: str, heading_path: list[str] | None = None) -> EvidenceChunk:
    return EvidenceChunk(
        id="chunk:format-test",
        text=text,
        source="internal",
        source_type="textbook",
        title="《口腔组织病理学》高岩、人卫十三五-第8版",
        metadata={
            "subject": "《口腔组织病理学》高岩、人卫十三五-第8版",
            "chapter": "第十三章 口腔黏膜病",
            "heading_path": heading_path or ["第十三章 口腔黏膜病", "六、扁平苔藓", "【临床表现】"],
        },
    )


def test_merges_ocr_soft_wraps_without_correcting_source_words() -> None:
    chunk = _chunk(
        """第十三章 口腔黏膜病
第十三章 口腔黏膜病
【临床表现】

口腔病变发生率约0.1%～

2.2%，最常见于舌

侧缘。聘部较少受累。"""
    )

    result = format_knowledge_chunk(chunk, "口腔扁平苔藓有哪些临床表现")

    assert "0.1%～2.2%" in result["detail"]
    assert "舌侧缘" in result["detail"]
    assert "聘部" in result["detail"]
    assert result["detail"].count("第十三章 口腔黏膜病") <= 1
    assert result["title"] == "扁平苔藓 · 临床表现"


def test_selected_detail_stops_before_the_next_same_level_section() -> None:
    chunk = _chunk(
        """六、扁平苔藓

【临床表现】

口腔黏膜病变常为双侧对称分布，最常见于颊黏膜。网状型呈交错的白色网纹，糜烂型患者常有疼痛。

【病理变化】

基底细胞液化变性，固有层可见淋巴细胞带状浸润。"""
    )

    result = format_knowledge_chunk(chunk, "口腔扁平苔藓的临床表现")

    assert "双侧对称分布" in result["detail"]
    assert "病理变化" not in result["detail"]
    assert "基底细胞液化变性" not in result["detail"]


def test_numbered_items_are_exposed_as_a_safe_list_block() -> None:
    chunk = _chunk(
        """牙周炎

【治疗】

非手术治疗包括以下措施：

1. 口腔卫生指导。

2. 洁治、刮治和根面平整

3. 必要时进行咬合调整

【预后】

规范治疗后炎症通常可以得到控制。""",
        ["牙周炎", "【治疗】"],
    )

    result = format_knowledge_chunk(chunk, "牙周炎如何治疗")

    list_blocks = [block for block in result["blocks"] if block["type"] == "list"]
    assert list_blocks == [
        {
            "type": "list",
            "items": ["口腔卫生指导。", "洁治、刮治和根面平整", "必要时进行咬合调整"],
        }
    ]
    assert "预后" not in result["detail"]
    assert all(set(block) in ({"type", "text"}, {"type", "items"}) for block in result["blocks"])


def test_captions_attributions_and_objectives_do_not_enter_preferred_window() -> None:
    chunk = _chunk(
        """掌握：口腔扁平苔藓的定义、临床表现、诊断与鉴别诊断。

【临床表现】

网状型呈交错的白色网纹，多见于双侧颊黏膜。糜烂型患者常因疼痛就诊。

该图像显示患者颊黏膜上的白色网纹。

Image provided by Example Author, MD.

患者还可有黏膜粗糙感和牵张感。"""
    )

    result = format_knowledge_chunk(chunk, "扁平苔藓临床表现")

    assert "网状型" in result["detail"]
    assert "掌握：" not in result["detail"]
    assert "该图像显示" not in result["detail"]
    assert "Image provided" not in result["detail"]


def test_broken_table_text_is_not_guessed_into_a_list_or_rebuilt_table() -> None:
    chunk = _chunk(
        """【鉴别诊断】

表10-1龈炎和早期牙周炎的区别

牙周袋附着丧失牙槽骨吸收治疗结果

龈炎假性牙周袋无恰当治疗后恢复正常

鉴别时应结合附着丧失和牙槽骨吸收进行判断。""",
        ["牙周炎", "【鉴别诊断】"],
    )

    result = format_knowledge_chunk(chunk, "如何鉴别龈炎和早期牙周炎")

    assert "鉴别时应结合附着丧失和牙槽骨吸收进行判断。" in result["detail"]
    assert "牙周袋附着丧失牙槽骨吸收治疗结果" not in result["detail"]
    assert not any(block["type"] == "list" for block in result["blocks"])


def test_short_coherent_section_is_not_padded_to_minimum_length() -> None:
    chunk = _chunk(
        """【诊断】

诊断主要依据典型临床表现，必要时结合组织病理检查。

【治疗】

治疗应根据病损类型制定。""",
        ["扁平苔藓", "【诊断】"],
    )

    result = format_knowledge_chunk(chunk, "扁平苔藓如何诊断")

    assert result["detail"].endswith("组织病理检查。")
    assert len(result["detail"]) < 250
    assert "治疗应根据" not in result["detail"]


def test_pdf_metadata_and_adjacent_duplicate_headings_are_removed_conservatively() -> None:
    chunk = _chunk(
        """（已压缩）《口腔组织病理学》高岩、人卫十三五-第8版(OCR).pdf
第十三章 口腔黏膜病
第十三章口腔黏膜病
【临床表现】
【临床表现】
病损常呈双侧对称分布。"""
    )

    result = format_knowledge_chunk(chunk, "扁平苔藓临床表现")

    assert ".pdf" not in result["detail"]
    assert "OCR" not in result["detail"]
    assert "【临床表现】" not in result["detail"]
    assert all(block.get("text") != "临床表现" for block in result["blocks"])
    assert result["subject"] == "口腔组织病理学"


def test_pipe_separated_internal_path_becomes_a_short_reading_title() -> None:
    chunk = _chunk(
        "口腔扁平苔藓常呈珠光白色网状条纹，部分患者伴有烧灼感。",
        ["第六章 口腔斑纹类疾病", "【临床表现】"],
    )
    chunk.title = "口腔黏膜病学｜第六章 口腔斑纹类疾病｜【临床表现】"

    result = format_knowledge_chunk(chunk, "白色网状条纹伴烧灼感")

    assert result["title"] == "口腔斑纹类疾病 · 临床表现"
    assert "｜" not in result["title"]


def test_sentence_like_heading_is_reduced_to_its_short_prefix() -> None:
    long_heading = "电测法：根尖定位仪是临床上根管治疗最常用的必备仪器。"
    chunk = _chunk(
        f"{long_heading}\n\n电测法可使用根尖定位仪辅助确定根管工作长度。",
        [
            "根管工作长度测量",
            long_heading,
        ],
    )

    result = format_knowledge_chunk(chunk, "根管治疗如何确定工作长度")

    assert result["title"] == "电测法"
    assert len(result["title"]) <= 24


def test_heading_repeated_by_the_result_title_is_not_rendered_again() -> None:
    chunk = _chunk(
        """六、扁平苔藓

【临床表现】

口腔病变常呈双侧对称分布，最常见于颊黏膜。"""
    )

    result = format_knowledge_chunk(chunk, "扁平苔藓临床表现")

    assert result["title"] == "扁平苔藓 · 临床表现"
    assert [block["type"] for block in result["blocks"]] == ["paragraph"]
    assert result["detail"].startswith("口腔病变常呈双侧对称分布")
