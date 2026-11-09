# -*- coding: utf-8 -*-
"""
export_service.py —— V3.1.1 工单 ZIP 导出（纯内存，不落盘）

为什么在内存里做：
- Streamlit Cloud 的本地磁盘不持久，先写临时文件再下载不可靠；
- 工单数量少（通常 4~6 张），BytesIO + zipfile 的开销可以忽略。

ZIP 结构：
    {项目名}/
        .tasks/
            01_research_xxx.md
            02_backend_xxx.md
            ...
解压后把 `.tasks` 文件夹交给 IDE Agent 即可。
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

import project_store


class ExportError(RuntimeError):
    """项目不存在、没有工单或打包失败时抛出，UI 直接展示可读提示。"""


def _safe_name(text: str, fallback: str = "project", limit: int = 40) -> str:
    """清洗 ZIP 内的目录名/文件名，避免 Windows 解压时遇到非法字符。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(text or "").strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(".-")
    return (cleaned or fallback)[:limit]


def build_zip_from_tickets(
    tickets: list[dict],
    project_name: str = "project",
) -> tuple[bytes, str]:
    """用内存中的工单数据生成 ZIP（数据库暂不可用时的兜底）。

    入参 tickets 的每项至少包含 filename 与 content_md；
    与 build_tickets_zip 共用同一套打包规则。
    """
    if not tickets:
        raise ExportError("暂无工单可下载")

    safe_project = _safe_name(project_name)
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for ticket in tickets:
                filename = _safe_name(
                    ticket.get("filename", ""),
                    fallback="ticket.md",
                    limit=80,
                )
                if not filename.lower().endswith(".md"):
                    filename += ".md"
                # 统一放进 {项目名}/.tasks/，解压后就是一个可直接交给 IDE 的文件夹
                archive.writestr(
                    f"{safe_project}/.tasks/{filename}",
                    ticket.get("content_md", ""),
                )
    except Exception:
        # 固定提示，不把打包底层异常返回页面
        raise ExportError("打包 ZIP 失败，请稍后重试") from None

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return buffer.getvalue(), f"{safe_project}_tickets_{stamp}.zip"


def build_tickets_zip(project_id: str) -> tuple[bytes, str]:
    """从数据库拉取项目全部工单，在内存生成 ZIP。

    返回：(zip 字节内容, 建议下载文件名)
    不读本地 .tasks，确保云端部署时导出功能不依赖临时磁盘。
    """
    project = project_store.repo.get_project(project_id)
    if not project:
        raise ExportError("项目不存在或已被删除")

    tickets = project_store.repo.export_tickets(project_id)
    if not tickets:
        raise ExportError("当前项目还没有工单，无法导出")

    return build_zip_from_tickets(tickets, project.get("name", "project"))
