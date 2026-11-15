# -*- coding: utf-8 -*-
"""
decompose_logic.py —— 三阶段状态机（模块二）+ 报错回流（模块四）

阶段流转（代码强制，不靠提示词自觉）：
    CLARIFY（一次一问，必须回答）
      -> OUTLINE（生成 3~4 模块大纲，必须确认）
      -> CONFIRMED（解锁工单生成）
      -> TICKETS（工单落盘，看板扫描展示）

上下文策略：
- 追问阶段：只带需求 + 已有回答 + 已问问题（最多 3 轮，量很小）；
- 工单阶段：每个模块单独调用，只带项目摘要 + 当前模块 + 依赖文件名，
  绝不把其他模块的工单全文塞进去。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import config
import llm_client
import project_store
import prompts
import session_store
import ticket_writer
from schemas import Module, Outline, Question, parse_outline, parse_question


class LogicError(RuntimeError):
    """阶段顺序不合法、缺少前置条件时抛出，UI 直接展示给产品经理。"""


@dataclass
class PMSession:
    """一次项目对话的全部状态；由 Streamlit session_state 持有。"""

    requirement: str
    project_name: str = ""
    project_slug: str = ""
    session_id: str = ""       # V3.1.1：网页访客标识，用于数据库隔离
    project_id: str = ""       # V3.1.1：数据库 projects.id，空表示尚未入库
    phase: str = config.PHASE_CLARIFY
    round: int = 0
    transcript: list[dict[str, str]] = field(default_factory=list)
    pending_question: Question | None = None
    outline: Outline | None = None
    outline_confirmed: bool = False
    generated_tickets: list[str] = field(default_factory=list)
    # V3.3.1：内存兜底数据（数据库暂存失败时用于生成 ZIP），不写入 JSON 快照
    generated_ticket_data: list[dict] = field(default_factory=list)
    db_sync_failed: bool = False

    # ------------------------------------------------------------------
    # 状态持久化（V5.1）：会话 <-> 可落盘 dict
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """序列化为可 JSON 落盘的普通 dict（dataclass 嵌套自动展开）。"""
        return {
            "requirement": self.requirement,
            "project_name": self.project_name,
            "project_slug": self.project_slug,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "phase": self.phase,
            "round": self.round,
            "transcript": self.transcript,
            "pending_question": asdict(self.pending_question) if self.pending_question else None,
            "outline": asdict(self.outline) if self.outline else None,
            "outline_confirmed": self.outline_confirmed,
            "generated_tickets": self.generated_tickets,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PMSession":
        """从磁盘快照恢复会话；所有字段走白名单，缺字段用安全默认值。"""
        # 阶段白名单校验：坏数据一律回落到「追问中」，避免 UI 状态错乱
        phase = str(data.get("phase", config.PHASE_CLARIFY))
        if phase not in {
            config.PHASE_CLARIFY,
            config.PHASE_OUTLINE,
            config.PHASE_CONFIRMED,
            config.PHASE_TICKETS,
            config.PHASE_BUG,
        }:
            phase = config.PHASE_CLARIFY

        # 待回答问题：逐字段构造，忽略历史版本可能多出来的字段
        pending_raw = data.get("pending_question")
        pending = None
        if isinstance(pending_raw, dict):
            pending = Question(
                need_more_info=bool(pending_raw.get("need_more_info", False)),
                question=str(pending_raw.get("question", "")),
                reason=str(pending_raw.get("reason", "")),
                options=[str(item) for item in pending_raw.get("options", [])],
                auto_trimmed=bool(pending_raw.get("auto_trimmed", False)),
            )

        # 大纲：模块逐项白名单构造，避免脏类型流进后续工单
        outline_raw = data.get("outline")
        outline = None
        if isinstance(outline_raw, dict):
            modules = [
                Module(
                    name=str(item.get("name", "")),
                    goal=str(item.get("goal", "")),
                    kind=str(item.get("kind", "backend")),
                    depends_on=[str(dep) for dep in item.get("depends_on", [])],
                )
                for item in outline_raw.get("modules", [])
                if isinstance(item, dict)
            ]
            outline = Outline(
                project_name=str(outline_raw.get("project_name", "")),
                summary=str(outline_raw.get("summary", "")),
                modules=modules,
                research_needed=bool(outline_raw.get("research_needed", False)),
                research_goal=str(outline_raw.get("research_goal", "")),
            )

        transcript = [
            {"role": str(item.get("role", "assistant")), "content": str(item.get("content", ""))}
            for item in data.get("transcript", [])
            if isinstance(item, dict)
        ]

        # 轮次容错：坏值不影响恢复
        try:
            round_no = int(data.get("round", 0) or 0)
        except (TypeError, ValueError):
            round_no = 0

        return cls(
            requirement=str(data.get("requirement", "")),
            project_name=str(data.get("project_name", "")),
            project_slug=str(data.get("project_slug", "")),
            session_id=str(data.get("session_id", "")),
            project_id=str(data.get("project_id", "")),
            phase=phase,
            round=round_no,
            transcript=transcript,
            pending_question=pending,
            outline=outline,
            outline_confirmed=bool(data.get("outline_confirmed", False)),
            generated_tickets=[str(item) for item in data.get("generated_tickets", [])],
        )

    # ------------------------------------------------------------------
    # V3.1.1 数据库同步：项目与工单写入 Turso / 本地 SQLite
    # ------------------------------------------------------------------
    def _ensure_project(self) -> None:
        """确保当前会话已有数据库项目；没有就创建并把 project_id 写回。

        项目名在追问阶段可能还没定，先用占位名，大纲阶段再更新。
        数据库不可用时直接抛错：云端 SaaS 下没有持久化就无法导出，不能静默继续。
        """
        if self.project_id:
            return
        if not self.session_id:
            self.session_id = config.new_session_id()
        project = project_store.repo.create_project(
            session_id=self.session_id,
            name=self.project_name or "未命名项目",
            requirement=self.requirement,
            phase=self.phase,
        )
        self.project_id = project["id"]

    def _sync_project(self) -> None:
        """把名称、大纲、阶段同步到数据库；项目尚未创建时先补建。"""
        self._ensure_project()
        project_store.repo.update_project(
            self.project_id,
            name=self.project_name or "未命名项目",
            outline_json=project_store.outline_to_json(self.outline),
            phase=self.phase,
        )

    def _persist_ticket(self, path: Path, ticket_type: str, content_md: str) -> bool:
        """把工单写入数据库；失败只做标记，不拦用户（本地/内存仍可导出）。

        返回 True 表示入库成功；False 表示已降级为本地/内存兜底。
        """
        try:
            self._ensure_project()
            project_store.repo.add_ticket(
                project_id=self.project_id,
                ticket_type=ticket_type,
                filename=path.name,
                content_md=content_md,
            )
            return True
        except Exception as exc:
            print(f"[pm] 工单入库失败（已转内存兜底）：{type(exc).__name__}: {str(exc)[:160]}")
            self.db_sync_failed = True
            return False

    @classmethod
    def from_db_project(cls, project: dict) -> "PMSession | None":
        """从数据库项目记录恢复「项目级状态」（不含对话记录）。

        公网部署时本地 session.jsonl 不可靠，且多访客共享磁盘可能串数据，
        因此恢复只使用数据库中的 requirement / outline_json / phase。
        outline_json 为空（还在追问阶段）时返回 None，让用户重新开始。
        """
        outline_raw = project.get("outline_json") or ""
        if not outline_raw:
            return None
        try:
            data = json.loads(outline_raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None

        modules = [
            Module(
                name=str(item.get("name", "")),
                goal=str(item.get("goal", "")),
                kind=str(item.get("kind", "backend")),
                depends_on=[str(dep) for dep in item.get("depends_on", [])],
            )
            for item in data.get("modules", [])
            if isinstance(item, dict)
        ]
        if not modules:
            return None

        outline = Outline(
            project_name=str(data.get("project_name", project.get("name", ""))),
            summary=str(data.get("summary", "")),
            modules=modules,
            research_needed=bool(data.get("research_needed", False)),
            research_goal=str(data.get("research_goal", "")),
        )
        phase = str(project.get("phase", config.PHASE_OUTLINE))
        return cls(
            requirement=str(project.get("requirement", "")),
            project_name=str(project.get("name", outline.project_name)),
            project_slug=config.slugify_project_name(str(project.get("name", outline.project_name))),
            session_id=str(project.get("session_id", "")),
            project_id=str(project.get("id", "")),
            phase=phase,
            outline=outline,
            outline_confirmed=phase in (config.PHASE_CONFIRMED, config.PHASE_TICKETS),
        )

    # ------------------------------------------------------------------
    # 追问阶段
    # ------------------------------------------------------------------
    def _qa_digest(self) -> str:
        """把需求、回答、已问问题压缩成短上下文（不传完整对话历史）。"""
        answers = [item["content"] for item in self.transcript if item["role"] == "pm"]
        asked = [item["content"] for item in self.transcript if item["role"] == "assistant"]

        lines = [f"原始需求：{self.requirement}", ""]
        if answers:
            lines.append("产品经理的回答：")
            lines.extend(f"{index}. {text}" for index, text in enumerate(answers, 1))
        else:
            lines.append("产品经理尚未回答任何问题。")
        if asked:
            lines.extend(["", "已经问过的问题（不要重复）："])
            lines.extend(f"- {text}" for text in asked)
        return "\n".join(lines)

    def _ask(self, progress: Callable[[str], None] | None = None) -> Question:
        """调用模型生成一个追问。

        注意：这里不再因为「模型多问了一个问题」而抛错。
        parse_question 会把多个问句自动收敛为第一个，并标记 auto_trimmed，
        由 UI 轻提示即可，避免产品经理被技术性错误打断。
        """
        data = llm_client.chat_json(
            prompts.ASK_PROMPT,
            self._qa_digest(),
            progress=progress,
        )
        return parse_question(data)

    def start(self, progress: Callable[[str], None] | None = None) -> Question:
        """开始第一阶段：生成第一个追问；若信息已足够则直接进入大纲阶段。"""
        self.phase = config.PHASE_CLARIFY
        self.round = 0
        self.transcript = []
        question = self._ask(progress)
        # 追问生成成功后再建项目，避免千问失败时留下空项目
        self._ensure_project()
        if not question.need_more_info or not question.question.strip():
            # 模型判断信息已足够：不制造无意义追问，直接允许生成大纲
            self.phase = config.PHASE_OUTLINE
            self.pending_question = None
            session_store.save_state(self)  # 先落盘再返回，F5 后可恢复
            return question
        self.pending_question = question
        self.transcript.append({"role": "assistant", "content": question.question})
        session_store.save_state(self)  # 追问生成后立即落盘
        return question

    def answer(
        self,
        text: str,
        progress: Callable[[str], None] | None = None,
    ) -> Question | None:
        """记录回答；返回下一个问题，或返回 None 表示信息已足够。

        严格遵守「一次一问、必须等回答」：只有调用本方法后才会产生下一轮。
        """
        text = (text or "").strip()
        if not text:
            raise LogicError("回答不能为空")
        if self.pending_question is None:
            raise LogicError("当前没有待回答的问题")

        self.transcript.append({"role": "pm", "content": text})
        self.pending_question = None
        self.round += 1

        # 达到追问轮数上限，不再纠缠，直接允许生成大纲
        if self.round >= config.MAX_CLARIFY_ROUNDS:
            self.phase = config.PHASE_OUTLINE
            session_store.save_state(self)
            return None

        question = self._ask(progress)
        if not question.need_more_info or not question.question.strip():
            self.phase = config.PHASE_OUTLINE
            session_store.save_state(self)
            return None

        self.pending_question = question
        self.transcript.append({"role": "assistant", "content": question.question})
        session_store.save_state(self)
        return question

    def skip_clarify(self) -> None:
        """用户赶时间：跳过剩余追问，直接进入大纲阶段。

        只推进阶段并记录一条说明，不改动已回答的内容与轮次；
        后续 build_outline 仍使用已有需求 + 回答生成大纲。
        """
        if self.phase != config.PHASE_CLARIFY:
            return
        self.pending_question = None
        self.phase = config.PHASE_OUTLINE
        self.transcript.append(
            {"role": "assistant", "content": "已按你的要求跳过剩余追问，直接进入大纲阶段。"}
        )
        session_store.save_state(self)

    # ------------------------------------------------------------------
    # 大纲阶段
    # ------------------------------------------------------------------
    def build_outline(self, progress: Callable[[str], None] | None = None) -> Outline:
        """第二阶段：生成 3~4 个模块的大纲，等待产品经理确认。"""
        if self.pending_question is not None:
            raise LogicError("还有问题未回答，请先提交回答")
        if not self.requirement.strip():
            raise LogicError("需求为空，请先输入需求")

        data = llm_client.chat_json(
            prompts.OUTLINE_PROMPT,
            self._qa_digest(),
            progress=progress,
        )
        outline = parse_outline(data)
        self.outline = outline
        # 用户显式填了项目名就优先用用户的，否则用模型建议的名字
        self.project_name = self.project_name or outline.project_name
        self.project_slug = config.slugify_project_name(self.project_name or outline.project_name)
        self.phase = config.PHASE_OUTLINE
        # 项目名与大纲写库：公网部署时数据库才是权威事实源
        self._sync_project()
        # 本地快照继续保留，方便单机调试与同进程 F5 恢复
        session_store.save_state(self)
        return outline

    def confirm_outline(self) -> Path:
        """产品经理点击「确认大纲无误」：写 project.md 并解锁工单生成。"""
        if self.outline is None:
            raise LogicError("还没有生成大纲")
        self.outline_confirmed = True
        self.phase = config.PHASE_CONFIRMED
        meta_path = ticket_writer.write_project_meta(
            self.project_slug, self.requirement, self.outline, self.transcript
        )
        self._sync_project()
        session_store.save_state(self)
        return meta_path

    # ------------------------------------------------------------------
    # 工单阶段
    # ------------------------------------------------------------------
    def _ticket_request(
        self,
        module: Module | None,
        research: bool,
        depends_files: list[str],
    ) -> str:
        """为单个模块组装请求：只带项目摘要 + 当前模块 + 依赖文件名。

        这是上下文强隔离的关键：不带完整对话、不带其他模块的工单正文。
        """
        assert self.outline is not None
        lines = [
            f"项目：{self.outline.project_name}",
            f"项目摘要：{self.outline.summary}",
            "",
        ]

        if research:
            lines.extend(
                [
                    "本工单类型：前置调研（research）",
                    f"调研目标：{self.outline.research_goal or '围绕需求做必要的竞品/平台规则调研'}",
                    "输出要求：给出结论、可复用的事实清单、对后续模块的输入建议；不要写代码。",
                ]
            )
        else:
            assert module is not None
            lines.extend(
                [
                    f"本工单模块：{module.name}",
                    f"模块目标：{module.goal}",
                    f"模块类型：{module.kind}",
                    "项目全貌（只用于理解边界，不要越界实现其他模块）：",
                    *[f"- {item.name}：{item.goal}" for item in self.outline.modules],
                ]
            )

        lines.extend(
            [
                "",
                f"前置依赖文件（只读结论，不复制全文）：{'、'.join(depends_files) or '无'}",
                "",
                "请按系统规则输出工单正文，从「## 0. 上下文边界」开始。",
            ]
        )
        return "\n".join(lines)

    def generate_prd(self, progress: Callable[[str], None] | None = None) -> Path:
        """V4.0：生成一份大白话产品需求说明书（PRD）。

        - 不注入技术/代码规则，只输出功能清单、操作流程、验收清单；
        - 末尾由代码强制追加 WorkBuddy / 微信小程序专家团的固定提示；
        - 只写一份 `产品需求说明书.md`，不再生成多张技术工单。
        """

        def _notify(message: str) -> None:
            if progress:
                progress(message)

        if not self.outline_confirmed or self.outline is None:
            raise LogicError("请先确认大纲，再生成产品说明书")

        _notify("正在把你的需求翻译成小白能看懂的产品说明书...")
        modules_text = "\n".join(
            f"- {module.name}：{module.goal}" for module in self.outline.modules
        )
        user_content = "\n".join(
            [
                "===== 用户原始需求 =====",
                self.requirement.strip(),
                "",
                "===== 已确认的产品模块 =====",
                modules_text or "无",
                "",
                "===== 用户在追问中的补充回答 =====",
                self._qa_digest() or "无",
                "",
                "请输出完整的产品需求说明书三块内容；不要写代码，不要写数据库和 API。",
            ]
        )

        body = ""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                body = llm_client.chat_text(
                    prompts.PRD_PROMPT,
                    user_content,
                    progress=_notify,
                    system_builder=config.build_prd_system_prompt,
                )
                break
            except llm_client.QuotaExceededError:
                raise
            except llm_client.LLMError as exc:
                last_error = exc
                print(
                    f"--- 产品说明书第 {attempt + 1} 次生成失败："
                    f"{type(exc).__name__}: {str(exc)[:120]} ---"
                )
                if attempt == 0:
                    time.sleep(2)
        else:
            print(f"--- 产品说明书生成失败：{type(last_error).__name__ if last_error else 'unknown'} ---")
            raise LogicError("产品说明书生成失败，请重试") from None

        if len(body.strip()) < 50:
            raise LogicError("产品说明书内容为空，请重试")

        # 固定追加 WorkBuddy 提示，避免模型漏写
        prd = body.rstrip() + "\n\n" + config.PRD_WORKBUDDY_HINT + "\n"
        path = ticket_writer.write_prd(self.project_slug, prd)

        self.generated_ticket_data = [
            {"filename": path.name, "type": "prd", "content_md": prd}
        ]
        self.db_sync_failed = False
        try:
            self._ensure_project()
            project_store.repo.add_ticket(
                project_id=self.project_id,
                ticket_type="prd",
                filename=path.name,
                content_md=prd,
            )
        except Exception as exc:
            print(
                f"[pm] 产品说明书入库失败（已转内存兜底）："
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            self.db_sync_failed = True

        self.phase = config.PHASE_TICKETS
        try:
            self._sync_project()
        except Exception as exc:
            print(f"[pm] 项目状态同步失败（已标记）：{type(exc).__name__}: {str(exc)[:160]}")
            self.db_sync_failed = True
        session_store.save_state(self)
        _notify("产品说明书已生成")
        return path

    def generate_tickets(self, progress: Callable[[str], None] | None = None) -> list[Path]:
        """第三阶段：逐模块生成 Markdown 工单（断点续跑 + 单张重试）。

        V3.4.6 变化：
        - 已有部分工单时不再整体拦截，只补齐缺失模块，支持第二张失败后继续；
        - 每张工单失败自动重试一次（会顺带切换备用模型）；
        - 单张失败不会删除前面已成功的工单。
        """

        def _notify(message: str) -> None:
            if progress:
                progress(message)

        def _chat_with_retry(user_content: str, label: str) -> str:
            """单张工单最多尝试两次；额度耗尽立即停止，不重试。"""
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    return llm_client.chat_text(
                        prompts.TICKET_PROMPT,
                        user_content,
                        progress=_notify,
                    )
                except llm_client.QuotaExceededError:
                    raise
                except llm_client.LLMError as exc:
                    last_error = exc
                    print(
                        f"--- {label} 第 {attempt + 1} 次生成失败："
                        f"{type(exc).__name__}: {str(exc)[:120]} ---"
                    )
                    if attempt == 0:
                        time.sleep(2)
            raise LogicError(f"{label} 生成失败，请重试") from None

        if not self.outline_confirmed or self.outline is None:
            raise LogicError("请先确认大纲，再生成工单")

        paths_info = config.ensure_project_dirs(self.project_slug)
        tasks_dir = paths_info["tasks_dir"]

        # 1) 断点续跑：扫描已有工单，按模块名匹配，只补齐缺失部分
        existing_files = [
            item for item in tasks_dir.glob("*.md") if not item.name.startswith("fix_")
        ]
        existing_map: dict[str, str] = {}
        for item in existing_files:
            if config.RESEARCH_PREFIX in item.name:
                existing_map[config.RESEARCH_PREFIX] = item.name
                continue
            for module in self.outline.modules:
                if ticket_writer._slug(module.name) in item.name:
                    existing_map[module.name] = item.name
                    break

        pending: list[tuple[str, Module | None]] = []
        if self.outline.research_needed and config.RESEARCH_PREFIX not in existing_map:
            pending.append((config.RESEARCH_PREFIX, None))
        for module in self.outline.modules:
            if module.name not in existing_map:
                pending.append((module.kind, module))

        if not pending:
            raise LogicError(
                f"工单已全部生成（{len(existing_map)} 张），无需重复生成。"
            )

        # 2) 生成锁：双击时只有一个请求能拿到锁
        lock_path = tasks_dir / ".generating.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                age = 0
            if age <= 600:
                raise LogicError("工单正在生成中，请勿重复点击")
            lock_path.unlink(missing_ok=True)  # 崩溃残留锁：清理后重试
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)

        paths: list[Path] = []
        # 已有模块 -> 文件名，供后续工单的依赖映射
        name_to_file: dict[str, str] = {
            name: filename
            for name, filename in existing_map.items()
            if name != config.RESEARCH_PREFIX
        }
        self.generated_ticket_data = []
        self.db_sync_failed = False
        total = len(pending)
        try:
            _notify(
                f"检测到已有 {len(existing_map)} 张工单，准备补齐 {total} 张..."
            )
            for index, (kind, module) in enumerate(pending, 1):
                label = "前置调研" if module is None else str(module.name)
                _notify(f"正在撰写工单 {index}/{total}（{label}）...")
                print(
                    f"--- 正在请求大模型生成第 {index}/{total} 张工单（{label}）---"
                )

                if module is None:
                    user_content = self._ticket_request(
                        module=None, research=True, depends_files=[]
                    )
                else:
                    depends_files = [
                        name_to_file[name]
                        for name in module.depends_on
                        if name in name_to_file
                    ]
                    user_content = self._ticket_request(
                        module=module,
                        research=False,
                        depends_files=depends_files,
                    )

                try:
                    body = _chat_with_retry(user_content, label)
                    print(f"--- 成功拿到内容，长度 {len(body)}，准备写文件 ---")
                    if module is None:
                        path = ticket_writer.write_ticket(
                            self.project_slug,
                            self.outline.project_name,
                            config.RESEARCH_PREFIX,
                            f"前置调研：{self.outline.research_goal or '需求调研'}",
                            body,
                            depends_on=[],
                        )
                        self.generated_ticket_data.append(
                            {
                                "filename": path.name,
                                "type": config.RESEARCH_PREFIX,
                                "content_md": body,
                            }
                        )
                        self._persist_ticket(path, config.RESEARCH_PREFIX, body)
                    else:
                        depends_files = [
                            name_to_file[name]
                            for name in module.depends_on
                            if name in name_to_file
                        ]
                        path = ticket_writer.write_ticket(
                            self.project_slug,
                            self.outline.project_name,
                            module.kind,
                            module.name,
                            body,
                            depends_on=depends_files,
                        )
                        self.generated_ticket_data.append(
                            {
                                "filename": path.name,
                                "type": module.kind,
                                "content_md": body,
                            }
                        )
                        self._persist_ticket(path, module.kind, body)
                        name_to_file[module.name] = path.name
                except ValueError as exc:
                    print(f"--- 工单写入被拦截：{exc} ---")
                    raise LogicError(
                        f"第 {index} 张（{label}）内容为空，已终止写入；"
                        "已保留前面成功的工单，可再次点击继续补齐。"
                    ) from None
                except LogicError as exc:
                    print(f"--- 第 {index} 张（{label}）失败：{exc} ---")
                    raise LogicError(
                        f"第 {index} 张（{label}）生成失败；"
                        "已保留前面成功的工单，可再次点击「继续生成缺失工单」补齐。"
                    ) from None
                except Exception as exc:
                    print(
                        f"--- 第 {index} 张（{label}）异常："
                        f"{type(exc).__name__}: {str(exc)[:160]} ---"
                    )
                    raise LogicError(
                        f"第 {index} 张（{label}）生成异常；"
                        "已保留前面成功的工单，可再次点击「继续生成缺失工单」补齐。"
                    ) from None

                paths.append(path)
                print(f"--- 已写入 {path.name} ---")

            self.generated_tickets = [str(path) for path in paths]
            self.phase = config.PHASE_TICKETS
            _notify("正在整理工单与项目状态...")
            try:
                self._sync_project()
            except Exception as exc:
                print(f"[pm] 项目状态同步失败（已标记）：{type(exc).__name__}: {str(exc)[:160]}")
                self.db_sync_failed = True
            return paths
        finally:
            # 释放锁 + 落盘阶段；落盘失败不影响工单结果，因此单独兜底
            lock_path.unlink(missing_ok=True)
            try:
                session_store.save_state(self)
            except Exception:
                pass


def generate_fix_ticket(
    project_slug: str,
    log_text: str,
    project_id: str = "",
    progress: Callable[[str], None] | None = None,
) -> Path:
    """模块四：根据报错日志 + 项目上下文生成 fix_NN.md。

    V3.1.1 变化：
    - 传入 project_id 时，上下文从数据库读取（需求 + 大纲 + 工单文件名），
      不再依赖本地 project.md，适配 Streamlit Cloud 本地磁盘不持久；
    - 生成的 fix 工单同时写入 tickets 表（type=fix），导出 ZIP 能一并带走。
    V3.3.6 变化：新增 progress 回调，UI 可看到主模型/备用模型的实时切换旁白。
    """
    log_text = (log_text or "").strip()
    if not log_text:
        raise LogicError("请先粘贴报错日志")

    tasks_dir = config.project_paths(project_slug)["tasks_dir"]
    if not tasks_dir.exists() and not project_id:
        raise LogicError("当前项目还没有工单，请先生成大纲与工单")

    if project_id:
        # 云端模式：上下文来自数据库，不读本地文件
        project = project_store.repo.get_project(project_id)
        if not project:
            raise LogicError("项目不存在或已被删除")
        tickets = project_store.repo.list_tickets(project_id)
        ticket_lines = "\n".join(
            f"- {item['filename']}（{item['type']}）" for item in tickets
        ) or "- 暂无工单"
        context = "\n".join(
            [
                "===== 项目需求 =====",
                str(project.get("requirement", ""))[:2000],
                "",
                "===== 已确认大纲 =====",
                str(project.get("outline_json", ""))[:3000],
                "",
                "===== 现有工单（只给文件名，不读全文）=====",
                ticket_lines,
            ]
        )
    else:
        # 本地兼容模式：沿用原来的本地 project.md 摘要
        context = project_store.fix_context(project_slug)

    body = llm_client.chat_text(
        prompts.FIX_PROMPT,
        f"{context}\n\n===== 报错原文 =====\n{log_text}",
        progress=progress,
    )
    path = ticket_writer.write_fix_ticket(project_slug, body)

    if project_id:
        try:
            project_store.repo.add_ticket(
                project_id=project_id,
                ticket_type="fix",
                filename=path.name,
                content_md=path.read_text(encoding="utf-8"),
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            print(f"[pm] Fix 工单入库失败：{type(exc).__name__}: {str(exc)[:160]}")
            raise LogicError("Fix 工单写入数据库失败，请稍后重试") from None
    return path
