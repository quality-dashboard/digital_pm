# -*- coding: utf-8 -*-
"""
session_store.py —— 会话状态持久化（V5.1 状态恢复）

解决什么问题：
- 浏览器 F5 或 websocket 重连会让 Streamlit 丢掉内存里的 PMSession；
- 本模块在每次状态机流转时，把「需求 / 对话记录 / 当前问题 / 大纲 / 阶段」
  作为一条 state_snapshot 追加到本地 JSONL，页面重新加载时读最后一条恢复。

存放位置：
- 追问阶段（项目名未定）：D:\projects\_drafts\session.jsonl
- 大纲生成后（有项目目录）：D:\projects\{项目名}\.history\session.jsonl

设计原则：
- 只追加、不覆盖，天然保留历史，便于排查；
- 只认最后一条 state_snapshot；坏行跳过，不让页面崩；
- 恢复时只读取快照，不把整段 JSONL 喂给模型，上下文依然隔离。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import config

STATE_EVENT = "state_snapshot"


def _draft_state_path() -> Path:
    """追问阶段的草稿快照路径。"""
    return config.PROJECTS_ROOT / config.DRAFTS_DIR_NAME / config.DRAFT_STATE_FILE


def save_state(session) -> Path | None:
    """把当前会话完整快照追加到磁盘，返回写入路径；失败时返回 None。

    状态持久化是「体验增强」，不应该反过来让主流程崩溃：
    磁盘权限、路径被占用等 OSError 一律吞掉，下一次流转会继续尝试。
    """
    if getattr(session, "project_slug", ""):
        # 项目已确定：写项目自己的 .history/session.jsonl
        target = config.ensure_project_dirs(session.project_slug)["history_dir"] / config.DRAFT_STATE_FILE
    else:
        # 项目未确定：写全局草稿目录，保证追问阶段 F5 也能恢复
        target = _draft_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": STATE_EVENT,
        "state": session.to_dict(),
    }
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return target
    except OSError:
        return None


def _iter_history_files():
    """遍历所有可能的状态文件：草稿 + 每个项目的 .history。

    产出 (项目 slug 或 None, 文件路径)，供按修改时间挑选最新进度。
    """
    draft = _draft_state_path()
    if draft.exists():
        yield None, draft

    if not config.PROJECTS_ROOT.exists():
        return
    for project_dir in config.PROJECTS_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        history = project_dir / config.HISTORY_DIR_NAME / config.DRAFT_STATE_FILE
        if history.exists():
            yield project_dir.name, history


def _read_last_state(history: Path) -> dict | None:
    """从后往前找最后一条合法 state_snapshot；坏行/缺字段直接跳过。"""
    try:
        lines = history.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == STATE_EVENT and isinstance(record.get("state"), dict):
            return record["state"]
    return None


def load_latest_session() -> dict | None:
    """扫描所有项目与草稿，返回最近一次的状态快照。

    返回：{"slug": 项目目录名或 None, "state": dict, "source": 文件路径}
    没有可恢复记录时返回 None。
    """
    candidates = []
    for slug, history in _iter_history_files():
        try:
            candidates.append((history.stat().st_mtime, slug, history))
        except OSError:
            continue

    # 最近写入的文件优先：F5 后应回到最后一次操作所在的项目
    for _, slug, history in sorted(candidates, key=lambda item: item[0], reverse=True):
        state = _read_last_state(history)
        if state:
            return {"slug": slug, "state": state, "source": str(history)}
    return None


def has_restorable_session() -> bool:
    """页面启动时用于判断是否展示「已恢复」提示。"""
    return load_latest_session() is not None
