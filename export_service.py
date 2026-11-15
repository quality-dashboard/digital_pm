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


# 每个 ZIP 强制附带的「保姆级」新手指南：不是工单，但能显著降低新手放弃率
NEWBIE_GUIDE = """# 🚀 新手开发环境配置与实战指南

恭喜你拿到了系统的施工图纸！这份指南能帮你避开 99% 的新手坑。

## 第一步：安装你的“数字包工头” (IDE)
1. 推荐工具：Trae 或 CodeBuddy（在官网免费下载）。
2. 安装路径：建议默认安装。安装完成后，用微信扫码登录。

## 第二步：怎么让 AI 替你写代码？
1. 在 Trae 中新建一个空文件夹（命名为你的项目名）。
2. 打开我们为你生成的 `01_数据库设计.md`，把全文复制，粘贴到 Trae 的对话框，输入：“请根据这份文档，帮我建库建表”。
3. 依次打开 `02_后端逻辑.md` 和 `03_前端界面.md`，同样复制粘贴给 AI。等它写完，再喂下一个，千万不要一次性全丢进去。

## 第三步：遇到红字报错怎么办？（最重要）
1. 如果终端出现一堆红色的英文报错，**不要慌，也不要乱改代码**。
2. 直接选中报错的那几行红字，右键复制。
3. 回到 Trae 的对话框，粘贴报错，加上一句：“我遇到了这个报错，请帮我修复”。AI 会告诉你缺什么依赖，或者该怎么配环境。
"""


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
            # 强制附带新手指南：与 .tasks 同级，解压后第一眼就能看到
            archive.writestr(
                f"{safe_project}/必读_新手开发环境指南.md",
                NEWBIE_GUIDE,
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
