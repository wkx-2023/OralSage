"""Weixin text command handling for OralSage study plans."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .learning.memory import LearningMemory
from .learning.study_plan import (
    complete_study_plan_item,
    create_study_plan_item,
    defer_study_plan_item,
    get_study_plan_day,
    recommend_study_plan,
    update_study_plan_item,
)


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def today_iso() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


def tomorrow_iso() -> str:
    return (datetime.now(SHANGHAI).date() + timedelta(days=1)).isoformat()


def build_daily_plan_text(memory: LearningMemory, *, user_id: str, plan_date: str | None = None) -> str:
    target_date = normalize_date(plan_date) or today_iso()
    result = get_study_plan_day(memory, user_id=user_id, plan_date=target_date)
    items = result.get("items", [])
    if not items:
        return (
            f"{target_date} 还没有学习计划。\n\n"
            "你可以发送“添加计划 复习牙周病基础 30分钟”，我会帮你加入今天的计划。"
        )
    lines = [f"{target_date} 学习计划", ""]
    for index, item in enumerate(items, start=1):
        status = "已完成" if item.get("status") == "completed" else "待完成"
        notes = str(item.get("notes") or "").strip()
        lines.append(f"{index}. {item.get('title')}（{status}）")
        if notes:
            lines.append(f"   备注：{notes}")
    stats = result.get("stats", {})
    lines.extend(
        [
            "",
            f"进度：{stats.get('completed', 0)}/{stats.get('total', len(items))}",
            "可发送“打卡 1”完成第 1 项，或发送“修改 1 新标题”调整计划。",
        ]
    )
    return "\n".join(lines)


def handle_weixin_plan_message(
    memory: LearningMemory,
    *,
    user_id: str,
    text: str,
) -> dict[str, Any]:
    normalized = normalize_text(text)
    if not normalized:
        return reply("我收到了空消息。你可以发送“今日计划”“打卡 1”或“修改 1 新标题”。")

    try:
        if is_help(normalized):
            return reply(help_text())
        if is_daily_plan_request(normalized):
            return reply(build_daily_plan_text(memory, user_id=user_id, plan_date=parse_date_hint(normalized)))
        if is_recommend_request(normalized):
            return reply(handle_recommend(memory, user_id=user_id, text=normalized))
        if is_add_request(normalized):
            return reply(handle_add(memory, user_id=user_id, text=normalized))
        if is_complete_request(normalized):
            return reply(handle_complete(memory, user_id=user_id, text=normalized))
        if is_update_request(normalized):
            return reply(handle_update(memory, user_id=user_id, text=normalized))
        if is_defer_request(normalized):
            return reply(handle_defer(memory, user_id=user_id, text=normalized))
    except (LookupError, ValueError) as exc:
        return reply(str(exc))
    except Exception as exc:  # Keep Weixin conversations from failing silently.
        return reply(f"这次处理计划时遇到问题：{type(exc).__name__}: {exc}")

    return reply(
        "我现在支持每日计划、打卡和修改计划。\n\n"
        "你可以发送“今日计划”“打卡 1”“修改 1 复习根尖周炎”或“添加计划 练习口腔病理题”。"
    )


def handle_recommend(memory: LearningMemory, *, user_id: str, text: str) -> str:
    target = parse_date_hint(text) or today_iso()
    result = recommend_study_plan(memory, user_id=user_id, plan_date=target, max_items=4)
    items = result.get("recommendations", [])
    if not items:
        return f"{target} 暂时没有新的推荐计划。"
    lines = [f"{target} 推荐计划", ""]
    for index, item in enumerate(items, start=1):
        minutes = item.get("estimated_minutes")
        suffix = f"（约 {minutes} 分钟）" if minutes else ""
        lines.append(f"{index}. {item.get('title')}{suffix}")
        reason = str(item.get("reason") or "").strip()
        if reason:
            lines.append(f"   原因：{reason}")
    lines.append("")
    lines.append("如果要加入今天计划，请发送“添加计划 计划标题”。")
    return "\n".join(lines)


def handle_add(memory: LearningMemory, *, user_id: str, text: str) -> str:
    target = parse_date_hint(text) or today_iso()
    title = strip_command(text, ["添加计划", "新增计划", "加计划", "安排"])
    title = remove_date_words(title).strip(" ：:，,")
    if not title:
        raise ValueError("请告诉我要添加的计划内容，例如“添加计划 复习龋病诊断 20分钟”。")
    result = create_study_plan_item(memory, user_id=user_id, plan_date=target, title=title)
    item = result["item"]
    return f"已添加到 {target}：{item['title']}\n\n发送“今日计划”可以查看完整列表。"


def handle_complete(memory: LearningMemory, *, user_id: str, text: str) -> str:
    target = parse_date_hint(text) or today_iso()
    item = resolve_item_from_text(memory, user_id=user_id, plan_date=target, text=text, pending_only=True)
    result = complete_study_plan_item(memory, item_id=item["id"], user_id=user_id, completed=True)
    completed = result["item"]
    return f"打卡成功：{completed['title']}\n\n{build_daily_plan_text(memory, user_id=user_id, plan_date=target)}"


def handle_update(memory: LearningMemory, *, user_id: str, text: str) -> str:
    target = parse_date_hint(text) or today_iso()
    item = resolve_item_from_text(memory, user_id=user_id, plan_date=target, text=text, pending_only=False)
    new_title = extract_update_title(text)
    if not new_title:
        raise ValueError("请告诉我要改成什么，例如“修改 1 复习牙髓炎鉴别诊断”。")
    result = update_study_plan_item(
        memory,
        item_id=item["id"],
        user_id=user_id,
        updates={"title": new_title},
    )
    updated = result["item"]
    return f"已修改：{updated['title']}\n\n{build_daily_plan_text(memory, user_id=user_id, plan_date=updated['plan_date'])}"


def handle_defer(memory: LearningMemory, *, user_id: str, text: str) -> str:
    target = parse_date_hint(text) or today_iso()
    item = resolve_item_from_text(memory, user_id=user_id, plan_date=target, text=text, pending_only=False)
    to_date = parse_defer_target(text) or tomorrow_iso()
    result = defer_study_plan_item(memory, item_id=item["id"], user_id=user_id, to_date=to_date)
    updated = result["item"]
    return f"已延期到 {updated['plan_date']}：{updated['title']}"


def resolve_item_from_text(
    memory: LearningMemory,
    *,
    user_id: str,
    plan_date: str,
    text: str,
    pending_only: bool,
) -> dict[str, Any]:
    day = get_study_plan_day(memory, user_id=user_id, plan_date=plan_date)
    items = day.get("items", [])
    if pending_only:
        items = [item for item in items if item.get("status") != "completed"]
    if not items:
        raise LookupError(f"{plan_date} 没有可处理的计划项。")

    index = extract_index(text)
    if index is not None:
        if index < 1 or index > len(items):
            raise LookupError(f"没有找到第 {index} 项。发送“今日计划”可以查看当前编号。")
        return items[index - 1]

    keyword = strip_command(text, ["打卡", "完成", "已完成", "修改计划", "修改", "改计划", "延期", "推迟"])
    keyword = remove_date_words(keyword).strip(" ：:，,")
    if keyword:
        for item in items:
            if keyword in str(item.get("title") or ""):
                return item
        raise LookupError(f"没有找到包含“{keyword}”的计划项。发送“今日计划”可以查看当前列表。")

    if len(items) == 1:
        return items[0]
    raise LookupError("今天有多个计划项，请发送“打卡 1”或“修改 1 新标题”指定编号。")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def is_help(text: str) -> bool:
    return text in {"帮助", "help", "说明", "怎么用"}


def is_daily_plan_request(text: str) -> bool:
    return any(word in text for word in ["今日计划", "今天计划", "每日计划", "查看计划", "我的计划", "计划列表"])


def is_recommend_request(text: str) -> bool:
    return any(word in text for word in ["推荐计划", "生成计划", "计划推荐"])


def is_add_request(text: str) -> bool:
    return text.startswith(("添加计划", "新增计划", "加计划", "安排"))


def is_complete_request(text: str) -> bool:
    return text.startswith(("打卡", "完成", "已完成"))


def is_update_request(text: str) -> bool:
    return text.startswith(("修改计划", "修改", "改计划"))


def is_defer_request(text: str) -> bool:
    return text.startswith(("延期", "推迟"))


def strip_command(text: str, commands: list[str]) -> str:
    for command in commands:
        if text.startswith(command):
            return text[len(command) :].strip()
    return text.strip()


def extract_index(text: str) -> int | None:
    match = re.search(r"(?:第\s*)?(\d{1,2})\s*(?:项|个|条)?", text)
    if not match:
        return None
    return int(match.group(1))


def extract_update_title(text: str) -> str:
    cleaned = strip_command(text, ["修改计划", "修改", "改计划"])
    cleaned = remove_date_words(cleaned)
    cleaned = re.sub(r"^(?:第\s*)?\d{1,2}\s*(?:项|个|条)?", "", cleaned).strip()
    cleaned = re.sub(r"^(?:为|成|改成|改为|：|:)", "", cleaned).strip()
    return cleaned


def parse_date_hint(text: str) -> str | None:
    if "明天" in text:
        return tomorrow_iso()
    if "今天" in text or "今日" in text:
        return today_iso()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day).isoformat()
    match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if match:
        month, day = (int(part) for part in match.groups())
        return date(datetime.now(SHANGHAI).year, month, day).isoformat()
    return None


def parse_defer_target(text: str) -> str | None:
    if "后天" in text:
        return (datetime.now(SHANGHAI).date() + timedelta(days=2)).isoformat()
    if "明天" in text:
        return tomorrow_iso()
    match = re.search(r"(?:到|至)\s*(.+)$", text)
    if match:
        return parse_date_hint(match.group(1))
    return parse_date_hint(text)


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    return date.fromisoformat(str(value).strip()[:10]).isoformat()


def remove_date_words(text: str) -> str:
    result = re.sub(r"(今天|今日|明天|后天)", "", text)
    result = re.sub(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}[日号]?", "", result)
    result = re.sub(r"\d{1,2}月\d{1,2}[日号]?", "", result)
    return result


def reply(text: str) -> dict[str, Any]:
    return {
        "reply_text": text,
        "reply_attachments": [],
        "reply_actions": [],
    }


def help_text() -> str:
    return (
        "OralSage 微信计划助手支持这些指令：\n\n"
        "今日计划：查看今天的计划\n"
        "推荐计划：生成今天的学习建议\n"
        "添加计划 复习牙周病基础：新增计划\n"
        "打卡 1：完成第 1 项\n"
        "修改 1 复习牙髓炎鉴别诊断：修改第 1 项\n"
        "延期 1 到明天：把第 1 项延期"
    )
