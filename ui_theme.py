# -*- coding: utf-8 -*-
"""
ui_theme.py —— Router（数字项目经理）UI 主题层

职责：
  ① 把 router_theme.css 注入 Streamlit 页面
  ② 用自定义 HTML 渲染 Streamlit 原生组件做不出来的「高级感」部件

接入方式（app_ui.py 顶部，set_page_config 之后第一行 UI 代码）：
    import ui_theme
    ui_theme.inject_theme()

两个必须遵守的防泄漏规则（本次修复）：
  1. CSS 里若出现 </style>（包括注释里的示例代码），会提前闭合真正的 style 标签，
     后面的 CSS 会被浏览器当成正文显示。inject_theme 会统一转义。
  2. st.markdown 会把缩进 4 个空格以上的内容当成 Markdown 代码块，
     即使 unsafe_allow_html=True 也会把 HTML 源码原样打印出来。
     因此所有自定义 HTML 都先 textwrap.dedent + strip 再交给 st.markdown。

注意：
  - inject_theme() 每次脚本重跑都会执行，这是必须的（Streamlit 每次 rerun 重建 DOM）
  - 所有插入 HTML 的动态文本一律 html.escape，工单正文 / 报错堆栈含 < > & 时不会破坏结构
  - 本文件只依赖 Python 标准库 + streamlit，不引入前端依赖
"""
from __future__ import annotations

import html
import textwrap
from pathlib import Path
from typing import Iterable

import streamlit as st

_CSS_PATH = Path(__file__).with_name("router_theme.css")

_TONES = {"run", "wait", "done", "error", "neutral"}


def _esc(value: object) -> str:
    """所有动态文本进 HTML 前必须转义。"""
    return html.escape(str(value), quote=True)


def _markdown_html(markup: str) -> None:
    """输出自定义 HTML。

    关键：先 dedent + strip，去掉公共缩进和首尾空白。
    否则 st.markdown 会把 4 空格缩进的 HTML 当 Markdown 代码块，
    页面上会直接暴露 <div> / 字符串源码，unsafe_allow_html 也救不回来。
    """
    cleaned = textwrap.dedent(markup).strip()
    st.markdown(cleaned, unsafe_allow_html=True)


def inject_theme(css_path: str | None = None) -> None:
    """注入全局样式。必须在每个 rerun 调用一次，放在任何 UI 组件之前。"""
    path = Path(css_path) if css_path else _CSS_PATH
    # utf-8-sig：兼容带 BOM 的 CSS（BOM 会让 <style> 前面多出不可见字符，导致标签失效）
    css = path.read_text(encoding="utf-8-sig")

    # 防泄漏：CSS 注释里可能写了示例 </style>，浏览器会在这里提前闭合 style，
    # 把后面的全部 CSS 当正文渲染。转义后既保留注释，也不会闭合标签。
    css = css.replace("</style>", "<\\/style>")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def badge(text: str, tone: str = "neutral") -> str:
    """状态徽章 HTML（可拼进其他组件的 HTML 里）。

    tone: run=追问中 / wait=等待确认 / done=已完成 / error=报错 / neutral=中性
    """
    tone = tone if tone in _TONES else "neutral"
    return (
        f'<span class="pm-badge pm-badge--{tone}">'
        f'<span class="pm-badge-dot"></span>{_esc(text)}</span>'
    )


def render_header(
    project: str,
    step: int,
    phase_text: str,
    phase_tone: str = "run",
) -> None:
    """顶部自定义 Header：品牌 + 三步进度 + 当前项目 + 阶段徽章。

    step: 1=澄清需求 / 2=确认大纲 / 3=工单看板
    """
    steps = ("澄清需求", "确认大纲", "工单看板")
    parts = []
    for index, name in enumerate(steps, 1):
        state = "is-done" if index < step else ("is-active" if index == step else "")
        mark = "✓" if index < step else str(index)
        parts.append(
            f'<div class="pm-step {state}">'
            f'<span class="pm-step-dot">{mark}</span><span>{name}</span></div>'
        )
        if index < len(steps):
            parts.append('<span class="pm-step-line"></span>')

    _markdown_html(
        f"""
        <div class="pm-header">
          <div class="pm-brand">
            <span class="pm-brand-name">数字项目经理</span>
            <span class="pm-brand-sub">Router</span>
          </div>
          <div class="pm-steps">{''.join(parts)}</div>
          <div class="pm-header-right">
            <span class="pm-header-project">{_esc(project or '未选择项目')}</span>
            {badge(phase_text, phase_tone)}
          </div>
        </div>
        """
    )


