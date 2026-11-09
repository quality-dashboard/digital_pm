# -*- coding: utf-8 -*-
"""V3.2.0 部署自检：对比 .env 与 secrets.toml 的 Turso 配置，并实测连接。

使用：
    cd /d D:\digital_pm
    python check_cloud.py

安全约定：
- TURSO_DATABASE_URL 完整打印（不是密钥，用于核对库地址）；
- TURSO_AUTH_TOKEN 只打印前 10 位 + 长度，绝不打印完整值；
- 异常信息截断 300 字符，避免日志过长。
"""
from __future__ import annotations

import os
from pathlib import Path

import config
import db


def _load_toml(path: Path) -> dict:
    """读取 TOML：Python 3.11+ 用 tomllib，3.10 及以下用最简顶层键解析。

    secrets.toml 里我们只关心顶层两个字符串键，不需要完整 TOML 语法支持。
    """
    try:
        import tomllib

        with path.open("rb") as handle:
            return tomllib.load(handle)
    except ModuleNotFoundError:
        result: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result


def _toml_secrets() -> tuple[dict, Path | None]:
    """读取本地 secrets.toml（项目级优先，其次用户级），返回 (内容, 路径)。"""
    candidates = [
        Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            return _load_toml(path), path
        except Exception as exc:
            print(f"SECRETS_PARSE_ERROR: {path} / {type(exc).__name__}: {str(exc)[:200]}")
    return {}, None


def _mask(value: str) -> str:
    """Token 只展示前 10 位，避免完整泄露。"""
    value = str(value or "")
    return f"{value[:10]}... (len={len(value)})" if value else "<empty>"


def main() -> None:
    env_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    env_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    secrets, secrets_path = _toml_secrets()
    sec_url = str(secrets.get("TURSO_DATABASE_URL", "")).strip()
    sec_token = str(secrets.get("TURSO_AUTH_TOKEN", "")).strip()

    print("=== 数字项目经理 V3.2.0 数据层自检 ===")
    print("配置优先级：环境变量(.env) > Streamlit Secrets > 默认值")
    print("SECRETS_FILE:", secrets_path or "<未找到>")
    print("ENV_URL:", env_url or "<empty>")
    print("ENV_TOKEN:", _mask(env_token))
    print("SECRETS_URL:", sec_url or "<empty>")
    print("SECRETS_TOKEN:", _mask(sec_token))
    print("URL_CONFLICT:", bool(env_url and sec_url and env_url != sec_url))
    print("URL_SCHEME_OK:", env_url.startswith(("libsql://", "https://")) if env_url else False)

    try:
        import libsql
        print("LIBSQL:", "installed")
    except Exception as exc:
        print("LIBSQL: MISSING /", type(exc).__name__)

    print("DB_BACKEND(config):", config.DB_BACKEND)
    print("DB_BACKEND(actual):", db.db.backend)
    print("FALLBACK_REASON:", db.db.fallback_reason or "<none>")

    try:
        print("HEALTH:", db.db.healthcheck())
        if db.db.backend != "turso":
            print("TURSO_OK: False")
            print("提示：当前是本地 SQLite；请检查 .env / secrets.toml 的 Turso 配置")
            return
        tables = [
            row["name"]
            for row in db.db.query(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        print("TABLES:", tables)
        print("TURSO_OK:", "projects" in tables and "tickets" in tables)
    except Exception as exc:
        print("TURSO_OK: False")
        print("ERROR:", type(exc).__name__, str(exc)[:300])


if __name__ == "__main__":
    main()
