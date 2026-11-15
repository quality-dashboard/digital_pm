# -*- coding: utf-8 -*-
"""
app_ui.py —— Streamlit 极简看板（V3.1.1 云端版）

四个标签页：
① 需求与追问：输入需求 → 初步分析 → 一次一问的对话流
② 大纲确认：生成 3~4 模块大纲 → 确认无误
③ 工单看板：数据库读取 + 一键导出 ZIP
④ Bug 修复：贴报错 → 生成 fix_NN.md

V3.1.1 关键变化：
- 页面最顶部有访问码门禁，未通过不渲染任何业务逻辑；
- 每个访客生成独立 session_id，项目/工单全部按 session_id 从数据库读写；
- 看板与 ZIP 导出只依赖数据库，不依赖 Streamlit Cloud 的临时磁盘；
- 不再读取共享磁盘上的 session.jsonl 恢复对话，避免多访客串数据。
"""
from __future__ import annotations

import shutil
from collections.abc import Callable

import streamlit as st

# -----------------------------------------------------------------------------
# set_page_config 必须是第一个 Streamlit 命令
# 注意：必须放在 import config 之前。config 导入时会读取 st.secrets（云端配置），
# 先读 secrets 会让 set_page_config 不再是「第一个命令」而直接报错。
# -----------------------------------------------------------------------------
st.set_page_config(page_title="数字项目经理 V3.1.2", page_icon="🧭", layout="wide")

