"""快照存储：历史版本恢复、乐观围栏与交错并发场景测试（仅依赖标准库）：
python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import (ConflictError, SnapshotNotFound, SnapshotStore,
                       UnrecoverableError)


def make_payload(version_tag: str, broken: int = 0) -> dict:
    """构造可区分版本的自包含 payload（结构与 main.build_payload 对齐）。"""
    return {
        "name": f"核查-{version_tag}",
        "sources_before": {"tables": {"t": ["a", "b"]}},
        "sources_after": {"tables": {"t": ["a"] if broken else ["a", "b"]}},
        "dependencies": {"jobs": [{"name": "j", "outputs": {"c": [["t.b"]]}}]},
        "report": {
            "summary": {"total": 1, "changed": broken,
                        "before": {"ok": 1, "broken": 0, "unknown": 0},
                        "after": {"ok": 1 - broken, "broken": broken, "unknown": 0},
                        "final_total": 1,
                        "final_broken_after": broken, "final_unknown_after": 0},
            "columns": [{"job": "j", "column": "c", "name": "j.c",
                         "tag": version_tag,
                         "after": {"status": "broken" if broken else "ok"}}],
            "labels": {"ok": "正常", "broken": "断供", "unknown": "待核实"},
        },
    }


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.store = SnapshotStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, tag, broken=0, base_id=None):
        return self.store.publish(f"核查-{tag}", make_payload(tag, broken), base_id=base_id)


class TestRestoreBasic(StoreTestBase):
    def test_publish_chain_and_history_origin(self):
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        hist = self.store.history()
        self.assertEqual([h["id"] for h in hist], [id2, id1])
        self.assertTrue(all(h["origin_type"] == "upload" for h in hist))
        cur = self.store.current()
        self.assertEqual(cur["id"], id2)
        self.assertEqual(cur["payload"]["report"]["columns"][0]["tag"], "v2")

    def test_restore_publishes_full_copy_as_new_active(self):
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        res = self.store.restore(id1)
        new_id = res["id"]
        self.assertNotIn(new_id, (id1, id2))
        self.assertEqual(res["origin_snapshot_id"], id1)
        cur = self.store.current()
        # 完整内容原样复制：payload 与历史版逐字节一致
        old = self.store.get_snapshot(id1)
        self.assertEqual(cur["payload"], old["payload"])
        self.assertEqual(cur["payload"]["report"]["columns"][0]["tag"], "v1")
        # 恢复版标记来源，旧两版均归档
        self.assertEqual(cur["origin_type"], "restore")
        self.assertEqual(cur["origin_snapshot_id"], id1)
        states = {h["id"]: h["state"] for h in self.store.history()}
        self.assertEqual(states[new_id], "active")
        self.assertEqual(states[id1], "archived")
        self.assertEqual(states[id2], "archived")
        self.assertIn(f"#{id1}", cur["name"])

    def test_restored_payload_is_independent_copy(self):
        id1 = self.publish("v1")
        self.store.restore(id1)
        cur = self.store.current()
        cur["payload"]["report"]["columns"][0]["tag"] = "tampered"
        again = self.store.get_snapshot(id1)
        self.assertEqual(again["payload"]["report"]["columns"][0]["tag"], "v1")

    def test_failed_record_is_not_recoverable(self):
        id1 = self.publish("v1")
        fid = self.store.record_failed("坏核查", "JSON 不合法")
        with self.assertRaises(UnrecoverableError):
            self.store.restore(fid)
        # 失败后现役版原样保留
        self.assertEqual(self.store.current()["id"], id1)
        snap = self.store.get_snapshot(fid)
        self.assertIsNone(snap["payload"])
        self.assertEqual(snap["error"], "JSON 不合法")

    def test_restore_missing_id(self):
        self.publish("v1")
        with self.assertRaises(SnapshotNotFound):
            self.store.restore(9999)

    def test_restore_failure_keeps_original(self):
        id1 = self.publish("v1")
        fid = self.store.record_failed("坏核查", "boom")
        try:
            self.store.restore(fid)
        except UnrecoverableError:
            pass
        self.assertEqual(self.store.current()["id"], id1)
        # 没有多余的 active 行
        active = [h for h in self.store.history() if h["state"] == "active"]
        self.assertEqual(len(active), 1)

    def test_restore_can_be_chained_and_each_records_origin(self):
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        r1 = self.store.restore(id1)
        # 再恢复一次 v2（此时 v2 已归档但内容完整，仍可恢复）
        r2 = self.store.restore(id2, base_id=r1["id"])
        cur = self.store.current()
        self.assertEqual(cur["id"], r2["id"])
        self.assertEqual(cur["origin_snapshot_id"], id2)
        self.assertEqual(cur["payload"]["report"]["columns"][0]["tag"], "v2")
        # 上一次恢复版已归档，其来源链仍可追溯
        snap_r1 = self.store.get_snapshot(r1["id"])
        self.assertEqual(snap_r1["state"], "archived")
        self.assertEqual(snap_r1["origin_snapshot_id"], id1)

    def test_get_snapshot_unknown_returns_none(self):
        self.assertIsNone(self.store.get_snapshot(424242))


class TestFencing(StoreTestBase):
    def test_late_upload_loses_to_restore(self):
        """迟到的核查不得覆盖恢复结果。"""
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        # 核查 A 基于 #id2 发起；期间恢复把 #id1 重新发布
        self.store.restore(id1, base_id=id2)
        restored_id = self.store.current()["id"]
        # A 的结果迟到，携带旧 base_id -> 冲突，不发布
        with self.assertRaises(ConflictError):
            self.publish("late-A", base_id=id2)
        cur = self.store.current()
        self.assertEqual(cur["id"], restored_id)
        self.assertEqual(cur["origin_type"], "restore")
        self.assertEqual(cur["payload"]["report"]["columns"][0]["tag"], "v1")

    def test_concurrent_restore_same_base_only_one_wins(self):
        """两人基于同一现役版同时恢复：仅一项生效，另一方冲突。"""
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        id3 = self.publish("v3")
        outcome = {}

        def restore(target, key, barrier):
            barrier.wait()
            try:
                outcome[key] = ("ok", self.store.restore(target, base_id=id3)["id"])
            except ConflictError as e:
                outcome[key] = ("conflict", e.current_id)

        bar = threading.Barrier(2)
        t1 = threading.Thread(target=restore, args=(id1, "A", bar))
        t2 = threading.Thread(target=restore, args=(id2, "B", bar))
        t1.start(); t2.start(); t1.join(); t2.join()

        results = [outcome["A"][0], outcome["B"][0]]
        self.assertEqual(sorted(results), ["conflict", "ok"])
        winner = outcome["A"] if outcome["A"][0] == "ok" else outcome["B"]
        self.assertEqual(self.store.current()["id"], winner[1])
        # 败方拿到的 current_id 即胜方新版本
        loser = outcome["A"] if outcome["A"][0] == "conflict" else outcome["B"]
        self.assertEqual(loser[1], winner[1])
        # 始终只有一个 active
        active = [h for h in self.store.history() if h["state"] == "active"]
        self.assertEqual(len(active), 1)

    def test_concurrent_upload_and_restore_only_one_wins(self):
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        outcome = {}
        bar = threading.Barrier(2)

        def do_upload():
            bar.wait()
            try:
                outcome["upload"] = ("ok", self.publish("v3", base_id=id2))
            except ConflictError as e:
                outcome["upload"] = ("conflict", e.current_id)

        def do_restore():
            bar.wait()
            try:
                outcome["restore"] = ("ok", self.store.restore(id1, base_id=id2)["id"])
            except ConflictError as e:
                outcome["restore"] = ("conflict", e.current_id)

        t1 = threading.Thread(target=do_upload); t2 = threading.Thread(target=do_restore)
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(v[0] for v in outcome.values())
        self.assertEqual(statuses, ["conflict", "ok"])
        self.assertEqual(len([h for h in self.store.history() if h["state"] == "active"]), 1)

    def test_new_upload_after_restore_can_publish(self):
        """恢复之后新发起的核查（基于新现役版）仍能发布。"""
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        rid = self.store.restore(id1, base_id=id2)["id"]
        new_id = self.publish("v3", base_id=rid)
        cur = self.store.current()
        self.assertEqual(cur["id"], new_id)
        self.assertEqual(cur["origin_type"], "upload")
        self.assertEqual(self.store.get_snapshot(rid)["state"], "archived")

    def test_upload_without_base_id_remains_unconditional(self):
        """不带围栏的旧客户端行为不变：无条件发布。"""
        id1 = self.publish("v1")
        id2 = self.publish("v2")
        new_id = self.publish("v3")  # base_id=None
        self.assertEqual(self.store.current()["id"], new_id)
        self.assertEqual(self.store.get_snapshot(id2)["state"], "archived")

    def test_restore_with_stale_base_keeps_current(self):
        id1 = self.publish("v1")
        id2 = self.publish("v2", broken=1)
        rid = self.store.restore(id1, base_id=id2)["id"]
        with self.assertRaises(ConflictError):
            self.store.restore(id1, base_id=id2)  # 仍用旧 base
        self.assertEqual(self.store.current()["id"], rid)

    def test_failed_upload_does_not_consume_fence(self):
        """核查失败只留痕，不改现役版；同一 base_id 仍可继续成功发布。"""
        id1 = self.publish("v1")
        fid = self.store.record_failed("坏核查", "bad")
        new_id = self.publish("v2", base_id=id1)
        self.assertEqual(self.store.current()["id"], new_id)
        self.assertEqual(self.store.get_snapshot(fid)["state"], "failed")


class TestReadsDuringWrites(StoreTestBase):
    def test_reader_always_sees_single_complete_active(self):
        """写入/恢复进行中，并发读者只能看到完整现役版，绝不混版或空窗。"""
        id1 = self.publish("v1")
        ids = [id1]
        errors = []

        def reader():
            for _ in range(300):
                cur = self.store.current()
                if cur is None:
                    errors.append("看到空现役版")
                    continue
                payload = cur["payload"]
                # 自包含完整性：标签与 summary 必须来自同一版本
                tag = payload["report"]["columns"][0]["tag"]
                if payload["name"] != f"核查-{tag}":
                    errors.append("混版：name 与 columns 标签不一致")
                active = [h for h in self.store.history() if h["state"] == "active"]
                if len(active) != 1:
                    errors.append(f"active 行数={len(active)}")

        def writer():
            for i in range(2, 25):
                ids.append(self.publish(f"v{i}", broken=i % 2))
            self.store.restore(ids[0], base_id=ids[-1])

        tr = threading.Thread(target=reader)
        tw = threading.Thread(target=writer)
        tr.start(); tw.start(); tr.join(); tw.join()
        self.assertEqual(errors, [])
        cur = self.store.current()
        self.assertEqual(cur["origin_type"], "restore")
        self.assertEqual(cur["payload"]["report"]["columns"][0]["tag"], "v1")


class TestMigration(unittest.TestCase):
    def test_legacy_db_without_origin_columns(self):
        """旧版库（无来源列）启动时自动迁移，历史数据默认标记为 upload。"""
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "legacy.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('active', 'archived', 'failed')),
            error TEXT, payload TEXT);
        """)
        blob = json.dumps(make_payload("legacy"), ensure_ascii=False)
        conn.execute("INSERT INTO snapshots(name, created_at, state, payload) "
                     "VALUES ('核查-legacy','2026-01-01','active',?)", (blob,))
        conn.commit(); conn.close()

        store = SnapshotStore(db)
        cur = store.current()
        self.assertEqual(cur["origin_type"], "upload")
        self.assertIsNone(cur["origin_snapshot_id"])
        # 迁移后恢复功能可用
        rid = store.restore(cur["id"], base_id=cur["id"])["id"]
        self.assertEqual(store.current()["id"], rid)
        self.assertEqual(store.current()["origin_snapshot_id"], cur["id"])


if __name__ == "__main__":
    unittest.main()
