# -*- coding: utf-8 -*-
"""
ticket_writer.py —— 工单落盘（模块三 / 模块四）

职责：
1. 生成 project.md（项目级上下文）；
2. 把 LLM 生成的正文组装成标准 Markdown 工单，写入 .tasks/NN_kind_title.md；
3. 生成 fix_NN.md；
4. 追加 .history/session.jsonl 留痕。

安全与稳定：
- 所有写入都是「同目录临时文件 + os.replace」原子替换；
- 编号只增不减，重新生成不会覆盖历史工单；
- 文件名清洗 Windows 非法字符，避免建文件失败。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import config
from schemas import Outline


def _slug(text: str, limit: int = 30) -> str:
    """清洗成文件名片段：去非法字符、空白转 '-'、限制长度。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(text).strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(".-")
    return (cleaned or "ticket")[:limit]


def _atomic_write(target: Path, content: str) -> None:
    """原子写文本：先写同目录 .tmp，再 os.replace，避免读到半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def _next_number(tasks_dir: Path, pattern: str) -> int:
    """扫描现有文件，返回下一个编号（只增不减，不覆盖历史工单）。"""
    regex = re.compile(pattern)
    max_no = 0
    for path in tasks_dir.glob("*.md"):
        match = regex.match(path.name)
        if match:
            max_no = max(max_no, int(match.group(1)))
    return max_no + 1


def write_project_meta(
    project_slug: str,
    requirement: str,
    outline: Outline,
    transcript: list[dict[str, str]],
) -> Path:
    """写入 project.md：需求原文、已确认大纲、技术规则快照。

    这张文件是「项目级上下文」：后续工单与 Fix 工单只读它的摘要，
    不读取完整对话，避免上下文越滚越大。
    """
    paths = config.ensure_project_dirs(project_slug)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    module_lines = [
        "| # | 模块 | 类型 | 目标 | 前置依赖 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, module in enumerate(outline.modules, 1):
        depends = "、".join(module.depends_on) if module.depends_on else "无"
        module_lines.append(
            f"| {index} | {module.name} | {module.kind} | {module.goal} | {depends} |"
        )

    content = "\n".join(
        [
            f"# 项目：{outline.project_name}",
            "",
            f"> 生成时间：{now} ｜ 技术栈：Streamlit + 阿里云百炼 ｜ 工单目录：{paths['tasks_dir']}",
            "",
            "## 1. 原始需求",
            "",
            requirement.strip(),
            "",
            "## 2. 项目摘要",
            "",
            outline.summary,
            "",
            "## 3. 已确认大纲",
            "",
            *module_lines,
            "",
            "## 4. 前置调研",
            "",
            f"- 是否需要：{'是' if outline.research_needed else '否'}",
            f"- 调研目标：{outline.research_goal or '无'}",
            "",
            "## 5. 上下文边界（写给后续工单）",
            "",
            "- 工单只读取本文件的摘要与自身内容，不读取完整对话记录；",
            "- 跨工单只传依赖文件名，不复制其他工单全文；",
            "- 不修改 .env、密钥、凭据与工单目录之外的文件。",
            "",
        ]
    )
    _atomic_write(paths["meta_file"], content)
    append_history(
        project_slug,
        {"event": "outline_confirmed", "project": outline.project_name},
    )
    return paths["meta_file"]


def write_ticket(
    project_slug: str,
    project_name: str,
    kind: str,
    title: str,
    body_markdown: str,
    depends_on: list[str] | None = None,
) -> Path:
    """把模型生成的正文组装成标准工单并落盘，返回文件路径。

    标题与元信息由代码生成，模型只负责从「## 0」开始的正文，
    这样即使模型输出漂移，工单格式也保持稳定、可扫描、可验收。
    """
    paths = config.ensure_project_dirs(project_slug)
    tasks_dir = paths["tasks_dir"]
    number = _next_number(tasks_dir, r"^(\d+)_")
    depends_text = "、".join(depends_on or []) or "无"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    header = "\n".join(
        [
            f"# [{kind}] {title}",
            "",
            f"> 项目：{project_name} ｜ 类型：{kind} ｜ 依赖：{depends_text}",
            f"> 执行平台：{config.execution_hint(kind)} ｜ 生成时间：{now}",
            "",
        ]
    )
    content = header + body_markdown.strip() + "\n"

    filename = f"{number:0{config.TICKET_NUMBER_WIDTH}d}_{kind}_{_slug(title)}.md"
    target = tasks_dir / filename
    _atomic_write(target, content)
    append_history(
        project_slug,
        {
            "event": "ticket_written",
            "file": filename,
            "kind": kind,
            "depends_on": depends_on or [],
        },
    )
    return target


def write_fix_ticket(project_slug: str, body_markdown: str) -> Path:
    """写入 fix_NN.md；标题从正文第一行 `# ...` 提取，缺失则补默认标题。"""
    paths = config.ensure_project_dirs(project_slug)
    tasks_dir = paths["tasks_dir"]
    number = _next_number(tasks_dir, r"^fix_(\d+)\.md$")

    body = body_markdown.strip()
    if not body.startswith("#"):
        body = f"# [Fix] 待修复问题 {number:02d}\n\n{body}"

    target = tasks_dir / f"fix_{number:02d}.md"
    _atomic_write(target, body + "\n")
    append_history(project_slug, {"event": "fix_written", "file": target.name})
    return target


def append_history(project_slug: str, event: dict) -> None:
    """追加一条 .history/session.jsonl 记录，仅用于排查，不自动喂给模型。"""
    paths = config.ensure_project_dirs(project_slug)
    record = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
    with (paths["history_dir"] / "session.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
