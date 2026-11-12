# -*- coding: utf-8 -*-
"""
llm_client.py —— 阿里云百炼（DashScope）调用封装（V3.3.0 多模型兜底）

两个入口：
- chat_json：需要结构化结果（追问、大纲），强制 JSON 模式并解析；
- chat_text：需要 Markdown 正文（工单、Fix 工单），直接返回文本。

多模型兜底：
- 模型池按 qwen-plus -> qwen-turbo -> qwen-max 顺序尝试；
- 任一模型遇到 429/5xx/网络异常，sleep 1 秒后切下一个，避免用户卡死；
- 全部失败只返回固定中文提示，页面不暴露底层异常与路径。

安全约定：
- 密钥只从环境变量 / Streamlit Secrets 读取，不打印、不落盘、不进异常文本；
- 页面可见的异常 message 一律是中文短提示，详细原因只写服务端日志。
"""
from __future__ import annotations

import json
import re
import time

import config

# 模型池：Plus 性价比最高；失败后切 Turbo；最后 Max 兜底。
# 如果环境变量 DASHSCOPE_MODEL 显式配置了其他模型，会放在池子最前面。
# 2026-11-12 实测可用模型（按业务优先级排序）：
# 已剔除额度过期的 qwen-plus / qwen-turbo / qwen-max 等旧模型，
# 避免每次请求都先撞一次失败再切换。
MODELS: tuple[str, ...] = (
    "qwen3.7-plus",
    "qwen-plus-2025-11-05",
    "qwen3.8-27b",
    "qwen3.8-flash",
    "deepseek-v4.1-flash",
    "glm-5.3-prime",
    "glm-5.3",
    "kimi-k3.1-pro",
    "glm-5.2-fast-preview",
    "deepseek-v4.1-flash-1017",
    "deepseek-r1-distill-qwen-1.5b",
    "qwen-deep-search-planning",
)

# 页面统一展示的友好提示：不包含 HTTP 状态码、路径、堆栈
FRIENDLY_ERROR = "AI 服务当前繁忙，请稍后重试"


class LLMError(RuntimeError):
    """百炼调用或解析失败的统一异常（message 必须是可展示的中文提示）。"""


class MissingAPIKeyError(LLMError):
    """未配置 DASHSCOPE_API_KEY 时抛出。"""


def get_api_key() -> str:
    """读取百炼密钥：环境变量优先，其次 Streamlit Secrets（云端部署）。"""
    return config.read_setting(config.DASHSCOPE_API_KEY_ENV)


def is_configured() -> bool:
    """是否具备调用千问的条件；UI 只显示「已配置 / 未配置」，不显示值。"""
    return bool(get_api_key())


def _model_pool() -> list[str]:
    """模型池：仅在用户配置的模型属于可用池时优先，否则按 MODELS 顺序。

    这样即使 .env / Secrets 里还留着已过期的 qwen-plus，也不会每次先撞一次
    失败再切换，直接使用探测出的可用模型。
    """
    configured = (config.DASHSCOPE_MODEL or "").strip()
    ordered: list[str] = [configured] if configured and configured in MODELS else []
    ordered.extend(model for model in MODELS if model not in ordered)
    return ordered


def primary_model() -> str:
    """返回当前实际优先使用的模型，供 UI 展示。"""
    return _model_pool()[0]


def _post(system_prompt: str, user_content: str, json_mode: bool, model: str) -> str:
    """用指定模型发送一次 chat/completions 请求，返回助手文本。"""
    try:
        import httpx
    except ImportError:
        raise LLMError("AI 服务组件缺失，请联系管理员") from None

    payload: dict = {
        "model": model,
        "messages": [
            # 规则注入在这里发生：每次请求都强制带上协作偏好与技术红线
            {"role": "system", "content": config.build_system_prompt(system_prompt)},
            {"role": "user", "content": user_content},
        ],
        "temperature": config.LLM_TEMPERATURE,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    url = f"{config.DASHSCOPE_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
            response = client.post(url, headers=headers, json=payload)
            # 兼容不支持 response_format 的网关：400 时去掉该参数重试一次
            if response.status_code == 400 and json_mode:
                payload.pop("response_format", None)
                response = client.post(url, headers=headers, json=payload)
    except Exception as exc:
        # 内部细节只写服务端日志，不进入页面
        print(f"[llm] {model} 网络异常：{type(exc).__name__}: {str(exc)[:160]}")
        raise LLMError("连接 AI 服务失败") from None

    if response.status_code >= 400:
        print(f"[llm] {model} HTTP {response.status_code}：{response.text[:200]}")
        raise LLMError("AI 服务返回错误")

    try:
        body = response.json()
        return body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"[llm] {model} 返回结构异常：{type(exc).__name__}")
        raise LLMError("AI 返回结构异常") from None


def _call(system_prompt: str, user_content: str, json_mode: bool) -> str:
    """带多模型 fallback 的统一调用：任一模型失败，sleep(1) 后切下一个。"""
    if not is_configured():
        raise MissingAPIKeyError("未配置 AI 密钥，请联系管理员")

    last_error: Exception | None = None
    for model in _model_pool():
        try:
            return _post(system_prompt, user_content, json_mode, model)
        except LLMError as exc:
            last_error = exc
            print(f"[llm] {model} 失败，切换下一个模型：{str(exc)[:120]}")
            time.sleep(1)  # 按产品经理要求：失败后等待 1 秒再切

    # 所有模型都失败：只给固定提示，绝不把底层异常/路径抛给页面
    print(f"[llm] 模型池全部失败：{type(last_error).__name__ if last_error else 'unknown'}")
    raise LLMError(FRIENDLY_ERROR)


def _extract_json(text: str) -> dict:
    """从模型文本中提取 JSON：兼容 markdown 围栏与前后多余说明。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("AI 返回格式异常")

    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        raise LLMError("AI 返回格式异常") from None
    if not isinstance(data, dict):
        raise LLMError("AI 返回格式异常")
    return data


def chat_json(system_prompt: str, user_content: str) -> dict:
    """要求模型返回 JSON 对象（追问 / 大纲阶段使用）。"""
    text = _call(system_prompt, user_content, json_mode=True)
    return _extract_json(text)


def chat_text(system_prompt: str, user_content: str) -> str:
    """要求模型返回 Markdown 正文（工单 / Fix 工单使用）。"""
    text = _call(system_prompt, user_content, json_mode=False).strip()
    if not text:
        raise LLMError("AI 返回内容为空，请重试")
    if len(text) > config.MAX_TICKET_CHARS:
        # 截断只是兜底保护；正常工单应远小于上限
        text = text[: config.MAX_TICKET_CHARS] + "\n\n> （内容超出上限，已截断）"
    return text
