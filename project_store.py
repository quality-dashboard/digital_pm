# -*- coding: utf-8 -*-
"""
project_store.py —— 项目与工单的只读扫描（模块五）

状态不存数据库：.tasks 目录里有什么文件，看板就展示什么。
本模块只读不写（写入统一走 ticket_writer），保证职责单一。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import config
import db


def list_projects() -> list[dict]:
    """扫描项目根目录，返回所有包含 .tasks 的项目，按最近更新时间倒序。

    返回字段：slug（目录名）、name（project.md 里的项目名）、
    ticket_count（工单数）、updated_at（最近修改时间）。
    """
    if not config.PROJECTS_ROOT.exists():
        return []

    items: list[dict] = []
    for project_dir in config.PROJECTS_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        tasks_dir = project_dir / config.TASKS_DIR_NAME
        if not tasks_dir.exists():
            continue
        tickets = list(tasks_dir.glob("*.md"))
        latest = max(
            (path.stat().st_mtime for path in tickets),
            default=project_dir.stat().st_mtime,
        )
        items.append(
            {
                "slug": project_dir.name,
                "name": _read_project_name(project_dir),
                "ticket_count": len(tickets),
                "updated_at": datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)


def _read_project_name(project_dir: Path) -> str:
    """从 project.md 第一行 `# 项目：xxx` 提取项目名，失败则回落目录名。"""
    meta = project_dir / config.PROJECT_META_FILE
    if not meta.exists():
        return project_dir.name
    try:
        first_line = meta.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        return first_line.replace("# 项目：", "").strip() or project_dir.name
    except Exception:
        return project_dir.name


def scan_tickets(project_slug: str) -> list[dict]:
    """扫描 .tasks/*.md，返回看板所需的元信息（不把全文喂给模型）。

    文件名即状态：01_research_x.md / 02_backend_x.md / fix_01.md。
    """
    tasks_dir = config.project_paths(project_slug)["tasks_dir"]
    if not tasks_dir.exists():
        return []

    items: list[dict] = []
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "path": path,
                "kind": _kind_from_name(path.name),
                "title": _first_heading(text) or path.stem,
                "depends_on": _parse_depends(text),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size": stat.st_size,
            }
        )
    return items


def _first_heading(text: str) -> str:
    """取第一行一级标题作为工单标题。"""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:60]
    return ""


def _kind_from_name(filename: str) -> str:
    """从文件名判断工单类型：fix_01.md / 01_research_x.md / 02_backend_x.md。"""
    if filename.startswith("fix_"):
        return "fix"
    match = re.match(r"^\d+_([a-z]+)_", filename)
    return match.group(1) if match else "unknown"


def _parse_depends(text: str) -> str:
    """从元信息行 `> 项目：… ｜ 依赖：xxx` 提取依赖，仅用于看板展示。"""
    for line in text.splitlines():
        if line.startswith(">") and "依赖：" in line:
            return line.split("依赖：", 1)[1].split("｜")[0].strip()
    return "无"


def read_project_meta(project_slug: str, limit: int = 4000) -> str:
    """读取 project.md 摘要（截断），供 Fix 工单拼上下文。"""
    meta = config.project_paths(project_slug)["meta_file"]
    if not meta.exists():
        return "（项目摘要尚未生成）"
    return meta.read_text(encoding="utf-8", errors="replace")[:limit]


def fix_context(project_slug: str) -> str:
    """组装 Fix 工单上下文：项目摘要 + 工单文件名（不读工单全文）。"""
    tickets = scan_tickets(project_slug)
    ticket_lines = "\n".join(
        f"- {item['name']}（{item['title']}）" for item in tickets
    ) or "- 暂无工单"
    return "\n".join(
        [
            "===== 项目摘要（节选）=====",
            read_project_meta(project_slug),
            "",
            "===== 现有工单（只给文件名，不读全文）=====",
            ticket_lines,
        ]
    )


def ticket_count(project_slug: str) -> int:
    """当前项目工单总数（含 fix 工单），用于侧边栏展示。"""
    return len(scan_tickets(project_slug))


def main_ticket_count(project_slug: str) -> int:
    """主工单数量：排除 fix_*.md。

    用途：判断「是否已经生成过工单」，用于防止重复生成。
    fix 工单是报错回流产物，不应该阻止主工单生成，因此单独统计。
    """
    tasks_dir = config.project_paths(project_slug)["tasks_dir"]
    if not tasks_dir.exists():
        return 0
    return sum(1 for path in tasks_dir.glob("*.md") if not path.name.startswith("fix_"))


# =============================================================================
# V3.1.1 云端数据访问：ProjectRepository
# =============================================================================
# 说明：
# - 上面保留 V3.0 的本地文件扫描函数，供旧页面/本地调试使用，不破坏现有逻辑；
# - 下面的 Repository 走 db.py（Turso 或本地 SQLite），新流程与页面应优先使用；
# - 所有查询按 session_id 隔离访客，避免 A 用户看到 B 用户的项目。


def outline_to_json(outline) -> str:
    """把 Outline dataclass 转成可入库的 JSON 字符串；None 返回空串。"""
    if outline is None:
        return ""
    return json.dumps(asdict(outline), ensure_ascii=False)


class ProjectRepository:
    """项目与工单的数据库仓库（V3.1.1 的云端事实源）。"""

    def __init__(self, database=None) -> None:
        # 默认使用 db.py 的模块级单例连接，测试时可注入替身
        self._db = database or db.db

    @staticmethod
    def _now() -> str:
        """统一 UTC 时间字符串，跨时区排序稳定。"""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    def create_project(
        self,
        session_id: str,
        name: str,
        requirement: str,
        outline_json: str = "",
        phase: str = "clarify",
    ) -> dict:
        """新建项目并返回完整记录；id 由服务端生成，前端不可指定。"""
        if not session_id:
            raise ValueError("session_id 不能为空")

        project_id = uuid.uuid4().hex
        created_at = self._now()
        self._db.execute(
            """
            INSERT INTO projects (id, session_id, name, requirement, outline_json, phase, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, session_id, name, requirement, outline_json, phase, created_at),
        )
        return {
            "id": project_id,
            "session_id": session_id,
            "name": name,
            "requirement": requirement,
            "outline_json": outline_json,
            "phase": phase,
            "created_at": created_at,
        }

    def get_project(self, project_id: str) -> dict | None:
        """按 id 读取项目；不存在返回 None。"""
        rows = self._db.query(
            "SELECT id, session_id, name, requirement, outline_json, phase, created_at "
            "FROM projects WHERE id = ?",
            (project_id,),
        )
        return rows[0] if rows else None

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        outline_json: str | None = None,
        phase: str | None = None,
    ) -> None:
        """按需更新项目字段；只更新显式传入的列，避免误覆盖。"""
        fields: list[str] = []
        params: list = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if outline_json is not None:
            fields.append("outline_json = ?")
            params.append(outline_json)
        if phase is not None:
            fields.append("phase = ?")
            params.append(phase)
        if not fields:
            return
        params.append(project_id)
        # 字段名来自上面的固定常量，值全部参数绑定，不存在 SQL 注入
        self._db.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )

    def list_projects(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        """按访客列出项目，按创建时间倒序。

        session_id 传 None 时返回全部（仅限本地调试/管理员，公开页面必须传）。
        """
        sql = (
            "SELECT id, session_id, name, requirement, outline_json, phase, created_at "
            "FROM projects"
        )
        params: list = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        return self._db.query(sql, tuple(params))

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------
    def add_ticket(
        self,
        project_id: str,
        ticket_type: str,
        filename: str,
        content_md: str,
    ) -> dict:
        """写入一张工单；同一项目同文件名重复写入时覆盖内容（便于重跑）。"""
        ticket_id = uuid.uuid4().hex
        created_at = self._now()
        self._db.execute(
            """
            INSERT INTO tickets (id, project_id, type, filename, content_md, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (project_id, filename) DO UPDATE SET
                type = excluded.type,
                content_md = excluded.content_md,
                created_at = excluded.created_at
            """,
            (ticket_id, project_id, ticket_type, filename, content_md, created_at),
        )
        return {
            "id": ticket_id,
            "project_id": project_id,
            "type": ticket_type,
            "filename": filename,
            "content_md": content_md,
            "created_at": created_at,
        }

    def list_tickets(self, project_id: str) -> list[dict]:
        """按文件名排序返回项目全部工单（research / 模块 / fix 混排）。"""
        return self._db.query(
            "SELECT id, project_id, type, filename, content_md, created_at "
            "FROM tickets WHERE project_id = ? ORDER BY filename ASC",
            (project_id,),
        )

    def ticket_count(self, project_id: str, exclude_fix: bool = False) -> int:
        """统计项目工单数量；exclude_fix=True 用于「是否已生成主工单」的幂等判断。"""
        sql = "SELECT COUNT(*) AS total FROM tickets WHERE project_id = ?"
        if exclude_fix:
            sql += " AND filename NOT LIKE 'fix_%'"
        rows = self._db.query(sql, (project_id,))
        return int(rows[0]["total"]) if rows else 0

    def export_tickets(self, project_id: str) -> list[dict]:
        """导出用：只返回 filename 与 content_md，按文件名排序。

        ZIP 将在内存中生成，不依赖 Streamlit Cloud 的临时磁盘。
        """
        return self._db.query(
            "SELECT filename, content_md FROM tickets "
            "WHERE project_id = ? ORDER BY filename ASC",
            (project_id,),
        )


# 模块级单例：UI 直接 project_store.repo.list_projects(...) 即可
repo = ProjectRepository()
