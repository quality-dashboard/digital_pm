# -*- coding: utf-8 -*-
"""
config.py —— V5.0「数字项目经理」全局配置与规则库

本文件只做四件事：
1. 定义协作偏好 USER_PROFILE（产品经理的沟通与决策习惯）；
2. 定义技术红线 TECH_RULES（IDE Agent 与工单必须遵守的交付规范）；
3. 提供 build_system_prompt()，把上面两段规则强制拼在每次 LLM 调用的最前面；
4. 集中管理项目路径、百炼接口参数、追问轮数与状态机常量。

设计原则：
- 只依赖 Python 标准库，配置本身不需要安装任何第三方包；
- 路径、模型、轮数都可用环境变量覆盖，代码里不写死；
- 规则注入由代码完成，不依赖模型「自觉记住」。
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from pathlib import Path

# =============================================================================
# 零、可选加载项目根目录的 .env（零第三方依赖）
# =============================================================================
PM_ROOT: Path = Path(__file__).resolve().parent


def load_env_file() -> int:
    """读取 PM_ROOT/.env，把 KEY=VALUE 写入进程环境变量，返回成功加载的条数。

    规则（按产品经理的明确要求）：
    - .env 优先：同名键一律覆盖进程环境变量，避免系统里残留的旧值/空值干扰；
    - 值为空的行直接跳过，不写入空字符串（否则会被误判为「已配置」）；
    - 可重复调用：Streamlit 每次重跑都会重新读取，改完 .env 不必重启进程。
    """
    env_file = PM_ROOT / ".env"
    if not env_file.exists():
        return 0

    loaded = 0
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 跳过空行、注释行与没有等号的非法行
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue  # 空值不写入，避免覆盖成空密钥
        os.environ[key] = value  # .env 优先
        loaded += 1
    return loaded


# 模块导入时先加载一次；app_ui.py 每次重跑还会再调用，确保修改 .env 后立即生效
load_env_file()


# =============================================================================
# 零·二、配置双轨读取（V3.2.0：本地 .env / 云端 Streamlit Secrets）
# =============================================================================
_SECRETS_CACHE: dict[str, str] | None = None


def _streamlit_secrets() -> dict[str, str]:
    """一次性读取 Streamlit Secrets 并缓存。

    避免每个配置项都去碰 st.secrets：那会重复触发 secrets 文件加载，
    在本地没有 secrets.toml 时还会重复打印 "No secrets found" 警告。
    """
    global _SECRETS_CACHE
    if _SECRETS_CACHE is not None:
        return _SECRETS_CACHE

    try:
        import streamlit as st
        # 非 Streamlit 运行时（命令行测试）不访问 secrets，避免无意义异常
        if not st.runtime.exists():
            _SECRETS_CACHE = {}
            return _SECRETS_CACHE
        _SECRETS_CACHE = {str(k): str(v) for k, v in dict(st.secrets).items()}
    except Exception:
        # 本地没有 secrets.toml、或 secrets 结构异常：按空配置处理
        _SECRETS_CACHE = {}
    return _SECRETS_CACHE


def _read_streamlit_secret(key: str) -> str:
    """从缓存的 Streamlit Secrets 读取单个配置项。"""
    return _streamlit_secrets().get(key, "").strip()


def read_setting(key: str, default: str = "") -> str:
    """双轨读取配置：环境变量 > Streamlit Secrets > 默认值。

    - 本地：.env 由 load_env_file() 写入 os.environ，命中第一档；
    - 云端：Secrets 面板注入，命中第二档；
    - 两处都没有：用默认值，保证页面能启动并给出明确配置提示。
    """
    value = os.getenv(key, "").strip()
    if value:
        return value
    return _read_streamlit_secret(key) or default


# =============================================================================
# 一、协作偏好 USER_PROFILE（每次 LLM 调用都会拼在最前面）
# =============================================================================
# 这不是「给模型看的建议」，而是代码层每次强制注入的硬规则。
USER_PROFILE: dict[str, str] = {
    "我的角色": "产品经理 / 验收官。我不写代码，只验收功能、UI 与逻辑。",
    "沟通风格": "极简、结论先行。先给一句话结论，再给分点、表格或清单。",
    "沟通节奏": "一次只问一个最关键的问题；我回答后你再继续，禁止一口气抛出多个问题。",
    "决策方式": "需要我拍板时，给 2~3 个选项，标注推荐项与一句话理由，不要替我做决定。",
    "验收要求": "每个任务都要给我「产品经理可执行」的验收步骤：点哪个按钮、输入什么数据、看到什么状态。",
    "输出边界": "对话里不要甩大段实现代码；工单面向 IDE Agent，可以写接口与约束。",
}

# =============================================================================
# 二、技术红线 TECH_RULES（IDE Agent 与工单都必须遵守）
# =============================================================================
TECH_RULES: dict[str, tuple[str, ...]] = {
    "技术栈": (
        "Python 3.10+；UI 只用 Streamlit，不引入第二套前端框架",
        "大模型统一走阿里云百炼 OpenAI 兼容接口（默认 qwen-plus）",
        "状态只落本地 Markdown / JSON 文件，不使用数据库、消息队列",
        "禁止引入 LangChain、AutoGen 等重型 Agent 框架",
    ),
    "代码规范": (
        "按职责拆分模块，单个文件不超过 400 行",
        "所有代码必须带详细中文注释，关键分支说明「为什么」",
        "新增代码不得破坏既有逻辑；确需改动先说明影响面",
        "禁止提交密钥、.env、本机凭据",
    ),
    "工单规范": (
        "每张工单必须包含：上下文边界、核心需求、验收清单、依赖关系",
        "验收清单必须写成产品经理可执行的步骤：点击 / 输入 / 验证状态流转",
        "跨工单只传依赖文件名，不复制其他工单全文（上下文强隔离）",
        "工单必须标注建议执行平台（Trae / CodeBuddy / WorkBuddy）",
    ),
    "安全边界": (
        "只允许改动工单指定范围内的文件；工单目录之外一律不动",
        "敏感文件（.env、密钥、凭据）只读不写",
        "任何自动执行能力必须显式开关 + 命令白名单，默认关闭",
    ),
}

_SECTION_LABELS: dict[str, str] = {
    "技术栈": "技术栈",
    "代码规范": "代码规范",
    "工单规范": "工单规范",
    "安全边界": "安全边界",
}


def build_system_prompt(task_prompt: str) -> str:
    """把协作偏好 + 技术红线拼在任务提示词最前面（模块一硬性要求）。

    参数 task_prompt：本次具体任务的系统提示词（追问 / 大纲 / 工单 / Fix）。
    返回：可直接作为 system content 传给百炼的完整规则前缀。
    """
    profile_lines = [f"- {key}：{value}" for key, value in USER_PROFILE.items()]
    rule_lines: list[str] = []
    for section, items in TECH_RULES.items():
        rule_lines.append(f"【{_SECTION_LABELS.get(section, section)}】")
        rule_lines.extend(f"- {item}" for item in items)

    return "\n".join(
        [
            "===== 一、协作对象与协作偏好（最高优先级，必须遵守）=====",
            *profile_lines,
            "",
            "===== 二、技术栈与交付红线（必须遵守）=====",
            *rule_lines,
            "",
            "===== 三、本次任务 =====",
            task_prompt.strip(),
        ]
    )


# =============================================================================
# 三、项目与工单路径
# =============================================================================
# 代码目录与项目数据目录分离：删代码不影响工单，清理工单不动代码。
# 默认相对路径 ./data/projects：云端容器没有 D 盘，相对路径随启动目录解析；
# 本地可用 .env 的 PM_PROJECTS_ROOT=D:\projects 覆盖，不影响现有数据。
PROJECTS_ROOT: Path = Path(read_setting("PM_PROJECTS_ROOT", "./data/projects"))
TASKS_DIR_NAME: str = ".tasks"          # 工单目录：{PROJECTS_ROOT}/{项目名}/.tasks/
HISTORY_DIR_NAME: str = ".history"      # 对话与确认留痕，排查用
PROJECT_META_FILE: str = "project.md"   # 项目级上下文：需求 / 大纲 / 规则快照

# 追问阶段项目名还没确定，状态快照先写到 _drafts 目录；
# 大纲生成、项目目录创建后，后续快照统一写项目自己的 .history/session.jsonl。
DRAFTS_DIR_NAME: str = "_drafts"
DRAFT_STATE_FILE: str = "session.jsonl"

# 工单命名规则：01_research_xxx.md / 02_module_xxx.md / fix_01.md
TICKET_NUMBER_WIDTH: int = 2
RESEARCH_PREFIX: str = "research"
MODULE_PREFIX: str = "module"
FIX_PREFIX: str = "fix"

# Windows 文件名非法字符：统一替换成 '-'，避免建目录失败
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify_project_name(name: str) -> str:
    """把用户输入的项目名转成合法、可读的 Windows 目录名。

    - 保留中文（Windows 支持，产品经理一眼能认）；
    - 非法字符替换为 '-'，连续空白压成 '-'；
    - 空名用时间戳兜底，最长 40 字符，避免路径过长。
    """
    slug = _UNSAFE_FILENAME_CHARS.sub("-", str(name).strip())
    slug = re.sub(r"\s+", "-", slug).strip(".-")
    if not slug:
        slug = datetime.now().strftime("project-%Y%m%d-%H%M%S")
    return slug[:40]


def project_paths(project_slug: str) -> dict[str, Path]:
    """返回一个项目的标准路径集合（纯计算，不创建目录）。"""
    project_dir = PROJECTS_ROOT / project_slug
    return {
        "project_dir": project_dir,
        "tasks_dir": project_dir / TASKS_DIR_NAME,
        "history_dir": project_dir / HISTORY_DIR_NAME,
        "meta_file": project_dir / PROJECT_META_FILE,
    }


def ensure_project_dirs(project_slug: str) -> dict[str, Path]:
    """幂等创建项目目录、.tasks 与 .history，返回路径集合。"""
    paths = project_paths(project_slug)
    for key in ("project_dir", "tasks_dir", "history_dir"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


# =============================================================================
# 四、阿里云百炼（DashScope）配置
# =============================================================================
DASHSCOPE_API_KEY_ENV: str = "DASHSCOPE_API_KEY"
DASHSCOPE_BASE_URL: str = read_setting(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
# V3.3.4 欠费保护：只用最基础的经典模型，避免变种/第三方模型产生额外费用。
# 注意：账号欠费或免费额度耗尽时会直接提示「系统今日额度已用尽」，不再自动切换模型。
LLM_MODELS: tuple[str, ...] = ("qwen-turbo", "qwen-plus")
DASHSCOPE_MODEL: str = read_setting("DASHSCOPE_MODEL", "qwen-turbo")
# 单次请求最大输出 token：严格封顶，防止模型输出过长导致费用失控
LLM_MAX_OUTPUT_TOKENS: int = 2000


def _env_number(name: str, default: str, cast):
    """安全解析数值型环境变量：非法值回落默认值，不让页面启动即崩。"""
    try:
        return cast(os.getenv(name, default))
    except (TypeError, ValueError):
        return cast(default)


LLM_TIMEOUT = _env_number("LLM_TIMEOUT", "90", float)                 # 单次请求超时（秒）
LLM_MAX_ATTEMPTS = max(1, int(_env_number("LLM_MAX_ATTEMPTS", "2", int)))  # 含首次在内的总尝试次数
LLM_TEMPERATURE = _env_number("LLM_TEMPERATURE", "0.3", float)        # 拆解需要稳定输出
MAX_TICKET_CHARS: int = 12000     # 单张工单文本上限，超出截断并提示

# =============================================================================
# 五、交互节奏与拆解约束
# =============================================================================
MAX_CLARIFY_ROUNDS: int = 3        # 最多追问 3 轮，防止把产品经理问烦
MIN_OUTLINE_MODULES: int = 3       # 大纲最少 3 个模块
MAX_OUTLINE_MODULES: int = 4       # 大纲最多 4 个模块
MAX_TICKETS_PER_PROJECT: int = 12  # 单项目工单上限，避免失控

# 工单建议执行平台：告诉产品经理这张工单该丢给谁
EXECUTION_HINTS: dict[str, str] = {
    "research": "任意联网搜研窗口（Trae / CodeBuddy 均可）",
    "frontend": "Trae / CodeBuddy 窗口",
    "backend": "Trae / CodeBuddy 窗口",
    "doc": "WorkBuddy 的『文案撰写』或『软件工坊』专家团",
    "fix": "Trae / CodeBuddy 窗口（建议新开会话，避免上下文污染）",
}
DEFAULT_EXECUTION_HINT: str = "Trae / CodeBuddy 窗口"


def execution_hint(kind: str) -> str:
    """按工单类型返回建议执行平台文案；未登记类型返回默认值。"""
    return EXECUTION_HINTS.get(kind, DEFAULT_EXECUTION_HINT)


# =============================================================================
# 六、三阶段状态机常量（禁止在 UI 里写裸字符串）
# =============================================================================
PHASE_CLARIFY: str = "clarify"      # 追问中：一次一问，等待回答
PHASE_OUTLINE: str = "outline"      # 大纲已生成：等待确认
PHASE_CONFIRMED: str = "confirmed"  # 大纲已确认：允许生成工单
PHASE_TICKETS: str = "tickets"      # 工单已生成：进入看板
PHASE_BUG: str = "bug"              # 报错回流：只新增 fix 工单，不改变主阶段

# =============================================================================
# 七、V3.1.1 云数据库配置（Turso / 本地 SQLite 自动降级）
# =============================================================================
# 上线到 Streamlit Cloud 时在 Secrets / 环境变量里配置：
#   TURSO_DATABASE_URL = libsql://<你的库>.turso.io
#   TURSO_AUTH_TOKEN   = <Turso 控制台生成的 token>
# 未配置 URL 时自动降级到本地 SQLite，便于本地测试；token 只读、不打印。
TURSO_DATABASE_URL: str = read_setting("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN: str = read_setting("TURSO_AUTH_TOKEN")

# 本地降级数据库文件：代码目录/data/local.db（云端部署该文件不持久，仅本地开发用）
LOCAL_DB_PATH: Path = PM_ROOT / "data" / "local.db"
# 当前后端：有 Turso URL 用云端，否则本地 SQLite；UI 可据此显示连接状态
DB_BACKEND: str = "turso" if TURSO_DATABASE_URL else "sqlite"


def new_session_id() -> str:
    """生成网页访客唯一标识，用于隔离不同访客的项目与工单数据。

    用户首次输入需求时由 UI 调用并写入 st.session_state；
    同一浏览器会话刷新保持不变，新访客会得到新的 id。
    """
    return uuid.uuid4().hex


# =============================================================================
# 八、V3.1.1 公网安全门（极简访问码，防君子不防小人）
# =============================================================================
# 不是账号体系，只用于阻止陌生人随手打开体验版；真正的多租户鉴权后续再做。
# 可在 Streamlit Cloud Secrets 或 .env 中通过 APP_ACCESS_CODE 覆盖默认值。
APP_ACCESS_CODE: str = read_setting("APP_ACCESS_CODE", "8888") or "8888"
