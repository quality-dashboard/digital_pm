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
import re
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


# 澄清阶段必须收集的四个字段：三个具体角色 + 交付形态
ROLE_LABELS: dict[str, str] = {
    "usage": "使用角色",
    "approval": "审批角色",
    "viewer": "数据查看角色",
}
DELIVERY_FORMS: tuple[str, ...] = (
    "网页链接",
    "单文件 HTML",
    "微信小程序",
    "桌面软件",
    "Excel 工具",
    "纯文档",
)

# FDE 五层问题地图：来自售前见客户的真实顺序，不是产品流程。
# ① 听现状 -> ② 画流程 -> ③ 摸数据 -> ④ 定角色 -> ⑤ 划边界。
# 顺序不能换：第③层漏掉，方案会在实施阶段死在“数据根本导不进来”。
#
# 面试三句话：
# 1. 客户说的和他要的是两个东西：他说“要个报表”，真实痛点是“每周花两天核对”。
# 2. 售前最容易漏的是第三层：数据在三个系统里、格式不统一、没人负责导，方案当场作废。
# 3. 所以我把“该问什么”从靠经验，变成一张画得出来、勾得完的地图。
LAYER_NAMES: dict[int, str] = {
    1: "为什么存在",
    2: "现在怎么走",
    3: "数据从哪来",
    4: "谁在用",
    5: "边界",
}
LAYER_STATUS_LABELS: dict[str, str] = {
    "not_started": "未开始",
    "in_progress": "进行中",
    "full": "已勾满",
    "skipped": "已跳过",
    "pending": "待确认",
}


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
    # V4.2：澄清必答项（角色与交付形态），不允许默认成“老板/小程序”
    roles: dict[str, str] = field(default_factory=dict)
    delivery_form: str = ""
    required_pending: bool = False
    # V4.3 四步开场：usage_questions -> inference_confirm -> detail -> done
    onboarding_step: str = ""
    audience: str = ""
    place: str = ""
    result_viewer: str = ""
    inference_pending: list[str] = field(default_factory=list)
    detail_item: str = ""
    # V5 五层问题地图
    layer_status: dict[int, str] = field(default_factory=dict)
    layer_answers: dict[int, list[str]] = field(default_factory=dict)
    layer_summary: dict[int, str] = field(default_factory=dict)
    layer_index: int = 0
    layer_sub_index: int = 0
    layer_unknown: int = 0
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
            "roles": self.roles,
            "delivery_form": self.delivery_form,
            "required_pending": self.required_pending,
            "onboarding_step": self.onboarding_step,
            "audience": self.audience,
            "place": self.place,
            "result_viewer": self.result_viewer,
            "inference_pending": self.inference_pending,
            "detail_item": self.detail_item,
            "layer_status": self.layer_status,
            "layer_answers": self.layer_answers,
            "layer_summary": self.layer_summary,
            "layer_index": self.layer_index,
            "layer_sub_index": self.layer_sub_index,
            "layer_unknown": self.layer_unknown,
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

        # 角色与交付形态：旧快照没有这些字段时保持为空，由页面重新追问
        roles_raw = data.get("roles")
        roles: dict[str, str] = {}
        if isinstance(roles_raw, dict):
            roles = {
                str(key): str(value).strip()
                for key, value in roles_raw.items()
                if str(value).strip()
            }

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
            roles=roles,
            delivery_form=str(data.get("delivery_form", "")).strip(),
            required_pending=bool(data.get("required_pending", False)),
            onboarding_step=str(data.get("onboarding_step", "")),
            audience=str(data.get("audience", "")),
            place=str(data.get("place", "")),
            result_viewer=str(data.get("result_viewer", "")),
            inference_pending=[
                str(item) for item in data.get("inference_pending", [])
            ],
            detail_item=str(data.get("detail_item", "")),
            layer_status={
                int(key): str(value)
                for key, value in (data.get("layer_status") or {}).items()
            },
            layer_answers={
                int(key): [str(item) for item in value]
                for key, value in (data.get("layer_answers") or {}).items()
                if isinstance(value, list)
            },
            layer_summary={
                int(key): str(value)
                for key, value in (data.get("layer_summary") or {}).items()
            },
            layer_index=int(data.get("layer_index", 0) or 0),
            layer_sub_index=int(data.get("layer_sub_index", 0) or 0),
            layer_unknown=int(data.get("layer_unknown", 0) or 0),
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
        """只带最近 6 轮上下文，避免全量对话塞爆 prompt。"""
        recent = self.transcript[-12:]
        answers = [item["content"] for item in recent if item["role"] == "pm"]
        asked = [item["content"] for item in recent if item["role"] == "assistant"]

        lines = [f"原始需求：{self.requirement}", ""]
        if answers:
            lines.append("产品经理的回答：")
            lines.extend(f"{index}. {text}" for index, text in enumerate(answers, 1))
        else:
            lines.append("产品经理尚未回答任何问题。")
        if asked:
            lines.extend(["", "已经问过的问题（不要重复）："])
            lines.extend(f"- {text}" for text in asked)
        if self.roles or self.delivery_form:
            lines.extend(["", "已确认的角色与交付形态（后续必须使用这些角色名）："])
            for key, label in ROLE_LABELS.items():
                if self.roles.get(key):
                    lines.append(f"- {label}：{self.roles[key]}")
            if self.delivery_form:
                lines.append(f"- 交付形态：{self.delivery_form}")
        if self.layer_summary:
            lines.extend(["", "五层访谈摘要（必须沿用，不要重新发明）："])
            for layer in range(1, 6):
                summary = self.layer_summary.get(layer)
                if summary:
                    lines.append(f"- 第{layer}层 {LAYER_NAMES.get(layer, '')}：{summary}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # V4.2 澄清必答项：角色 + 交付形态
    # ------------------------------------------------------------------
    def missing_required_fields(self) -> list[str]:
        """返回尚未确认的必答项，空列表表示已齐。"""
        missing = [
            label
            for key, label in ROLE_LABELS.items()
            if not str(self.roles.get(key, "")).strip()
        ]
        if not str(self.delivery_form or "").strip():
            missing.append("交付形态")
        return missing

    def _requirement_summary(self) -> str:
        """把“我想做个记账的”收敛成“记账的”，用于第一句复述。"""
        text = re.sub(
            r"^(我想做(?:个|一个)?|我想开发(?:个|一个)?|帮我做(?:个|一个)?|"
            r"做(?:个|一个)?|开发(?:个|一个)?|想要(?:个|一个)?)",
            "",
            self.requirement.strip(),
        )
        return (text.strip("。！!，, ") or self.requirement.strip())[:60]

    def _usage_question_text(self) -> str:
        """第 2 步：先复述，再问三个数字选项；只问还没答的。"""
        lines = [
            f"我理解你想做一个「{self._requirement_summary()}」，对吗？",
            "",
            "如果差不多，请回我三个数字：",
        ]
        if not self.audience:
            lines.extend(
                ["一、这东西谁用？", "1 我自己", "2 我的同事", "3 客户或外部的人", ""]
            )
        if not self.place:
            lines.extend(
                ["二、在哪儿用？", "1 手机上", "2 电脑上打开", "3 打印出来", ""]
            )
        if not self.result_viewer:
            lines.extend(
                ["三、做完的结果给谁看？", "1 只有我自己", "2 要给领导", "3 要给客户", ""]
            )
        lines.append("可以按顺序回，例如：1 1 1；也可以先只回一个数字。")
        return "\n".join(lines).strip()

    def _inference_question_text(self) -> str:
        """第 3 步：只猜三件，用户只回“对/不对”。"""
        labels = {
            "approval": "1. 谁点头：我猜没有人需要点头，对吗？（对 / 不对）",
            "viewer": "2. 结果给谁看：我猜只有你自己看，对吗？（对 / 不对）",
            "delivery": "3. 在哪儿打开：我猜是网页链接（手机和电脑都能打开），对吗？（对 / 不对）",
        }
        lines = ["剩下三件我不问你，我先猜，你告诉我哪条不对。", ""]
        lines.extend(labels[item] for item in self.inference_pending)
        lines.extend(["", "可以按顺序回：对 对 对；哪条不对就回“不对”，我再问你。"])
        return "\n".join(lines)

    def _normalize_delivery(self, value: str) -> str:
        """把用户口语化的交付形态收敛成标准选项。"""
        text = str(value or "").strip()
        lower = text.lower()
        if "单文件" in text or "html" in lower:
            return "单文件 HTML"
        if "小程序" in text:
            return "微信小程序"
        if "桌面" in text or "exe" in lower or "客户端" in text:
            return "桌面软件"
        if "excel" in lower or "表格" in text:
            return "Excel 工具"
        if "文档" in text or "word" in lower:
            return "纯文档"
        if "网页" in text or "链接" in text or "网站" in text:
            return "网页链接"
        return text

    def _answer_usage_questions(self, text: str) -> Question | None:
        """第 2 步：解析三个数字；只补问缺失项，绝不把三问再问一遍。"""
        self.transcript.append({"role": "pm", "content": text})
        numbers = re.findall(r"[123]", text or "")

        def take() -> str:
            return numbers.pop(0) if numbers else ""

        if not self.audience:
            self.audience = take()
        if not self.place:
            self.place = take()
        if not self.result_viewer:
            self.result_viewer = take()

        # 关键词兜底：用户没回数字，也能听懂“手机/电脑/领导”
        if not self.audience:
            for word, value in (("同事", "2"), ("客户", "3"), ("外部", "3"), ("自己", "1")):
                if word in text:
                    self.audience = value
                    break
        if not self.place:
            for word, value in (("手机", "1"), ("电脑", "2"), ("打印", "3")):
                if word in text:
                    self.place = value
                    break
        if not self.result_viewer:
            for word, value in (("领导", "2"), ("客户", "3"), ("自己", "1")):
                if word in text:
                    self.result_viewer = value
                    break

        if not (self.audience and self.place and self.result_viewer):
            self.pending_question = Question(
                need_more_info=True,
                question=self._usage_question_text(),
                reason="还差一个数字，先确认这个。",
                options=[],
            )
            self.transcript.append(
                {"role": "assistant", "content": self.pending_question.question}
            )
            session_store.save_state(self)
            return self.pending_question

        self.roles["usage"] = {
            "1": "你自己",
            "2": "你的同事",
            "3": "客户或外部用户",
        }[self.audience]
        self.roles["viewer"] = {
            "1": "你自己",
            "2": "你的领导",
            "3": "你的客户",
        }[self.result_viewer]
        self.roles["approval"] = ""
        self.delivery_form = ""
        self.onboarding_step = "inference_confirm"
        self.inference_pending = ["approval", "viewer", "delivery"]
        self.pending_question = Question(
            need_more_info=True,
            question=self._inference_question_text(),
            reason="先猜，再让你确认，减少你的输入。",
            options=[],
        )
        self.transcript.append(
            {"role": "assistant", "content": self.pending_question.question}
        )
        session_store.save_state(self)
        return self.pending_question

    def _accept_inference_guess(self, item: str) -> None:
        """用户回“对”：接受默认推断。"""
        if item == "approval":
            self.roles["approval"] = "没有人需要点头"
        elif item == "viewer":
            self.roles["viewer"] = "你自己"
        elif item == "delivery":
            self.delivery_form = "网页链接"

    def _begin_inference_detail(self, item: str) -> Question:
        """用户回“不对”：只追问这一条，不重新问另外两条。"""
        self.detail_item = item
        self.onboarding_step = "detail"
        if item == "approval":
            text = "那谁点头？直接说岗位或名字就行。"
        elif item == "viewer":
            text = "那结果给谁看？直接说岗位或名字就行。"
        else:
            text = (
                "那你想怎么拿到结果？\n"
                "1 发网页链接\n"
                "2 发一个文件（双击打开）\n"
                "3 微信小程序\n"
                "4 Excel 或文档"
            )
        self.pending_question = Question(
            need_more_info=True,
            question=text,
            reason="按你的实际情况改一下。",
            options=[],
        )
        self.transcript.append(
            {"role": "assistant", "content": self.pending_question.question}
        )
        session_store.save_state(self)
        return self.pending_question

    def _answer_inference_confirm(self, text: str) -> Question | None:
        """第 3 步：按顺序解析“对/不对”，只补问未确认项。"""
        self.transcript.append({"role": "pm", "content": text})
        tokens = re.findall(r"不对|对", text or "")
        for token in tokens:
            if not self.inference_pending:
                break
            item = self.inference_pending.pop(0)
            if token == "对":
                self._accept_inference_guess(item)
                continue
            if item == "viewer" and self.result_viewer in ("2", "3"):
                # 第二问已经说了给领导/客户，这里直接按那个角色，不再问
                self.roles["viewer"] = {
                    "2": "你的领导",
                    "3": "你的客户",
                }[self.result_viewer]
                continue
            return self._begin_inference_detail(item)

        if self.detail_item:
            return self.pending_question
        return self._continue_inference()

    def _answer_inference_detail(self, text: str) -> Question | None:
        """处理“不对”后的单条补充，然后继续未确认项。"""
        self.transcript.append({"role": "pm", "content": text})
        item = self.detail_item
        if item == "approval":
            self.roles["approval"] = text.strip() or "有人需要点头"
        elif item == "viewer":
            self.roles["viewer"] = text.strip() or "你自己"
        elif item == "delivery":
            self.delivery_form = {
                "1": "网页链接",
                "2": "单文件 HTML",
                "3": "微信小程序",
                "4": "Excel 工具",
            }.get(text.strip(), self._normalize_delivery(text))
        self.detail_item = ""
        return self._continue_inference()

    def _continue_inference(self) -> Question | None:
        """还有未确认项就只问剩下的；全部确认完进入大纲阶段。"""
        if not self.inference_pending:
            return self._finish_onboarding()
        self.onboarding_step = "inference_confirm"
        self.pending_question = Question(
            need_more_info=True,
            question=self._inference_question_text(),
            reason="还剩几条，很快。",
            options=[],
        )
        self.transcript.append(
            {"role": "assistant", "content": self.pending_question.question}
        )
        session_store.save_state(self)
        return self.pending_question

    def _finish_onboarding(self) -> None:
        """三条确认完：允许去生成大纲，不再追问。"""
        self.onboarding_step = "done"
        self.detail_item = ""
        self.inference_pending = []
        self.pending_question = None
        self.phase = config.PHASE_OUTLINE
        self.transcript.append(
            {
                "role": "assistant",
                "content": "好，我按这些推断继续。去「② 大纲确认」生成大纲，确认后再生成产品说明书。",
            }
        )
        session_store.save_state(self)
        return None

    # ------------------------------------------------------------------
    # V5 五层问题地图（一次只问一个，下层不开启）
    # ------------------------------------------------------------------
    def _layer_question_text(self) -> str:
        layer, sub = self.layer_index, self.layer_sub_index
        if layer == 1:
            if sub == 0:
                if any(word in self.requirement for word in ("记账", "记")):
                    return "你现在是怎么记的？"
                return "这件事你现在是怎么做的？"
            if sub == 1:
                return "哪一步最烦？"
            return "做错了，代价有多大？"
        if layer == 2:
            return (
                "现在这件事分几步走？",
                "每一步分别是谁在做？",
                "多久做一次？",
            )[sub]
        if layer == 3:
            return (
                "数据从哪拿？谁给你的？",
                "是什么格式？",
                "多久更新一次？",
            )[sub]
        if layer == 4:
            return (
                "谁负责录入？",
                "谁点头/谁批准？",
                "谁看结果？",
            )[sub]
        if layer == 5:
            return "第一版明确不做什么？"
        return ""

    def _layer_question_count(self, layer: int) -> int:
        return 1 if layer == 5 else 3

    def _layer_summary_text(self, layer: int) -> str:
        answers = self.layer_answers.get(layer, [])
        if self.layer_status.get(layer) == "skipped":
            return "【已跳过】未确认"
        if self.layer_status.get(layer) == "pending":
            return "【待确认】"
        if layer == 1 and len(answers) >= 3:
            return f"现状：{answers[0]}；最烦：{answers[1]}；代价：{answers[2]}"
        if layer == 2 and len(answers) >= 3:
            return f"流程：{answers[0]}；谁做：{answers[1]}；频率：{answers[2]}"
        if layer == 3 and len(answers) >= 3:
            return f"来源：{answers[0]}；格式：{answers[1]}；更新：{answers[2]}"
        if layer == 4 and len(answers) >= 3:
            return f"录入：{answers[0]}；点头：{answers[1]}；查看：{answers[2]}"
        if layer == 5 and answers:
            return f"第一版不做：{answers[0]}"
        return ""

    def _answer_layer(self, text: str) -> Question | None:
        """当前层只推进一步；不跨层、不连发多问。"""
        self.transcript.append({"role": "pm", "content": text})
        current = self.layer_index

        if any(word in text for word in ("不重要", "跳过", "不需要聊")):
            self.layer_status[current] = "skipped"
            self.layer_summary[current] = "【已跳过】未确认"
            return self._advance_layer()

        if any(word in text for word in ("不知道", "不清楚", "没统计", "答不上", "没有数据")):
            self.layer_unknown += 1
            if self.layer_unknown >= 3:
                # 连续三次答不上：标待确认，绝不替他编
                self.layer_status[current] = "pending"
                self.layer_summary[current] = "【待确认】"
                return self._advance_layer()
            self.pending_question = Question(
                need_more_info=True,
                question=self._layer_question_text(),
                reason="没关系，我先记【待确认】；你按知道的说就行。",
                options=[],
            )
            self.transcript.append(
                {"role": "assistant", "content": self.pending_question.question}
            )
            session_store.save_state(self)
            return self.pending_question

        answers = self.layer_answers.setdefault(current, [])
        answers.append(text)
        self.layer_unknown = 0
        self.layer_sub_index += 1

        if self.layer_sub_index >= self._layer_question_count(current):
            self.layer_status[current] = "full"
            self.layer_summary[current] = self._layer_summary_text(current)
            return self._advance_layer()

        self.pending_question = Question(
            need_more_info=True,
            question=self._layer_question_text(),
            reason="这一层还没聊完，继续。",
            options=[],
        )
        self.transcript.append(
            {"role": "assistant", "content": self.pending_question.question}
        )
        session_store.save_state(self)
        return self.pending_question

    def _advance_layer(self) -> Question | None:
        """当前层结束才开启下一层；五层结束进入确认单。"""
        next_layer = self.layer_index + 1
        if next_layer > 5:
            self.layer_index = 0
            self.layer_sub_index = 0
            self.layer_unknown = 0
            self.pending_question = None
            self.phase = config.PHASE_OUTLINE
            self.transcript.append(
                {
                    "role": "assistant",
                    "content": "五层聊完了。先看需求确认单，确认后再展开完整 PRD。",
                }
            )
            session_store.save_state(self)
            return None

        self.layer_index = next_layer
        self.layer_sub_index = 0
        self.layer_unknown = 0
        self.layer_status[next_layer] = "in_progress"
        self.pending_question = Question(
            need_more_info=True,
            question=self._layer_question_text(),
            reason="一层一层来，不跳层。",
            options=[],
        )
        self.transcript.append(
            {"role": "assistant", "content": self.pending_question.question}
        )
        session_store.save_state(self)
        return self.pending_question

    def document_gate(self) -> tuple[bool, str]:
        """硬门禁：五层必须全部“已勾满”或“已跳过”，否则不生成文档。"""
        missing = [
            layer
            for layer in range(1, 6)
            if self.layer_status.get(layer) not in ("full", "skipped")
        ]
        if missing:
            return (
                False,
                f"还有 {len(missing)} 层没聊透，现在出文档一定是偏的。"
                f"我们先说第 {missing[0]} 层。",
            )
        return True, ""

    def build_confirmation_sheet(self) -> str:
        """生成《需求确认单》五栏；缺一层就拒绝，不调用模型。"""
        ok, message = self.document_gate()
        if not ok:
            raise LogicError(message)

        def answer(layer: int, index: int = 0) -> str:
            if self.layer_status.get(layer) == "skipped":
                return "【已跳过】未确认"
            if self.layer_status.get(layer) == "pending":
                return "【待确认】"
            values = self.layer_answers.get(layer, [])
            return values[index] if len(values) > index else "【待确认】"

        lines = [
            "# 需求确认单",
            "",
            "## 一、客户原话",
            self.requirement.strip(),
            "",
            "## 二、我理解你要解决的是",
            f"{self.layer_summary.get(1, '【待确认】')}；{self.layer_summary.get(2, '【待确认】')}",
            "",
            "## 三、第一版做这几件",
            f"- 按现在的流程跑通：{answer(2, 0)}",
            f"- 每一步有人负责：{answer(2, 1)}",
            f"- 按这个频率先跑起来：{answer(2, 2)}",
            "",
            "## 四、第一版不做这几件",
            f"- {answer(5, 0)}",
            "",
            "## 五、你需要提供的东西",
            f"- 数据：{self.layer_summary.get(3, '【待确认】')}",
            f"- 配合的人：{self.layer_summary.get(4, '【待确认】')}",
            "- 请确认：谁配合、给什么数据、开什么权限、什么时候能给。",
        ]
        return "\n".join(lines)

    def _infer_delivery_from_requirement(self) -> str:
        text = self.requirement.lower()
        if "小程序" in text:
            return "微信小程序"
        if "excel" in text or "表格" in text:
            return "Excel 工具"
        if "文档" in text:
            return "纯文档"
        return "网页链接"

    def ensure_outline_from_layers(self) -> Outline:
        """五层确认后，用确认单直接生成一个可开 PRD 的大纲。"""
        ok, message = self.document_gate()
        if not ok:
            raise LogicError(message)
        usage = self.layer_answers.get(4, ["待确认", "待确认", "待确认"])
        self.roles = {
            "usage": usage[0] if len(usage) > 0 else "待确认",
            "approval": usage[1] if len(usage) > 1 else "待确认",
            "viewer": usage[2] if len(usage) > 2 else "待确认",
        }
        self.delivery_form = self._infer_delivery_from_requirement()
        name = self.project_name or self._requirement_summary()[:16] or "未命名项目"
        self.project_name = name
        self.project_slug = config.slugify_project_name(name)
        self.outline = Outline(
            project_name=name,
            summary=f"{self.layer_summary.get(1, '')}；{self.layer_summary.get(2, '')}",
            modules=[
                Module(name="需求确认", goal="把五层访谈结论固化为确认单", kind="doc"),
                Module(
                    name="核心流程",
                    goal="按确认单跑通第一版流程",
                    kind="backend",
                    depends_on=["需求确认"],
                ),
                Module(
                    name="验收",
                    goal="按确认单逐条验收",
                    kind="test",
                    depends_on=["核心流程"],
                ),
            ],
            research_needed=False,
        )
        self.outline_confirmed = True
        self.phase = config.PHASE_CONFIRMED
        self._sync_project()
        session_store.save_state(self)
        return self.outline

    def _ensure_required_fields(self) -> None:
        """生成大纲/说明书前的硬门禁：开场三件必须确认完。"""
        if self.missing_required_fields():
            raise LogicError("还差一点信息，请回到「① 需求与追问」把开头三件确认完。")

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
        """开始五层问题地图：第一屏从第 1 层第一个问题开始。"""
        self.phase = config.PHASE_CLARIFY
        self.round = 0
        self.transcript = []
        self.layer_status = {layer: "not_started" for layer in range(1, 6)}
        self.layer_answers = {}
        self.layer_summary = {}
        self.layer_index = 1
        self.layer_sub_index = 0
        self.layer_unknown = 0
        self.layer_status[1] = "in_progress"
        self.onboarding_step = ""
        self.roles = {}
        self.delivery_form = ""
        question = Question(
            need_more_info=True,
            question=self._layer_question_text(),
            reason="先聊第 1 层：这件事现在怎么做的。",
            options=[],
        )
        self._ensure_project()
        self.pending_question = question
        self.transcript.append({"role": "assistant", "content": question.question})
        session_store.save_state(self)
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
        if self.layer_index > 0:
            return self._answer_layer(text)
        if self.onboarding_step == "usage_questions":
            return self._answer_usage_questions(text)
        if self.onboarding_step == "inference_confirm":
            return self._answer_inference_confirm(text)
        if self.onboarding_step == "detail":
            return self._answer_inference_detail(text)
        if self.pending_question is None:
            raise LogicError("当前没有待回答的问题")

        # 失败回滚快照：模型报错时必须恢复上一个可回答问题，避免卡成“无头案”
        previous_question = self.pending_question
        previous_round = self.round
        self.transcript.append({"role": "pm", "content": text})
        self.pending_question = None
        self.round += 1

        # 达到追问轮数上限，不再纠缠，直接允许生成大纲
        if self.round >= config.MAX_CLARIFY_ROUNDS:
            self.phase = config.PHASE_OUTLINE
            session_store.save_state(self)
            return None

        try:
            question = self._ask(progress)
        except Exception:
            # 回滚：恢复待回答问题、轮次与对话记录，页面继续显示回答框
            self.pending_question = previous_question
            self.round = previous_round
            if (
                self.transcript
                and self.transcript[-1].get("role") == "pm"
                and self.transcript[-1].get("content") == text
            ):
                self.transcript.pop()
            raise

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
        if self.onboarding_step not in ("", "done"):
            # 四步开场还没确认完：不跳过，也不用内部术语解释
            self.transcript.append(
                {"role": "assistant", "content": "先把这三件确认完，马上就能生成，不用等很久。"}
            )
            session_store.save_state(self)
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
        self._ensure_required_fields()
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
        self._ensure_required_fields()
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

        ok, message = self.document_gate()
        if not ok:
            raise LogicError(message)
        if not self.outline_confirmed or self.outline is None:
            raise LogicError("请先确认大纲，再生成产品说明书")
        self._ensure_required_fields()

        _notify("正在把你的需求翻译成小白能看懂的产品说明书...")
        modules_text = "\n".join(
            f"- {module.name}：{module.goal}" for module in self.outline.modules
        )
        usage = self.roles.get("usage", "使用角色")
        approval = self.roles.get("approval", "审批角色")
        viewer = self.roles.get("viewer", "数据查看角色")
        delivery = self.delivery_form or "未指定"
        user_content = "\n".join(
            [
                "===== 用户原始需求 =====",
                self.requirement.strip(),
                "",
                "===== 已确认角色（全文必须使用这些具体名称）=====",
                f"- 使用角色：{usage}",
                f"- 审批角色：{approval}",
                f"- 数据查看角色：{viewer}",
                "",
                "===== 交付形态（不得假设成小程序）=====",
                f"- 本产品交付形态为：{delivery}",
                "",
                "===== 已确认的产品模块 =====",
                modules_text or "无",
                "",
                "===== 用户在追问中的补充回答 =====",
                self._qa_digest() or "无",
                "",
                "请输出完整产品说明书，必须包含：基础假设、交付形态说明、"
                "产品功能清单（每条带来源标签）、核心操作流程（只用具体角色名）、"
                "验收清单、冲突处理、实现风险清单、外包报价、发布引流、本次推断清单。"
                "不要写代码，不要写数据库和 API。",
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

        usage = self.roles.get("usage", "使用角色")
        approval = self.roles.get("approval", "审批角色")
        viewer = self.roles.get("viewer", "数据查看角色")
        delivery = self.delivery_form or "未指定"

        # 1) 清除模板污染：老板/管理员/用户 一律替换成具体角色
        body = (
            body.replace("老板", approval)
            .replace("管理员", viewer)
            .replace("用户", usage)
        )
        # 交付形态不是小程序时，清掉模型残留的“小程序”假设
        if delivery != "微信小程序":
            body = body.replace("微信小程序", delivery).replace("小程序", delivery)

        # 2) 基础假设：代码强制注入，保证四类假设逐条可确认
        if delivery == "微信小程序":
            storage = "微信云数据库"
            deploy = "微信开发者工具提交审核，审核周期约 1~7 天，需要小程序主体认证"
        elif delivery == "单文件 HTML":
            storage = "浏览器本地存储，容量有限，清理缓存可能丢失"
            deploy = "直接发送 HTML 文件或用静态托管链接分享，不依赖服务器"
        elif delivery == "网页链接":
            storage = "云端数据库"
            deploy = "部署到云服务器或静态托管，生成可访问链接"
        elif delivery == "Excel 工具":
            storage = "本地 Excel 文件"
            deploy = "发送 Excel 文件，需要说明 Office 版本与宏支持"
        elif delivery == "桌面软件":
            storage = "本地数据库或本地文件"
            deploy = "提供安装包，需要说明 Windows/macOS 兼容性"
        else:
            storage = "本地文件或浏览器存储"
            deploy = "按实际交付方式说明"

        base_bullets = (
            f"- 交付形态：我推断为{delivery}，如不对请指出\n"
            f"- 使用角色：我推断为{usage}，如不对请指出\n"
            f"- 审批角色：我推断为{approval}，如不对请指出\n"
            f"- 数据查看角色：我推断为{viewer}，如不对请指出\n"
            f"- 数据存放位置：我推断为{storage}，如不对请指出\n"
            f"- 上线/分发方式：我推断为{deploy}，如不对请指出\n"
            f"- 本产品交付形态为{delivery}，原因是你在澄清阶段确认了它；如果不对请指出\n"
        )
        if "基础假设" not in body:
            body = (
                "## 〇、基础假设（请逐条确认）\n" + base_bullets + "\n" + body
            )
        else:
            # 模型可能只写了一段话而不是可勾选列表，代码补一份标准确认清单
            body = (
                "### 基础假设补充（请逐条确认）\n" + base_bullets + "\n" + body
            )
        if "本产品交付形态为" not in body:
            body = (
                f"## 交付形态\n本产品交付形态为{delivery}，原因是你选择/确认了它。\n\n"
            ) + body

        # 3) 每条功能/流程补来源标签（模型漏标时保守标为推断）
        lines = body.splitlines()
        in_labeled_section = False
        for index, line in enumerate(lines):
            if "产品功能清单" in line or "核心操作流程" in line:
                in_labeled_section = True
                continue
            if in_labeled_section and line.startswith("## "):
                in_labeled_section = False
            if (
                in_labeled_section
                and line.strip().startswith("- ")
                and not any(tag in line for tag in ("【你说的】", "【我推断的】", "【行业惯例】"))
            ):
                lines[index] = line.rstrip() + "【我推断的】"
        body = "\n".join(lines)

        # 4) 冲突处理：禁止“按确定方案执行”，缺失时给标准选项
        body = body.replace(
            "按确定方案执行",
            "按推荐方案执行，已放弃另一方案的能力，你可以要求改回",
        )
        if "方案一" not in body:
            body += (
                "\n\n## 四、冲突处理\n"
                "冲突点：需求中存在互相制约的目标（例如纯离线可用 vs 在线识别/在线图表）。\n"
                "方案一：优先保证离线可用，放弃部分在线能力。\n"
                "方案二：优先在线能力，接受断网/弱网不可用。\n"
                "我的推荐：方案一，理由是目标场景网络不稳定。\n"
                "请确认选哪个。\n"
                "此处我按方案一执行，已放弃方案二的在线能力，你可以要求改回。"
            )
        elif "请确认" not in body:
            body += "\n\n请确认选哪个。"

        # 5) 实现风险清单：代码兜底，覆盖四类必查风险
        risk_body = (
            "| 功能点 | 实现难度 | 具体风险 | 替代方案 |\n"
            "| --- | --- | --- | --- |\n"
            "| 纯离线图表 | 高 | 断网时图表库/字体加载失败 | 预置轻量图表，或降级为表格汇总 |\n"
            "| 导出表格 | 中 | 中文编码可能乱码 | 统一 UTF-8 BOM 或 Excel 兼容格式 |\n"
            "| 本地存储 | 中 | 浏览器容量有限，清理缓存会丢失 | 限制附件大小，提供导出备份 |\n"
            "| 移动端操作 | 中 | 手势/点击精度不足 | 使用大按钮，减少滑动依赖 |\n"
        )
        if "实现风险清单" not in body:
            body += "\n\n## 五、实现风险清单\n" + risk_body
        elif "离线" not in body or "编码" not in body:
            body += "\n\n### 实现风险补充\n" + risk_body

        # 6) 商业板块兜底
        extra_sections: list[str] = []
        if "外包" not in body:
            extra_sections.append(
                "## 六、外包防坑报价参考\n"
                "本产品属于轻量到中等复杂度。若找传统外包公司，报价通常在 "
                "5000~20000 元，常见加价点是改需求、加页面和后期维护。"
                "建议不要找全额外包，直接拿着这份说明书去 WorkBuddy 召唤"
                "对应专家团自己生成，成本近乎为零。"
            )
        if "引流" not in body:
            extra_sections.append(
                "## 七、发布与引流建议\n"
                f"第一波冷启动建议从小红书开始：用「不懂代码的文科生，用 AI 花了 2 小时"
                f"做了一个{self.outline.project_name}产品」这类标题，配 3 张使用截图；"
                "同步发朋友圈和 2~3 个目标用户社群。先把体验用户加到微信，"
                "建立个人 IP，再考虑会员、定制或广告变现。"
            )
        if extra_sections:
            body += "\n\n" + "\n\n".join(extra_sections)

        # 7) 本次推断清单：从正文提取 + 基础字段兜底，保证 >=5 条
        inferred: list[str] = []
        for line in body.splitlines():
            if "【我推断的】" in line:
                item = (
                    line.replace("【我推断的】", "")
                    .strip()
                    .lstrip("-* ")
                    .strip()
                )
                if item and item not in inferred:
                    inferred.append(item)
        fallback_inferences = [
            f"交付形态为{delivery}",
            f"使用角色为{usage}",
            f"审批角色为{approval}",
            f"数据查看角色为{viewer}",
            f"数据存放位置为{storage}",
            f"上线/分发方式为{deploy}",
        ]
        for item in fallback_inferences:
            if not any(item in existing for existing in inferred):
                inferred.append(item)
        inferred = inferred[:12]
        inference_lines = [
            "## 八、本次推断清单（请逐条确认）",
            "| # | 我推断的内容 | 是否成立 |",
            "| --- | --- | --- |",
        ]
        for idx, item in enumerate(inferred, 1):
            inference_lines.append(f"| {idx} | {item} | 请回复 是/否 |")
        body += "\n\n" + "\n".join(inference_lines)

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
