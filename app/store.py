"""快照存储：核查结果整版入库，成功后原子替换现役版本，失败保留旧版；
支持从历史成功版本恢复，并记录恢复来源。

- 每个快照自包含（源清单两版 + 依赖 + 报告），查询只读取 state='active' 的唯一版本，
  因此任何查询都不会混用两版数据。
- 新版本的解析/评估全部在写事务之外完成；替换仅一条事务内的
  UPDATE(archived) + INSERT(active)，失败回滚则旧版原样保留。
- 恢复（restore）把某个历史成功版本的完整 payload 原样复制为一条新的 active 快照，
  并以 origin_type='restore' + origin_snapshot_id 记录来源；失败记录（无 payload）不可恢复。
- 乐观并发围栏：上传/恢复可携带 base_id（发起时所基于的现役版本 id）。写事务内若发现
  现役版本已被他人替换，则抛 ConflictError 且不做任何写入——迟到的核查不会覆盖恢复结果，
  两人同时恢复只有一项生效，另一方收到冲突。
- WAL 模式保证写入进行时，旧版仍可被并发读取；事务提交前读者只会看到完整的旧现役版。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('active', 'archived', 'failed')),
    error                TEXT,
    payload              TEXT,
    origin_type          TEXT NOT NULL DEFAULT 'upload'
                               CHECK (origin_type IN ('upload', 'restore')),
    origin_snapshot_id   INTEGER REFERENCES snapshots(id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_state ON snapshots(state, id);
"""

# 旧版库（无来源列）增量迁移
_MIGRATIONS = (
    ("origin_type",
     "ALTER TABLE snapshots ADD COLUMN origin_type TEXT NOT NULL DEFAULT 'upload'"),
    ("origin_snapshot_id",
     "ALTER TABLE snapshots ADD COLUMN origin_snapshot_id INTEGER REFERENCES snapshots(id)"),
)


class StoreError(Exception):
    """存储层错误基类。"""


class ConflictError(StoreError):
    """乐观围栏冲突：base_id 与当前现役版本不一致（已被他人抢先发布/恢复）。"""

    def __init__(self, message: str, current_id: int):
        super().__init__(message)
        self.current_id = current_id


class SnapshotNotFound(StoreError):
    """指定 id 的快照不存在。"""


class UnrecoverableError(StoreError):
    """目标快照是失败记录或缺少 payload，不能恢复。"""


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        conn = self._conn()
        try:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # -- 内部：写事务入口的围栏检查 ---------------------------------------

    def _check_fence(self, conn: sqlite3.Connection,
                     base_id: Optional[int]) -> Optional[sqlite3.Row]:
        """返回当前 active 行；若给了 base_id 且与现役 id 不符则抛 ConflictError（零写入）。"""
        row = conn.execute(
            "SELECT id FROM snapshots WHERE state='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        current_id = row["id"] if row else None
        if base_id is not None and base_id != current_id:
            raise ConflictError(
                f"现役版本已变化（当前为 #{current_id}），本次操作基于的 #{base_id} "
                f"已不是现役版本，请刷新后重试",
                current_id=current_id if current_id is not None else -1,
            )
        return row

    # -- 写 ---------------------------------------------------------------

    def publish(self, name: str, payload: dict[str, Any],
                base_id: Optional[int] = None) -> int:
        """整版替换：新快照置为 active，旧 active 置为 archived。

        base_id 非空时做乐观围栏校验；冲突或事务失败则旧版原样保留。
        """
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._check_fence(conn, base_id)
                conn.execute("UPDATE snapshots SET state='archived' WHERE state='active'")
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, payload, origin_type) "
                    "VALUES (?, ?, 'active', ?, 'upload')",
                    (name, self._now(), blob))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def record_failed(self, name: str, error: str) -> int:
        """核查失败留痕，不影响现役版本。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, error, origin_type) "
                    "VALUES (?, ?, 'failed', ?, 'upload')",
                    (name or "(未命名)", self._now(), error))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def restore(self, snapshot_id: int,
                base_id: Optional[int] = None) -> dict[str, Any]:
        """把历史成功版本的完整内容作为新现役版本发布，并记录来源。

        - 目标不存在 → SnapshotNotFound；目标是失败记录/无 payload → UnrecoverableError。
        - base_id 非空且现役已变化 → ConflictError，任何错误都在事务内回滚，原版保留。
        返回 {id, name, summary, origin_snapshot_id}。
        """
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._check_fence(conn, base_id)
                target = conn.execute(
                    "SELECT id, name, state, payload FROM snapshots WHERE id=?",
                    (snapshot_id,)).fetchone()
                if target is None:
                    raise SnapshotNotFound(f"历史版本 #{snapshot_id} 不存在")
                if target["state"] == "failed" or not target["payload"]:
                    raise UnrecoverableError(
                        f"历史版本 #{snapshot_id} 是失败记录，没有可恢复的完整内容")
                # 经 JSON 往返得到与历史版完全一致、但独立的 payload 副本
                payload = json.loads(target["payload"])
                blob = json.dumps(payload, ensure_ascii=False)
                new_name = f"恢复自 #{target['id']}：{target['name']}"
                conn.execute("UPDATE snapshots SET state='archived' WHERE state='active'")
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, payload, "
                    "origin_type, origin_snapshot_id) "
                    "VALUES (?, ?, 'active', ?, 'restore', ?)",
                    (new_name, self._now(), blob, target["id"]))
                conn.commit()
                new_id = int(cur.lastrowid)
                return {"id": new_id, "name": new_name,
                        "summary": payload["report"]["summary"],
                        "origin_snapshot_id": target["id"]}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # -- 读（只读现役版本，绝不混版） --------------------------------------

    def current(self) -> Optional[dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, name, created_at, payload, origin_type, origin_snapshot_id "
                "FROM snapshots WHERE state='active' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            return {"id": row["id"], "name": row["name"],
                    "created_at": row["created_at"], "payload": payload,
                    "origin_type": row["origin_type"],
                    "origin_snapshot_id": row["origin_snapshot_id"]}
        finally:
            conn.close()

    def get_snapshot(self, snapshot_id: int) -> Optional[dict[str, Any]]:
        """读取单个快照（含完整 payload）；失败记录无 payload。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, name, created_at, state, error, payload, "
                "origin_type, origin_snapshot_id FROM snapshots WHERE id=?",
                (snapshot_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = json.loads(row["payload"]) if row["payload"] else None
            return item
        finally:
            conn.close()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at, state, error, origin_type, origin_snapshot_id "
                "FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