def render_status_panel(
    phase_text: str,
    phase_tone: str,
    round_now: int,
    round_max: int,
    meta_rows: Iterable[tuple[str, str]] = (),
) -> None:
    """侧边栏状态面板：当前阶段 + 追问进度 + 运行配置。

    在 `with st.sidebar:` 内调用。项目选择器等其他组件照常放在它后面。
    """
    pct = 0 if round_max <= 0 else min(100, int(round_now / round_max * 100))
    rows = "".join(
        f'<div class="pm-panel-row"><span>{_esc(label)}</span><b>{_esc(value)}</b></div>'
        for label, value in meta_rows
    )
    _markdown_html(
        f"""
        <div class="pm-panel-card">
          <div class="pm-panel-title">Current State</div>
          <div style="margin-bottom:12px">{badge(phase_text, phase_tone)}</div>
          <div class="pm-panel-row"><span>追问进度</span><b>{round_now} / {round_max}</b></div>
          <div class="pm-progress-track"><div class="pm-progress-fill" style="width:{pct}%"></div></div>
        </div>
        <div class="pm-panel-card">
          <div class="pm-panel-title">Run Config</div>
          {rows or '<div class="pm-panel-row"><span>暂无</span><b>—</b></div>'}
        </div>
        """
    )


def render_question(question: str, reason: str = "", options: Iterable[str] = ()) -> None:
    """追问卡片：替代 st.info 的默认蓝底，把「为什么问」降级为辅助信息。"""
    why = f'<div class="pm-question-why">为什么问：{_esc(reason)}</div>' if reason else ""
    opts_list = list(options)
    opts = (
        '<div class="pm-question-why">可能的方向：' + " / ".join(_esc(o) for o in opts_list) + "</div>"
        if opts_list else ""
    )
    _markdown_html(
        f"""
        <div class="pm-question">
          <div class="pm-question-label">AI 追问</div>
          <div class="pm-question-text">{_esc(question)}</div>
          {why}{opts}
        </div>
        """
    )


def outline_card(index: int, module: dict) -> str:
    """大纲模块卡片 HTML（在 st.columns 的每一列里 st.markdown 输出）。

    返回值已 dedent + strip，调用方直接：
        st.markdown(ui_theme.outline_card(...), unsafe_allow_html=True)
    不会触发 Markdown 缩进代码块。
    """
    deps = module.get("depends_on") or []
    dep_text = "、".join(str(d) for d in deps) if deps else "无"
    return textwrap.dedent(
        f"""
        <div class="pm-card">
          <div class="pm-card-head">
            <span class="pm-card-index">{index}</span>
            <span class="pm-card-title">{_esc(module.get('name', ''))}</span>
            {badge(str(module.get('kind', '模块')), 'neutral')}
          </div>
          <div class="pm-card-row"><b>目标</b><span>{_esc(module.get('goal', ''))}</span></div>
          <div class="pm-card-row"><b>依赖</b><span>{_esc(dep_text)}</span></div>
        </div>
        """
    ).strip()


def ticket_card_head(item: dict) -> str:
    """工单卡片头部 HTML；详情与代码块仍用 st.expander + st.markdown 渲染，
    这样 Markdown / 语法高亮由 Streamlit 负责，零转义风险。
    """
    deps = item.get("depends_on") or "无"
    return textwrap.dedent(
        f"""
        <div class="pm-card">
          <div class="pm-card-head">
            <span class="pm-card-index">{_esc(item.get('index', ''))}</span>
            <span class="pm-card-title">{_esc(item.get('title', ''))}</span>
          </div>
          <div class="pm-card-file">{_esc(item.get('name', ''))}</div>
          <div class="pm-card-row"><b>类型</b><span>{_esc(item.get('kind', ''))}</span></div>
          <div class="pm-card-row"><b>依赖</b><span>{_esc(deps)}</span></div>
          <div class="pm-card-row"><b>更新</b><span>{_esc(item.get('updated_at', ''))}</span></div>
        </div>
        """
    ).strip()
