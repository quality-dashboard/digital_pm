# -*- coding: utf-8 -*-
"""
db.py —— V3.2.2 数据访问层（Turso 云端 / 本地 SQLite 自动降级）

为什么单独一层：
- 上层（project_store / app_ui）只关心 SQL 与结果，不关心连的是 Turso 还是本地文件；
- Turso 与 SQLite 都使用 ? 占位符，SQL 可以完全复用；
- 本地未配置 TURSO_DATABASE_URL 时自动用 SQLite，方便开发测试，不阻塞本地运行。

客户端选择：
- 使用 Turso 官方 libsql 包（libsql.connect(...)），走 HTTP pipeline，
  避免 libsql-client 在部分网络/新版 Turso 上出现的 WebSocket 400 握手问题；
- 如果 libsql 在当前网络不可用，页面会自动降级到本地 SQLite 并提示原因。

安全：
- TURSO_AUTH_TOKEN 只在连接时使用；诊断日志最多打印前 10 位；
- 所有写入都走参数绑定，不做 SQL 字符串拼接。

线程安全：
- Streamlit 每个会话在独立线程里跑，因此用可重入锁保护共享连接；
- 本地 SQLite 使用 check_same_thread=False，并开启外键约束。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config

# =============================================================================
# 一、建表 SQL（Turso 与本地 SQLite 完全通用）
# =============================================================================
# 说明：
# - id 统一用 TEXT（UUID hex），避免不同数据库自增策略差异；
# - created_at 存 ISO8601 字符串（UTC），跨时区排序稳定；
# - tickets 对 (project_id, filename) 建唯一约束，天然防止同一项目重复工单；
# - 外键在本地 SQLite 通过 PRAGMA foreign_keys=ON 生效，Turso 默认支持。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id            TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL,
        name          TEXT NOT NULL,
        requirement   TEXT NOT NULL,
        outline_json  TEXT NOT NULL DEFAULT '',
        phase         TEXT NOT NULL DEFAULT 'clarify',
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL,
        type        TEXT NOT NULL,
        filename    TEXT NOT NULL,
        content_md  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (project_id, filename),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_daily (
        date   TEXT PRIMARY KEY,
        count  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_projects_session ON projects(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_project ON tickets(project_id, filename)",
)


class DatabaseError(RuntimeError):
    """数据库配置错误或连接失败时抛出，UI 直接展示给产品经理。"""


class Database:
    """Turso / SQLite 统一门面：execute 写、query 读、init_schema 建表。"""

    def __init__(self) -> None:
        # 按配置决定后端：有 Turso URL 用云端，否则本地 SQLite
        self._backend = "turso" if config.TURSO_DATABASE_URL else "sqlite"
        self._conn = None     # libsql.Connection 或 sqlite3.Connection
        self._lock = threading.RLock()  # 可重入，允许 execute 内部调用 init_schema
        self._schema_ready = False
        # Turso 连不上时自动降级到本地 SQLite，这里记录原因供 UI 提示
        self.fallback_reason: str = ""

    @property
    def backend(self) -> str:
        """返回当前实际后端（降级后会变成 sqlite），UI 可显示。"""
        return self._backend

    # ------------------------------------------------------------------
    # 连接初始化
    # ------------------------------------------------------------------
    def _ensure_client(self) -> None:
        """惰性建立连接：没有实际读写前不连接数据库，避免页面启动即失败。

        重要：这里使用 Turso 官方 libsql 包，不要换回旧的 libsql-client。
        libsql.connect 走 HTTP pipeline，避免 WebSocket 400 握手失败。
        """
        if self._conn is not None:
            return

        if self._backend == "turso":
            if not config.TURSO_AUTH_TOKEN:
                raise DatabaseError(
                    "已配置 TURSO_DATABASE_URL，但缺少 TURSO_AUTH_TOKEN，请补全配置"
                )
            try:
                import libsql
            except ImportError as exc:
                raise DatabaseError(
                    "缺少 libsql，请执行：python -m pip install libsql"
                ) from exc

            # 官方推荐写法：libsql.connect(database=..., auth_token=...)
            # timeout 给 15 秒，兼顾 Turso 冷启动与网络抖动
            self._conn = libsql.connect(
                database=config.TURSO_DATABASE_URL,
                auth_token=config.TURSO_AUTH_TOKEN,
                timeout=15.0,
            )
            return

        # 本地降级：SQLite 文件放代码目录 data/local.db（跨平台绝对路径）
        config.LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(config.LOCAL_DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------
    # 建表 + 自动降级
    # ------------------------------------------------------------------
    def _fallback_to_sqlite(self, reason: Exception) -> None:
        """Turso 连接/建表失败时切换到本地 SQLite，保证页面先能跑。

        注意：降级后数据写本地，不会自动同步回云端；
        UI 会显示 fallback_reason，排查完 Turso 配置后重启即可回到云端。
        """
        self.fallback_reason = f"{type(reason).__name__}: {reason}"[:500]
        self._backend = "sqlite"
        self._conn = None
        self._schema_ready = False
        # 只记录降级事件，不打印异常详情（可能包含 URL / 驱动内部信息）
        print("[db] Turso 连接失败，已降级本地 SQLite")

    def _cleanup_expired(self) -> None:
        """静默清理 30 天前的项目数据（隐私承诺在数据库侧真正落实）。

        - 先删 tickets 再删 projects，兼容未开启外键级联的环境；
        - datetime(created_at) 兼容 ISO8601 的 T 与 +00:00，比直接字符串比较准确；
        - 清理失败只写服务端日志，不阻塞用户操作。
        """
        try:
            self.execute(
                "DELETE FROM tickets WHERE project_id IN ("
                "SELECT id FROM projects "
                "WHERE datetime(created_at) < datetime('now', '-30 day')"
                ")"
            )
            self.execute(
                "DELETE FROM projects "
                "WHERE datetime(created_at) < datetime('now', '-30 day')"
            )
        except Exception as exc:
            # 只记录异常类型，不打印可能含连接串的错误详情
            print(f"[db] 过期数据清理失败：{type(exc).__name__}")

    def init_schema(self) -> None:
        """幂等建表；Turso 失败时自动降级到本地 SQLite 并继续建表。"""
        if self._schema_ready:
            return
        with self._lock:
            if self._schema_ready:
                return
            try:
                self._ensure_client()
                for statement in SCHEMA_STATEMENTS:
                    self._conn.execute(statement)
                self._conn.commit()
                self._schema_ready = True
            except Exception as exc:
                if self._backend == "turso":
                    # 用户要求：云端连不上先降级，让页面能跑，再排查配置
                    self._fallback_to_sqlite(exc)
                    self._ensure_client()
                    for statement in SCHEMA_STATEMENTS:
                        self._conn.execute(statement)
                    self._conn.commit()
                    self._schema_ready = True
                    self._cleanup_expired()
                    return
                raise

        # 建表成功后执行一次 30 天清理（每次进程首次连接执行一次）
        self._cleanup_expired()

    # ------------------------------------------------------------------
    # 统一读写接口（libsql 与 sqlite3 都是 DB-API，逻辑完全复用）
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: tuple | list = ()) -> None:
        """执行写操作（INSERT / UPDATE / DELETE / DDL），自动提交。"""
        self.init_schema()
        with self._lock:
            self._ensure_client()
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        """执行查询并统一返回 list[dict]。"""
        self.init_schema()
        with self._lock:
            self._ensure_client()
            cursor = self._conn.execute(sql, tuple(params))
            rows = cursor.fetchall()
            columns = [item[0] for item in (cursor.description or [])]
            return [dict(zip(columns, row)) for row in rows]

    # ------------------------------------------------------------------
    # 每日全局配额（防刷爆 API 额度）
    # ------------------------------------------------------------------
    def consume_quota(self, limit: int = 100) -> tuple[bool, int]:
        """检查并递增今日全局配额，返回 (是否允许, 今日已用次数)。

        - 日期按北京时间（UTC+8）切分，避免云端 UTC 与用户本地差 8 小时；
        - 并发下可能多放行几次，但对「防刷爆额度」足够；真正的资损保护
          还有按钮锁与后端幂等；
        - 达到上限时不写入，直接返回 False。
        """
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        self.init_schema()
        rows = self.query("SELECT count FROM quota_daily WHERE date = ?", (today,))
        used = int(rows[0]["count"]) if rows else 0
        if used >= limit:
            return False, used

        # UPSERT：当天第一次插入 1，之后累加；参数全部绑定
        self.execute(
            "INSERT INTO quota_daily (date, count) VALUES (?, 1) "
            "ON CONFLICT(date) DO UPDATE SET count = count + 1",
            (today,),
        )
        return True, used + 1

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def healthcheck(self) -> str:
        """返回连接状态，供 UI 排查；异常不抛出，只返回可读文本。"""
        try:
            self.query("SELECT 1 AS ok")
            suffix = f" ｜ 已降级：{self.fallback_reason[:120]}" if self.fallback_reason else ""
            return f"ok ({self._backend}){suffix}"
        except Exception as exc:  # 数据库不可达不应该让页面直接崩
            return f"error ({self._backend}): {type(exc).__name__}: {exc}"


# 模块级单例：整个 Streamlit 进程共用一个连接池/连接
db = Database()
