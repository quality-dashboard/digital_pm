# -*- coding: utf-8 -*-
"""
llm_client.py —— 阿里云百炼（DashScope）调用封装

两个入口：
- chat_json：需要结构化结果（追问、大纲），强制 JSON 模式并解析；
- chat_text：需要 Markdown 正文（工单、Fix 工单），直接返回文本。

关键约定：
- 每次调用都由 config.build_system_prompt() 把「协作偏好 + 技术红线」拼在
  System Prompt 最前面，调用方不需要、也不允许绕过；
- 密钥只从环境变量读取，不打印、不落盘、不进入异常文本；
- httpx 延迟导入：没装第三方包时，UI 仍能启动并给出明确安装提示。
"""
from __future__ import annotations

import json
import os
import re
import time

import config


class LLMError(RuntimeError):
    """百炼调用或解析失败的统一异常。"""


class MissingAPIKeyError(LLMError):
    """未配置 DASHSCOPE_API_KEY 时抛出。"""


def get_api_key() -> str:
    """读取百炼密钥：环境变量优先，其次 Streamlit Secrets（云端部署）。"""
    return config.read_setting(config.DASHSCOPE_API_KEY_ENV)


def is_configured() -> bool:
    """是否具备调用千问的条件；UI 只显示「已配置 / 未配置」，不显示值。"""
    return bool(get_api_key())


def _post(system_prompt: str, user_content: str, json_mode: bool) -> str:
    """发送一次 chat/completions 请求，返回助手文本。"""
    try:
        import httpx
    except ImportError as exc:
        raise LLMError("缺少 httpx，请先执行：python -m pip install -r requirements.txt") from exc

    payload: dict = {
        "model": config.DASHSCOPE_MODEL,
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
    except Exception as exc:  # 超时、DNS、连接重置等统一转换
        raise LLMError(f"连接百炼失败：{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        # 只回显前 300 字符，避免把完整响应（可能含敏感回显）打进日志
        raise LLMError(f"百炼返回 HTTP {response.status_code}：{response.text[:300]}")

    try:
        body = response.json()
        return body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"百炼返回结构异常：{str(response.text)[:300]}") from exc


def _call(system_prompt: str, user_content: str, json_mode: bool) -> str:
    """带重试与 response_format 兼容降级的统一调用。"""
    if not is_configured():
        raise MissingAPIKeyError(
            f"未配置 {config.DASHSCOPE_API_KEY_ENV}，无法调用千问。"
            "请在 .env 或环境变量中填写百炼密钥后重启页面。"
        )

    last_error: Exception | None = None
    disable_json_mode = False
    for attempt in range(1, config.LLM_MAX_ATTEMPTS + 1):
        try:
            return _post(
                system_prompt,
                user_content,
                json_mode=json_mode and not disable_json_mode,
            )
        except LLMError as exc:
            last_error = exc
            # 部分网关不支持 response_format：收到 400 后自动去掉参数，靠提示词约束 JSON
            if json_mode and "HTTP 400" in str(exc):
                disable_json_mode = True
            if attempt < config.LLM_MAX_ATTEMPTS:
                time.sleep(min(1.0 * attempt, 3.0))

    raise LLMError(f"百炼调用失败（共 {config.LLM_MAX_ATTEMPTS} 次）：{last_error}")


def _extract_json(text: str) -> dict:
    """从模型文本中提取 JSON：兼容 markdown 围栏与前后多余说明。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"模型输出中未找到 JSON：{cleaned[:200]}")

    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型输出 JSON 顶层必须是对象")
    return data


def chat_json(system_prompt: str, user_content: str) -> dict:
    """要求模型返回 JSON 对象（追问 / 大纲阶段使用）。"""
    return _extract_json(_call(system_prompt, user_content, json_mode=True))


def chat_text(system_prompt: str, user_content: str) -> str:
    """要求模型返回 Markdown 正文（工单 / Fix 工单使用）。"""
    text = _call(system_prompt, user_content, json_mode=False).strip()
    if not text:
        raise LLMError("模型返回内容为空")
    if len(text) > config.MAX_TICKET_CHARS:
        # 截断只是兜底保护；正常工单应远小于上限
        text = text[: config.MAX_TICKET_CHARS] + "\n\n> （内容超出上限，已截断）"
    return text
