# -*- coding: utf-8 -*-
"""
schemas.py —— 数据结构与 LLM 输出校验

为什么单独一层：
- 大模型输出是不可信输入，可能缺字段、类型错、模块数量不对；
- UI 与状态机只接收校验通过的对象，避免脏数据污染对话流与工单文件；
- 校验失败统一抛 SchemaError，由 UI 转成「重试」提示，不静默兜底。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config


class SchemaError(ValueError):
    """LLM 输出不符合约定结构时抛出。"""


@dataclass
class Question:
    """一次追问的结果：只允许一个问题。"""

    need_more_info: bool
    question: str
    reason: str = ""
    options: list[str] = field(default_factory=list)
    auto_trimmed: bool = False  # 模型原本给了多个问句，已被代码收敛为第一个


@dataclass
class Module:
    """大纲中的一个模块。"""

    name: str
    goal: str
    kind: str = "backend"
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Outline:
    """确认过的大纲：3~4 个模块 + 可选的前置调研。"""

    project_name: str
    summary: str
    modules: list[Module]
    research_needed: bool = False
    research_goal: str = ""


VALID_KINDS: tuple[str, ...] = ("frontend", "backend", "doc", "test", "research")


def _text(data: dict, key: str, limit: int = 2000, required: bool = True) -> str:
    """安全取字符串：兜底类型错误、去除首尾空白、截断超长内容。"""
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SchemaError(f"字段 {key} 必须是字符串，实际是 {type(value).__name__}")
    value = value.strip()
    if required and not value:
        raise SchemaError(f"字段 {key} 不能为空")
    return value[:limit]


def _string_list(data: dict, key: str, limit: int = 8) -> list[str]:
    """安全取字符串列表：兼容模型给出单个字符串或 null 的情况。"""
    value = data.get(key, [])
    if isinstance(value, str):
        return [value.strip()[:200]] if value.strip() else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            result.append(text[:300])
    return result


def _normalize_single_question(text: str) -> tuple[str, bool]:
    """把模型可能给出的一段多问句文本收敛成「只保留第一个问题」。

    背景：模型经常把补充说明写成第二个问句（如「核心用户是谁？他们怎么用？」）。
    直接抛错会让产品经理反复重试，体验很差；这里按中英文问号切分，
    保留第一个完整问句，其余丢弃，并返回「是否发生过裁剪」。
    """
    text = (text or "").strip()
    if not text:
        return "", False

    marks = [index for index, char in enumerate(text) if char in "？?"]
    if len(marks) <= 1:
        return text, False

    first = marks[0]
    trimmed = text[: first + 1].strip()
    return (trimmed or text), True


def parse_question(data: dict) -> Question:
    """校验追问输出：一次只保留一个问题，不因模型多问而报错。

    兜底策略：
    - 多个问号 -> 自动截取第一个问句，标记 auto_trimmed；
    - 模型说需要追问却没给问题 -> 视为信息已足够，避免页面报错。
    """
    if not isinstance(data, dict):
        raise SchemaError("追问结果必须是 JSON 对象")

    need = bool(data.get("need_more_info", False))
    raw_question = _text(data, "question", limit=300, required=False)
    question, auto_trimmed = _normalize_single_question(raw_question)

    if need and not question:
        need = False  # 没给出问题就视为信息足够，不让产品经理看到技术性错误

    return Question(
        need_more_info=need,
        question=question,
        reason=_text(data, "reason", limit=200, required=False),
        options=_string_list(data, "options", limit=3),
        auto_trimmed=auto_trimmed,
    )


def parse_outline(data: dict) -> Outline:
    """校验大纲：模块数必须 3~4 个，类型必须在白名单内。"""
    if not isinstance(data, dict):
        raise SchemaError("大纲结果必须是 JSON 对象")

    raw_modules = data.get("modules")
    if not isinstance(raw_modules, list):
        raise SchemaError("大纲缺少 modules 列表")

    modules: list[Module] = []
    for item in raw_modules[: config.MAX_OUTLINE_MODULES]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "backend")).strip().lower()
        if kind not in VALID_KINDS:
            kind = "backend"  # 未登记类型一律回落，避免脏类型流进文件名
        modules.append(
            Module(
                name=_text(item, "name", limit=40),
                goal=_text(item, "goal", limit=300),
                kind=kind,
                depends_on=_string_list(item, "depends_on", limit=4),
            )
        )

    if len(modules) < config.MIN_OUTLINE_MODULES:
        raise SchemaError(
            f"大纲至少需要 {config.MIN_OUTLINE_MODULES} 个模块，当前只有 {len(modules)} 个，请重试"
        )

    return Outline(
        project_name=_text(data, "project_name", limit=30),
        summary=_text(data, "summary", limit=800),
        modules=modules,
        research_needed=bool(data.get("research_needed", False)),
        research_goal=_text(data, "research_goal", limit=200, required=False),
    )
