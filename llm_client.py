# -*- coding: utf-8 -*-
"""
llm_client.py —— 阿里云百炼（DashScope）调用封装（V3.3.0 多模型兜底）

两个入口：
- chat_json：需要结构化结果（追问、大纲），强制 JSON 模式并解析；
- chat_text：需要 Markdown 正文（工单、Fix 工单），直接返回文本。

多模型兜底：
- 模型池按 config.LLM_MODELS 顺序尝试；
- 任一模型遇到 429/5xx/网络异常/超时，sleep 1 秒后切下一个，避免用户卡死；
- 每次「尝试模型 / 超时切换」都通过 progress 回调同步播报给 UI（st.status），
  页面不会长时间只有转圈没有文字；
- 全部失败只返回固定中文提示，页面不暴露底层异常与路径。

安全约定：
- 密钥只从环境变量 / Streamlit Secrets 读取，不打印、不落盘、不进异常文本；
- 页面可见的异常 message 一律是中文短提示，详细原因只写服务端日志。
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import config

# 模型池：按 config.LLM_MODELS 顺序兜底。
# 如果环境变量 DASHSCOPE_MODEL 显式配置了其他模型，会放在池子最前面。
# 欠费保护：模型池统一由 config.LLM_MODELS 管理，只保留最基础的经典模型。
MODELS: tuple[str, ...] = config.LLM_MODELS

# 页面统一展示的友好提示：不包含 HTTP 状态码、路径、堆栈
FRIENDLY_ERROR = "AI 服务当前繁忙，请稍后重试"

# 进度旁白回调：由 UI 传入（例如更新 st.status），LLM 层只负责同步喊话
ProgressFn = Callable[[str], None]


class LLMError(RuntimeError):
    """百炼调用或解析失败的统一异常（message 必须是可展示的中文提示）。"""


class MissingAPIKeyError(LLMError):
    """未配置 DASHSCOPE_API_KEY 时抛出。"""


class QuotaExceededError(LLMError):
    """402/403：欠费或额度耗尽，必须立即停止请求，避免继续产生费用。"""


class LLMTimeoutError(LLMError):
    """单次请求超时：单独成类，旁白才能明确说「超时」而不是笼统的失败。"""


def _notify(progress: ProgressFn | None, message: str) -> None:
    """安全触发进度旁白：UI 回调出错绝不能中断已经发出的大模型请求。"""
    if not progress:
        return
    try:
        progress(message)
    except Exception as exc:
        # 只记类型，不打印内容，避免页面已断开时日志刷屏
        print(f"[llm] 进度回调异常（已忽略）：{type(exc).__name__}")


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

    system_text = config.build_system_prompt(system_prompt)
    # 排查用：打印实际请求长度，判断是否接近模型 Token 上限
    print(
        f"[llm] {model} 请求长度：system={len(system_text)} 字符 / "
        f"user={len(user_content)} 字符 / max_tokens={config.LLM_MAX_OUTPUT_TOKENS}"
    )
    payload: dict = {
        "model": model,
        "messages": [
            # 规则注入在这里发生：每次请求都强制带上协作偏好与技术红线
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ],
        "temperature": config.LLM_TEMPERATURE,
        # 严格封顶输出 token，防止模型输出过长导致费用失控
        "max_tokens": config.LLM_MAX_OUTPUT_TOKENS,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    url = f"{config.DASHSCOPE_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }

    try:
        # 建连超时单独收紧（默认 10s）：网络不通时快速失败并切备用模型，
        # 读超时仍用 LLM_TIMEOUT（默认 90s），给正常长文本生成留足时间。
        timeout = httpx.Timeout(config.LLM_TIMEOUT, connect=config.LLM_CONNECT_TIMEOUT)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            # 兼容不支持 response_format 的网关：400 时去掉该参数重试一次
            if response.status_code == 400 and json_mode:
                payload.pop("response_format", None)
                response = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        # 超时单独抛出：上层据此播报「主模型超时，正在切换备用模型」
        print(f"[llm] {model} 响应超时（>{config.LLM_TIMEOUT:.0f}s）")
        raise LLMTimeoutError("模型响应超时") from None
    except Exception as exc:
        # 内部细节只写服务端日志，不进入页面
        print(f"[llm] {model} 网络异常：{type(exc).__name__}: {str(exc)[:160]}")
        raise LLMError("连接 AI 服务失败") from None

    if response.status_code == 429:
        print(f"[llm] {model} 触发 429 限流，等待 2 秒后重试一次")
        time.sleep(2)
        try:
            timeout = httpx.Timeout(
                config.LLM_TIMEOUT, connect=config.LLM_CONNECT_TIMEOUT
            )
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=payload)
        except Exception as exc:
            print(f"[llm] {model} 429 重试网络异常：{type(exc).__name__}: {str(exc)[:160]}")
            raise LLMError("连接 AI 服务失败") from None

    if response.status_code in (402, 403):
        print(f"[llm] {model} 返回 HTTP {response.status_code}，疑似欠费/额度耗尽，停止请求")
        raise QuotaExceededError("系统今日额度已用尽")

    if response.status_code >= 400:
        print(f"[llm] {model} HTTP {response.status_code}：{response.text[:200]}")
        raise LLMError("AI 服务返回错误")

    try:
        body = response.json()
        message = body["choices"][0]["message"]
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "")
        print(
            f"[llm] {model} 返回：content={len(content)} 字符 / "
            f"reasoning={len(reasoning)} 字符"
        )
        if not content.strip() and reasoning:
            print(
                f"[llm] {model} 正文为空但思考内容 {len(reasoning)} 字符，"
                "可能是 max_tokens 被思考过程耗尽"
            )
        return content
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"[llm] {model} 返回结构异常：{type(exc).__name__}")
        raise LLMError("AI 返回结构异常") from None


def _call(
    system_prompt: str,
    user_content: str,
    json_mode: bool,
    progress: ProgressFn | None = None,
) -> str:
    """带多模型 fallback 的统一调用：任一模型失败，sleep(1) 后切下一个。

    progress：进度旁白回调。每次尝试前、超时切换时都会同步喊话，
    UI 用 st.status 接住即可看到「正在试主模型 → 超时 → 切备用模型」全过程。
    """
    if not is_configured():
        raise MissingAPIKeyError("未配置 AI 密钥，请联系管理员")

    models = _model_pool()
    total = len(models)
    last_error: Exception | None = None

    for index, model in enumerate(models, start=1):
        role = "主模型" if index == 1 else f"备用模型 {index - 1}"
        _notify(progress, f"正在尝试{role} {model}（{index}/{total}）…")
        started = time.monotonic()

        try:
            text = _post(system_prompt, user_content, json_mode, model)
            if index > 1:
                # 用户需要知道「刚才慢是因为切过模型」，而不是以为一切正常
                _notify(progress, f"{role} {model} 已恢复响应，继续生成…")
            return text
        except QuotaExceededError:
            # 欠费/额度耗尽：立即停止，不再尝试其他模型，避免继续请求
            raise
        except LLMError as exc:
            last_error = exc
            waited = time.monotonic() - started
            reason = "响应超时" if isinstance(exc, LLMTimeoutError) else "暂时不可用"
            print(f"[llm] {model} 失败（{waited:.0f}s，{reason}）：{str(exc)[:120]}")
            if index < total:
                _notify(
                    progress,
                    f"{role} {model} {reason}（已等待 {waited:.0f} 秒），"
                    f"正在切换备用模型 {models[index]}…请稍候",
                )
                time.sleep(1)  # 按产品经理要求：失败后等待 1 秒再切
            else:
                _notify(progress, "所有模型均未响应，正在整理提示…")

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


def chat_json(
    system_prompt: str,
    user_content: str,
    progress: ProgressFn | None = None,
) -> dict:
    """要求模型返回 JSON 对象（追问 / 大纲阶段使用）。"""
    text = _call(system_prompt, user_content, json_mode=True, progress=progress)
    return _extract_json(text)


def chat_text(
    system_prompt: str,
    user_content: str,
    progress: ProgressFn | None = None,
) -> str:
    """要求模型返回 Markdown 正文（工单 / Fix 工单使用）。"""
    text = _call(system_prompt, user_content, json_mode=False, progress=progress).strip()
    if not text:
        print("[llm] 模型返回内容为空（已拦截，不写工单）")
        raise LLMError("AI 返回内容为空，请重试")
    if len(text) > config.MAX_TICKET_CHARS:
        # 截断只是兜底保护；正常工单应远小于上限
        text = text[: config.MAX_TICKET_CHARS] + "\n\n> （内容超出上限，已截断）"
    return text