try:

    # 这些模块必须在 set_page_config 之后导入：config 会访问 st.secrets
    import config  # noqa: E402
    import db  # noqa: E402
    import decompose_logic  # noqa: E402
    import export_service  # noqa: E402
    import llm_client  # noqa: E402
    import project_store  # noqa: E402
    import schemas  # noqa: E402
    import ui_theme  # noqa: E402

    # 全局注入 UI/UX 主题：set_page_config 之后第一件事，必须在任何业务组件之前
    # 每次 rerun 都要执行，Streamlit 会重建 DOM，不注入样式会丢失
    ui_theme.inject_theme()

    # 每次重跑都重新读取 .env：改完密钥无需重启进程
    config.load_env_file()
    config.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

    _PHASE_LABELS = {
        config.PHASE_CLARIFY: "追问中",
        config.PHASE_OUTLINE: "待确认大纲",
        config.PHASE_CONFIRMED: "大纲已确认",
        config.PHASE_TICKETS: "工单已生成",
        config.PHASE_BUG: "报错回流",
    }

    # 需求文本长度约束：
    # 下限 20 字拦截「做个系统」这类无信息量输入，上限 8000 字防止超长文本打爆大模型 Token
    REQ_MIN_CHARS = 20
    REQ_MAX_CHARS = 8000


    # =============================================================================
    # 一、公网安全门（最优先：未通过不执行任何业务逻辑）
    # =============================================================================
    if "access_ok" not in st.session_state:
        st.session_state["access_ok"] = False

    if not st.session_state["access_ok"]:
        st.title("数字项目经理 V3.1.2")
        st.caption("内部体验版 · 请先输入访问码")

        access_code = st.text_input(
            "请输入内部体验码",
            type="password",
            key="access_code_input",
            placeholder="向管理员获取体验码",
        )
        if st.button("进入", type="primary", use_container_width=True):
            if access_code.strip() == config.APP_ACCESS_CODE:
                st.session_state["access_ok"] = True
                st.rerun()
            else:
                st.error("体验码不正确，请向管理员确认")

        # 关键：st.stop() 保证门禁之后的内容（含数据库查询）完全不执行
        st.stop()


    # =============================================================================
    # 二、访客标识与云端状态恢复
    # =============================================================================
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = config.new_session_id()
    session_id: str = st.session_state["session_id"]

    # 生成中的重入拦截：用户刷新/重跑时直接停止，避免重复调用模型扣费
    if st.session_state.get("busy_phase"):
        st.info("正在生成中，请稍候……")
        st.stop()

    # 初始化数据库：Turso 连不上会自动降级到本地 SQLite，先保证页面可用
    try:
        db.db.init_schema()
    except Exception:
        # 固定文案：不显示路径、异常类型或堆栈
        st.error("数据库初始化失败，请联系管理员或稍后重试。")
        st.stop()

    if db.db.fallback_reason:
        st.warning(
            "⚠️ 云端数据库连接失败，系统已自动切换至本地离线模式，"
            "您的数据将保存在本地浏览器，不影响使用。"
        )


    def _session() -> decompose_logic.PMSession | None:
        """取当前对话状态；Streamlit 重跑脚本时对象会保留在 session_state。"""
        return st.session_state.get("pm_session")


    def _store(session: decompose_logic.PMSession) -> None:
        """写回对话状态。"""
        st.session_state["pm_session"] = session


    # 只有业务层精心构造的中文提示可以直接展示；
    # 其他异常（数据库驱动、网络、未知错误）统一用固定文案，避免泄露路径/堆栈。
    _SAFE_ERRORS = (
        decompose_logic.LogicError,
        export_service.ExportError,
        db.DatabaseError,
        llm_client.LLMError,
    )


    def _error_text(exc: Exception, fallback: str = "操作失败，请稍后重试") -> str:
        """把异常转换成页面可展示的短中文提示。"""
        if isinstance(exc, schemas.SchemaError):
            # 模型 JSON 结构不符合约定：给用户可理解的提示，不展示字段级细节
            return "AI 返回格式异常，请重试"
        if isinstance(exc, _SAFE_ERRORS):
            message = str(exc).strip()
            return message[:300] if message else fallback
        return fallback


    def _begin_busy(phase: str) -> None:
        """标记当前步骤正在生成，防止连点与刷新重入导致重复扣费。"""
        st.session_state["busy_phase"] = phase


    def _end_busy() -> None:
        """生成结束（成功或失败）后释放锁。"""
        st.session_state.pop("busy_phase", None)


    def _is_busy() -> bool:
        """当前是否有生成任务在跑；用于按钮 disabled。"""
        return bool(st.session_state.get("busy_phase"))


    def _status_progress(status) -> Callable[[str], None]:
        """把 LLM 层的进度旁白接到 st.status，避免长请求看起来像「页面卡死」。

        每次收到消息做两件事：
        1. 更新 status 标题（用户一眼看到当前在做什么）；
        2. 追加一行旁白历史（事后能看到「主模型超时、已切备用模型」的完整过程）。

        只做同步更新，不启后台线程：Streamlit 的元素更新必须发生在脚本主线程。
        """

        def _report(message: str) -> None:
            status.update(label=message)
            st.caption(f"· {message}")

        return _report


    def _quota_guard() -> None:
        """全局每日配额：超过 100 次直接拦截，保护 API 额度。

        配额按北京时间切分；只有真正要调用大模型时才消耗次数。
        """
        allowed, _used = db.db.consume_quota(limit=100)
        if not allowed:
            st.error("今日免费体验名额已满，请明天再来～")
            st.stop()


    def _active_project_id(selected: str) -> str:
        """当前操作的项目：优先侧边栏选择，其次当前会话项目。"""
        if selected != "（新建项目）":
            return selected
        session = _session()
        return session.project_id if session else ""


    def _project_slug(project_id: str) -> str:
        """数据库项目名 -> 本地目录名（双写本地文件时使用）。"""
        project = project_store.repo.get_project(project_id)
        if not project:
            return ""
        return config.slugify_project_name(str(project.get("name", "")))


    def _first_heading(text: str) -> str:
        """取工单 Markdown 第一行一级标题作为展示标题。"""
        for line in (text or "").splitlines():
            if line.startswith("# "):
                return line[2:].strip()[:60]
        return ""


    def _header_state(session: decompose_logic.PMSession | None) -> tuple[int, str, str]:
        """把内部 phase 映射成 Header 的三步进度、阶段文案与徽章色调。

        纯展示逻辑，不改变任何状态机流转：
        1=澄清需求 / 2=确认大纲 / 3=工单看板
        """
        if not session:
            return 1, "等待需求", "neutral"

        mapping = {
            config.PHASE_CLARIFY: (1, "澄清需求中", "run"),
            config.PHASE_OUTLINE: (2, "等待确认大纲", "wait"),
            config.PHASE_CONFIRMED: (3, "大纲已确认", "wait"),
            config.PHASE_TICKETS: (3, "工单已生成", "done"),
            config.PHASE_BUG: (3, "报错回流", "error"),
        }
        return mapping.get(session.phase, (1, "澄清需求中", "run"))


    def _depends_from_content(text: str) -> str:
        """从工单元信息行提取依赖，仅用于卡片展示；解析失败返回「无」。"""
        for line in (text or "").splitlines():
            if line.startswith(">") and "依赖：" in line:
                return line.split("依赖：", 1)[1].split("｜")[0].strip() or "无"
        return "无"


    @st.fragment
    def render_chat() -> None:
        """ChatGPT 式对话流：欢迎语 + 历史消息 + 快捷选项 + chat_input。

        只做展示与调用既有状态机方法，不改变追问/大纲/工单的业务规则。
        """
        # 全局错误提示：聊天失败后保留一条友好提示，下一次操作自动清除
        chat_error = st.session_state.pop("chat_error", "")
        if chat_error:
            st.error(chat_error)

        session = _session()
        # 状态自检：追问阶段却没有待回答问题，说明状态卡死，重置为初始输入
        if (
            session is not None
            and session.phase == config.PHASE_CLARIFY
            and session.pending_question is None
            and not _is_busy()
        ):
            st.session_state["requirement_input"] = session.requirement
            st.session_state.pop("pm_session", None)
            st.session_state.pop("restored_from", None)
            session = None

        # 首次进入的主动打招呼（常驻在对话开头）
        with st.chat_message("assistant"):
            st.write("你好，我是你的数字项目经理。你想开发什么应用？随便说，多离谱都行。")

        # 历史对话：pm -> user，其余 -> assistant
        if session:
            for item in session.transcript:
                with st.chat_message("user" if item["role"] == "pm" else "assistant"):
                    st.write(item["content"])

        # 合规勾选：未勾选不展示输入框，避免用户直接发送
        agree = st.checkbox(
            "我已阅读并同意：本工具会保存你的需求记录 30 天用于进度恢复，不会收集您的个人身份信息。",
            key="consent_agree",
        )
        if not agree:
            st.caption("勾选上方同意项后，即可开始对话。")
            return

        # 追问快捷选项：模型给出 options 时，用户可一键发送
        if session and session.pending_question and session.pending_question.options:
            st.caption("不想打字？直接点一个方向：")
            option_cols = st.columns(min(len(session.pending_question.options), 3))
            for index, option in enumerate(session.pending_question.options):
                if option_cols[index % len(option_cols)].button(
                    option,
                    key=f"quick_option_{session.round}_{index}",
                    disabled=_is_busy(),
                    use_container_width=True,
                ):
                    _handle_chat_message(session, option)

        # 逃生门：不想继续被追问，直接进入大纲
        if session and session.pending_question:
            if st.button(
                "赶时间？跳过追问，直接生成大纲 →",
                disabled=_is_busy(),
                key=f"skip_clarify_{session.round}",
            ):
                _quota_guard()
                _begin_busy("skip")
                ok = False
                try:
                    session.skip_clarify()
                    _store(session)
                    ok = True
                except Exception as exc:
                    st.error(_error_text(exc, "跳过失败，请稍后重试"))
                finally:
                    _end_busy()
                if ok:
                    st.rerun()

        # 聊天输入框：只有「还没有会话」或「有待回答问题」时才允许输入；
        # 追问结束后不再把用户输入当成回答，避免“当前没有待回答的问题”。
        can_chat = session is None or session.pending_question is not None
        if can_chat:
            prompt = st.chat_input("说说你的想法，多离谱都行……")
            if prompt:
                _handle_chat_message(session, prompt)
        elif session and session.phase in (
            config.PHASE_OUTLINE,
            config.PHASE_CONFIRMED,
            config.PHASE_TICKETS,
        ):
            st.success("追问已完成，信息足够，请去「② 大纲确认」或「③ 产品说明书」。")
        elif session:
            st.info("当前没有待回答的问题；如需新项目，请点侧边栏「（新建项目）」。")


    def _handle_chat_message(session: decompose_logic.PMSession | None, text: str) -> None:
        """处理一条聊天消息：首条创建 PMSession，之后作为追问回答。

        输入长度、每日配额、生成锁都在这里统一拦截；成功后全量 rerun，
        让侧边栏进度条与 Header 阶段同步更新。
        """
        content = (text or "").strip()
        if not content:
            return
        if session is not None and session.pending_question is None:
            # 追问已完成/被跳过：不再把新消息当回答，避免 LogicError
            st.info(
                "当前没有待回答的问题，请去「② 大纲确认」或「③ 产品说明书」；"
                "如需新项目，请点侧边栏「（新建项目）」。"
            )
            return
        if session is None and len(content) < REQ_MIN_CHARS:
            st.error(
                f"再多说一点：至少 {REQ_MIN_CHARS} 字（当前 {len(content)} 字）。"
                "补充目标用户和核心功能，AI 才能给出有价值的追问。"
            )
            return
        if len(content) > REQ_MAX_CHARS:
            st.error(f"内容太长了：最多 {REQ_MAX_CHARS} 字，请精简后再发送。")
            return

        _quota_guard()
        _begin_busy("chat")
        ok = False
        with st.status("正在思考……", expanded=True) as status:
            try:
                if session is None:
                    session = decompose_logic.PMSession(
                        requirement=content,
                        session_id=session_id,
                    )
                    session.start(progress=_status_progress(status))
                    st.session_state.pop("restored_from", None)
                else:
                    session.answer(content, progress=_status_progress(status))
                _store(session)
                ok = True
                status.update(label="思考完成", state="complete", expanded=False)
            except Exception as exc:
                # 服务端记录真实异常类型，页面只显示固定友好提示
                print(f"[ui] 聊天处理失败：{type(exc).__name__}: {str(exc)[:160]}")
                status.update(label="处理失败", state="error")
                if isinstance(exc, llm_client.QuotaExceededError):
                    friendly = "系统今日额度已用尽"
                else:
                    friendly = "AI大脑开小差了，请重新输入刚才的需求试试。"
                st.session_state["chat_error"] = friendly
                st.error(friendly)
            finally:
                _end_busy()
        if ok:
            st.rerun()


    # 首次加载：恢复最近项目（不按 session_id 过滤，刷新/换浏览器后不丢失历史任务）
    if "pm_session" not in st.session_state:
        try:
            _recent_projects = project_store.repo.list_projects(limit=1)
        except Exception:
            st.error("数据库连接失败，请联系管理员或稍后重试。")
            st.stop()

        if _recent_projects:
            _restored = decompose_logic.PMSession.from_db_project(_recent_projects[0])
            if _restored:
                st.session_state["pm_session"] = _restored
                st.session_state["restored_from"] = "云端项目"
                # 预填输入框，恢复后能看到原需求与项目名
                st.session_state["requirement_input"] = _restored.requirement
                st.session_state["project_name_input"] = _restored.project_name


    # =============================================================================
    # 三、侧边栏：数据库项目列表 + 运行配置
    # =============================================================================
    with st.sidebar:
        # 状态面板：阶段 + 追问进度 + 运行配置（替代原来的纯文本配置块）
        _sidebar_session = _session()
        _, _sidebar_phase_text, _sidebar_tone = _header_state(_sidebar_session)
        ui_theme.render_status_panel(
            phase_text=_sidebar_phase_text,
            phase_tone=_sidebar_tone,
            round_now=_sidebar_session.round if _sidebar_session else 0,
            round_max=config.MAX_CLARIFY_ROUNDS,
            meta_rows=[
                ("模型", llm_client.primary_model()),
                ("存储", db.db.backend),
                ("连接", db.db.healthcheck()),
                ("API Key", "已配置" if llm_client.is_configured() else "未配置"),
                (
                    "规则库",
                    f"{len(config.USER_PROFILE)} 条偏好 / "
                    f"{sum(len(items) for items in config.TECH_RULES.values())} 条红线",
                ),
            ],
        )

        st.divider()
        st.subheader("项目")
        try:
            # 全局项目列表：访问码是唯一门禁，不再按浏览器会话隔离，
            # 否则关闭标签页 / 换浏览器后旧任务会“消失”。
            _projects = project_store.repo.list_projects(limit=50)
        except Exception:
            st.error("读取项目列表失败，请稍后重试。")
            st.stop()

        # 最近项目放最前并默认选中，“（新建项目）”放最后
        _options = [item["id"] for item in _projects] + ["（新建项目）"]
        _labels = {
            item["id"]: f"{item['name']}（{_PHASE_LABELS.get(item['phase'], item['phase'])}）"
            for item in _projects
        }
        _labels["（新建项目）"] = "（新建项目）"
        selected_project_id = st.selectbox(
            "当前项目",
            _options,
            index=0,
            format_func=lambda pid: _labels.get(pid, pid),
        )
        st.caption("已显示全部历史项目；刷新或换浏览器后不会再丢失。")

        if selected_project_id != "（新建项目）":
            st.divider()
            st.subheader("危险操作")
            confirm_delete = st.checkbox(
                f"我确认删除《{_labels.get(selected_project_id, selected_project_id)}》"
                "及其所有工单和本地文件，删除后不可恢复",
                key=f"delete_confirm_{selected_project_id}",
            )
            if st.button(
                "删除当前项目",
                type="secondary",
                disabled=not confirm_delete or _is_busy(),
                key=f"delete_project_{selected_project_id}",
                use_container_width=True,
            ):
                try:
                    project = project_store.repo.get_project(selected_project_id)
                    project_name = str(project.get("name", "")) if project else ""
                    slug = config.slugify_project_name(project_name) if project_name else ""
                    project_store.repo.delete_project(selected_project_id)
                    if slug:
                        shutil.rmtree(
                            config.project_paths(slug)["project_dir"],
                            ignore_errors=True,
                        )
                    current = _session()
                    if current and current.project_id == selected_project_id:
                        st.session_state.pop("pm_session", None)
                        st.session_state.pop("restored_from", None)
                    st.session_state.pop("tickets", None)
                    st.session_state.pop("tickets_project_id", None)
                    st.success("项目已删除")
                    st.rerun()
                except Exception as exc:
                    print(f"[ui] 删除项目失败：{type(exc).__name__}: {str(exc)[:160]}")
                    st.error("删除失败，请稍后重试。")

        st.caption("一次一问 · 结论先行 · 工单即状态")

    # 顶部自定义 Header：三步进度 + 当前项目 + 阶段徽章（替代原生 st.title/st.caption）
    _header_session = _session()
    _header_step, _header_phase_text, _header_tone = _header_state(_header_session)
    ui_theme.render_header(
        project=(_header_session.project_name if _header_session else "未选择项目"),
        step=_header_step,
        phase_text=_header_phase_text,
        phase_tone=_header_tone,
    )

    # 恢复提示：明确告知「恢复了什么、没恢复什么」，避免以为对话丢了是 Bug
    if st.session_state.get("restored_from") and _session():
        _restored_session = _session()
        st.info(
            f"已从云端恢复项目：{_restored_session.project_name} ｜ "
            f"当前阶段：{_PHASE_LABELS.get(_restored_session.phase, _restored_session.phase)}。"
            "对话记录不存数据库；项目、大纲、工单均已恢复。"
        )

    tab_ask, tab_outline, tab_tickets, tab_bug = st.tabs(
        ["① 需求与追问", "② 大纲确认", "③ 产品说明书", "④ Bug 修复"]
    )

    # =============================================================================
    # ① 需求与追问
    # =============================================================================
    with tab_ask:
        # 对话式主界面：欢迎语、历史消息、快捷选项与 chat_input 都在同一个 fragment 内
        render_chat()

    # =============================================================================
    # ② 大纲确认
    # =============================================================================
    with tab_outline:
        session = _session()
        if not session:
            st.caption("请先在「① 需求与追问」提交需求")
        elif session.outline is None:
            if session.phase == config.PHASE_CLARIFY and session.pending_question is not None:
                st.caption("请先回答当前追问，再生成大纲。")
            elif st.button("生成大纲", type="primary", disabled=_is_busy()):
                _quota_guard()
                _begin_busy("outline")
                ok = False
                with st.status("正在生成 3~4 个模块的大纲……", expanded=True) as status:
                    try:
                        session.build_outline(progress=_status_progress(status))
                        _store(session)
                        ok = True
                        status.update(label="大纲生成完成", state="complete", expanded=False)
                    except Exception as exc:
                        status.update(label="生成失败", state="error")
                        st.error(_error_text(exc, "生成大纲失败，请稍后重试"))
                    finally:
                        _end_busy()
                if ok:
                    st.rerun()
        else:
            outline = session.outline
            st.subheader(f"项目大纲：{outline.project_name}")
            st.write(outline.summary)
            # 双列卡片展示模块；outline_card 已 dedent，不会触发 Markdown 代码块
            module_columns = st.columns(2)
            for index, module in enumerate(outline.modules, 1):
                module_card = {
                    "name": module.name,
                    "goal": module.goal,
                    "kind": module.kind,
                    "depends_on": module.depends_on,
                }
                with module_columns[(index - 1) % 2]:
                    st.markdown(
                        ui_theme.outline_card(index, module_card),
                        unsafe_allow_html=True,
                    )

            if outline.research_needed:
                st.info(f"需要前置调研：{outline.research_goal or '（未写具体目标）'}")

            if not session.outline_confirmed:
                if st.button("确认大纲无误", type="primary", disabled=_is_busy()):
                    _begin_busy("confirm")
                    ok = False
                    with st.spinner("正在写入项目上下文……"):
                        try:
                            session.confirm_outline()
                            _store(session)
                            ok = True
                        except Exception as exc:
                            st.error(_error_text(exc, "确认失败，请稍后重试"))
                        finally:
                            _end_busy()
                    if ok:
                        st.rerun()
            else:
                st.success(
                    f"大纲已确认，项目已写入数据库（{db.db.backend}）"
                )

    # =============================================================================
    # ③ 工单看板（数据库读取 + 内存 ZIP 导出）
    # =============================================================================
    @st.fragment
    def render_ticket_board(project_id: str) -> None:
        """看板优先读数据库；数据库暂不可用时用本次会话的内存工单兜底。"""
        project = None
        tickets: list[dict] = []
        db_error = False
        try:
            project = project_store.repo.get_project(project_id)
            tickets = project_store.repo.list_tickets(project_id)
        except Exception:
            # 云端超时/不可用：不报错，走内存兜底
            db_error = True

        # 只使用与当前项目匹配的内存工单，避免串项目
        memory_tickets = (
            list(st.session_state.get("tickets", []))
            if st.session_state.get("tickets_project_id") == project_id
            else []
        )
        if (db_error or not tickets) and memory_tickets:
            tickets = memory_tickets
            project = project or {"name": st.session_state.get("project_name", "当前项目")}
            st.warning("云端暂存失败，请立即下载 ZIP 保存到本地")

        # 生成时记录的警告只显示一次
        warning = st.session_state.pop("cloud_sync_warning", "")
        if warning:
            st.warning(warning)

        if not project:
            st.caption("项目不存在或已被删除。")
            return

        prd_tickets = [
            item
            for item in tickets
            if item["filename"] == "产品需求说明书.md" or item.get("type") == "prd"
        ]
        display_tickets = prd_tickets or tickets
        st.subheader(f"产品说明书预览：{project['name']}")

        if not display_tickets:
            st.caption("还没有产品说明书，请在上方点击「生成小白专属需求说明书」。")
            return

        # 微信生态兜底：微信内置浏览器经常拦截 ZIP 下载
        st.info(
            "📱 手机用户请注意：在微信内可能无法直接下载 ZIP。"
            "若点击无反应，请点右上角『···』选择『在浏览器打开』。"
        )

        if prd_tickets:
            # V4：直接渲染大白话说明书，不再用技术工单卡片
            for item in prd_tickets:
                st.markdown(item["content_md"])
        else:
            # 旧项目兼容：仍显示历史技术工单
            st.caption("当前项目还是旧版技术工单，建议重新生成产品说明书。")
            for item in tickets:
                with st.expander(item["filename"]):
                    st.markdown(item["content_md"])

        # 下载前免责声明与安全锁：这是说明书，不是成品软件
        st.warning(
            "⚠️ 这是产品说明书，不是成品软件。"
            "请把它交给 WorkBuddy 的【微信小程序开发专家团】生成小程序。"
        )
        agree_download = st.checkbox(
            "我已了解：我拿到的是产品说明书，需要交给 WorkBuddy 生成小程序",
            key=f"download_agree_{project_id}",
        )

        zip_bytes = b""
        zip_name = f"{project.get('name', 'project')}_产品说明书.zip".replace(" ", "-")
        if display_tickets:
            try:
                zip_bytes, zip_name = export_service.build_zip_from_tickets(
                    display_tickets, project.get("name", "project")
                )
            except export_service.ExportError:
                zip_bytes = b""

        zip_ready = bool(display_tickets) and len(zip_bytes) >= 100
        download_ready = zip_ready and agree_download
        st.download_button(
            "📥 下载产品说明书 (.zip)",
            data=zip_bytes if download_ready else b"",
            file_name=zip_name,
            mime="application/zip",
            key=f"download_zip_{project_id}",
            use_container_width=True,
            disabled=not download_ready,
        )
        if not zip_ready:
            st.caption("暂无可下载的说明书")
        elif not agree_download:
            st.caption("请先勾选上方确认项，再下载。")

        with st.expander("👇 若无法下载，请直接在此复制说明书全文"):
            for item in display_tickets:
                st.markdown(f"**{item['filename']}**")
                st.code(item["content_md"], language="markdown")

        # 点击后 fragment 自身重跑，完成刷新，不触发整页重算
        st.button("刷新列表", key=f"refresh_tickets_{project_id}")


    with tab_tickets:
        session = _session()
        active_project_id = _active_project_id(selected_project_id)

        if session and session.outline_confirmed and session.project_id == active_project_id:
            # V4：只生成一份产品说明书；已存在时允许覆盖重生成
            try:
                project_tickets = project_store.repo.list_tickets(active_project_id)
            except Exception:
                project_tickets = []
            prd_exists = any(
                item["filename"] == "产品需求说明书.md" or item.get("type") == "prd"
                for item in project_tickets
            )
            if prd_exists:
                st.info("已生成过产品说明书；重新生成会覆盖旧版本。")
            button_label = "重新生成产品说明书" if prd_exists else "生成小白专属需求说明书"
            if st.button(
                button_label,
                type="primary",
                disabled=_is_busy(),
                key="generate_prd_btn",
            ):
                _quota_guard()
                _begin_busy("prd")
                ok = False
                with st.status("正在把你的想法翻译成产品说明书...", expanded=True) as status:
                    try:
                        path = session.generate_prd(progress=_status_progress(status))
                        _store(session)
                        # 内存兜底：数据库超时也能下载
                        st.session_state["tickets"] = list(session.generated_ticket_data)
                        st.session_state["tickets_project_id"] = session.project_id
                        if session.db_sync_failed:
                            st.session_state["cloud_sync_warning"] = (
                                "云端暂存失败，请立即下载说明书保存到本地"
                            )
                        status.update(label="产品说明书生成完成", state="complete", expanded=False)
                        ok = True
                    except Exception as exc:
                        status.update(label="生成失败", state="error")
                        st.error(_error_text(exc, "生成失败，请稍后重试"))
                    finally:
                        _end_busy()
                if ok:
                    st.rerun()
        elif session and not session.outline_confirmed:
            st.caption("请先在「② 大纲确认」点击「确认大纲无误」。")

        if not active_project_id:
            st.caption("还没有选择项目。")
        else:
            render_ticket_board(active_project_id)

    # =============================================================================
    # ④ Bug 修复（项目上下文从数据库读取）
    # =============================================================================
    with tab_bug:
        active_project_id = _active_project_id(selected_project_id)

        if not active_project_id:
            st.caption("请先选择或创建一个项目。")
        else:
            log_text = st.text_area(
                "报错日志（直接从 IDE 复制）",
                height=200,
                key="bug_log_input",
                placeholder="粘贴 Trae / CodeBuddy 里的完整报错堆栈……",
            )
            if st.button("生成 Fix 工单", type="primary", disabled=_is_busy()):
                if not log_text.strip():
                    st.warning("请先粘贴报错日志")
                else:
                    _quota_guard()
                    _begin_busy("fix")
                    ok = False
                    with st.status("正在结合项目上下文生成修复工单……", expanded=True) as status:
                        try:
                            slug = _project_slug(active_project_id) or active_project_id
                            path = decompose_logic.generate_fix_ticket(
                                slug,
                                log_text,
                                project_id=active_project_id,
                                progress=_status_progress(status),
                            )
                            status.update(label="Fix 工单生成完成", state="complete", expanded=False)
                            st.success(f"已生成：{path.name}")
                            st.markdown(path.read_text(encoding="utf-8", errors="replace"))
                            ok = True
                        except Exception as exc:
                            status.update(label="生成失败", state="error")
                            st.error(_error_text(exc, "生成失败，请稍后重试"))
                        finally:
                            _end_busy()
                    if ok:
                        st.rerun()

except Exception:
    st.error("系统当前繁忙，请刷新页面重试。")
