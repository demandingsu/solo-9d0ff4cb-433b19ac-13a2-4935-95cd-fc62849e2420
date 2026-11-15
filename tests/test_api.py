"""API 端到端测试：在真实 uvicorn 服务上验证上传/恢复/围栏交错场景。

需要安装 requirements.txt 中的依赖；未安装时本模块整体 skip（存储层并发语义由
test_store.py 以纯标准库覆盖，CI 无第三方依赖也能跑核心用例）：
    python3 -m unittest tests.test_api -v
"""
import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import uvicorn  # noqa: F401
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----dwcheck" + uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        if value is None:
            continue
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(str(value).encode() + b"\r\n")
    for name, (filename, content) in files.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        lines.append(b"Content-Type: application/json\r\n\r\n")
        lines.append(content + b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), boundary


@unittest.skipUnless(HAVE_DEPS, "未安装 fastapi/uvicorn，跳过 HTTP 端到端测试")
class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls.tmp.name, "api-test.db")
        # 延迟导入，确保 DB_PATH 生效
        from app.main import app
        cls.port = _free_port()
        cls.config = uvicorn.Config(app, host="127.0.0.1", port=cls.port,
                                    log_level="error", lifespan="off")
        cls.server = uvicorn.Server(cls.config)
        cls.thread = __import__("threading").Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                cls._request("GET", "/api/health")
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("测试服务启动超时")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.tmp.cleanup()

    # -- HTTP 工具 --------------------------------------------------------

    @staticmethod
    def _request(method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{ApiTestCase.port}{path}"
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    @classmethod
    def _upload(cls, name="示例核查", base_id=None, mutate_after=None):
        files = {}
        for key, fname in (("sources_before", "sources_before.json"),
                           ("sources_after", "sources_after.json"),
                           ("dependencies", "dependencies.json")):
            content = (EXAMPLES / fname).read_bytes()
            if mutate_after and key == "sources_after":
                data = json.loads(content)
                mutate_after(data)
                content = json.dumps(data, ensure_ascii=False).encode()
            files[key] = (fname, content)
        body, boundary = _multipart({"name": name, "base_id": base_id}, files)
        return cls._request("POST", "/api/snapshots", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    # -- 用例 -------------------------------------------------------------

    def test_01_full_restore_flow(self):
        # 发布两版：v1 用原始示例，v2 让变更后清单再删掉 pm25（制造不同结论）
        status, v1 = self._upload("第一版")
        self.assertEqual(status, 200, v1)
        id1 = v1["id"]

        def drop_pm25(data):
            data["tables"]["raw_air_station_hourly"].remove("pm25")
        status, v2 = self._upload("误发布版", base_id=id1, mutate_after=drop_pm25)
        self.assertEqual(status, 200, v2)
        id2 = v2["id"]
        self.assertNotEqual(v1["summary"], v2["summary"])

        # 历史中能看到两版且来源为 upload
        status, hist = self._request("GET", "/api/snapshots")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in hist["items"]][:2], [id2, id1])

        # 查看旧版结论
        status, old = self._request("GET", f"/api/snapshots/{id1}")
        self.assertEqual(status, 200)
        self.assertEqual(old["payload"]["report"]["summary"], v1["summary"])
        self.assertEqual(old["origin_type"], "upload")

        # 现役是 v2
        status, cur = self._request("GET", "/api/snapshots/current")
        self.assertEqual(cur["id"], id2)

        # 恢复 v1（携带现役 base_id）
        status, res = self._request("POST", f"/api/snapshots/{id1}/restore",
                                    json.dumps({"base_id": id2}).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 200, res)
        rid = res["id"]
        self.assertEqual(res["origin_snapshot_id"], id1)

        # 恢复中/后查询只见完整现役版，且内容与 v1 完全一致
        status, cur = self._request("GET", "/api/snapshots/current")
        self.assertEqual(status, 200)
        self.assertEqual(cur["id"], rid)
        self.assertEqual(cur["origin_type"], "restore")
        self.assertEqual(cur["origin_snapshot_id"], id1)
        self.assertEqual(cur["payload"], old["payload"])
        self.assertIn(f"#{id1}", cur["name"])

    def test_02_failed_record_cannot_be_restored(self):
        # 构造一次失败核查：依赖文件不是合法 JSON
        files = {
            "sources_before": ("b.json", b'{"tables":{"t":["a"]}}'),
            "sources_after": ("a.json", b'{"tables":{"t":["a"]}}'),
            "dependencies": ("d.json", b"{not json"),
        }
        body, boundary = _multipart({"name": "失败核查"}, files)
        status, err = self._request("POST", "/api/snapshots", body,
                                    {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 400)
        status, hist = self._request("GET", "/api/snapshots")
        failed = next(i for i in hist["items"] if i["state"] == "failed")
        fid = failed["id"]

        # 失败记录查看得到留痕但无 payload
        status, snap = self._request("GET", f"/api/snapshots/{fid}")
        self.assertEqual(status, 200)
        self.assertIsNone(snap["payload"])
        self.assertTrue(snap["error"])

        # 恢复失败记录 -> 400，现役不变
        before = self._request("GET", "/api/snapshots/current")[1]
        status, err = self._request("POST", f"/api/snapshots/{fid}/restore",
                                    json.dumps({"base_id": before["id"]}).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        after = self._request("GET", "/api/snapshots/current")[1]
        self.assertEqual(after["id"], before["id"])

        # 不存在的版本 -> 404
        status, _ = self._request("POST", "/api/snapshots/999999/restore",
                                  json.dumps({}).encode(),
                                  {"Content-Type": "application/json"})
        self.assertEqual(status, 404)

    def test_03_late_upload_loses_to_restore(self):
        """与恢复交错：先发起的核查迟到，不得覆盖恢复结果。"""
        _, a = self._upload("交错-v1")
        _, b = self._upload("交错-v2", base_id=a["id"])
        base = b["id"]
        # 恢复先行生效
        status, res = self._request("POST", f"/api/snapshots/{a['id']}/restore",
                                    json.dumps({"base_id": base}).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        restored_id = res["id"]
        # 迟到的核查仍携带旧 base -> 409，且库中现役仍是恢复版
        status, conflict = self._upload("迟到核查", base_id=base)
        self.assertEqual(status, 409)
        self.assertIn("现役版本已变化", conflict["detail"])
        cur = self._request("GET", "/api/snapshots/current")[1]
        self.assertEqual(cur["id"], restored_id)
        self.assertEqual(cur["origin_type"], "restore")

    def test_04_concurrent_restore_same_base_only_one_wins(self):
        """两人基于同一现役版同时恢复：仅一项生效，另一方 409。"""
        _, v1 = self._upload("并发-v1")
        _, v2 = self._upload("并发-v2", base_id=v1["id"])
        _, v3 = self._upload("并发-v3", base_id=v2["id"])
        base = v3["id"]
        results = {}

        def restore(target, who):
            status, body = self._request(
                "POST", f"/api/snapshots/{target}/restore",
                json.dumps({"base_id": base}).encode(),
                {"Content-Type": "application/json"})
            results[who] = (status, body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(restore, v1["id"], "A")
            f2 = pool.submit(restore, v2["id"], "B")
            f1.result(); f2.result()

        statuses = sorted(s for s, _ in results.values())
        self.assertEqual(statuses, [200, 409])
        winner = next(b for s, b in results.values() if s == 200)
        cur = self._request("GET", "/api/snapshots/current")[1]
        self.assertEqual(cur["id"], winner["id"])
        loser_status = next(s for s, _ in results.values() if s == 409)
        self.assertEqual(loser_status, 409)
        # 全局只有一个 active
        hist = self._request("GET", "/api/snapshots")[1]["items"]
        self.assertEqual(len([i for i in hist if i["state"] == "active"]), 1)

    def test_05_new_upload_after_restore_publishes(self):
        """恢复后新发起的核查仍能发布。"""
        _, v1 = self._upload("恢复后-v1")
        _, v2 = self._upload("恢复后-v2", base_id=v1["id"])
        status, res = self._request("POST", f"/api/snapshots/{v1['id']}/restore",
                                    json.dumps({"base_id": v2["id"]}).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 200, res)
        rid = res["id"]
        status, v3 = self._upload("恢复后新核查", base_id=rid)
        self.assertEqual(status, 200, v3)
        cur = self._request("GET", "/api/snapshots/current")[1]
        self.assertEqual(cur["id"], v3["id"])
        self.assertEqual(cur["origin_type"], "upload")
        # 恢复版被归档但来源链保留
        snap = self._request("GET", f"/api/snapshots/{rid}")[1]
        self.assertEqual(snap["state"], "archived")
        self.assertEqual(snap["origin_snapshot_id"], v1["id"])

    def test_06_index_page_served(self):
        status, html = self._raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn("恢复为现役版", html)
        self.assertIn("/api/snapshots/", html)

    def _raw_get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


if __name__ == "__main__":
    unittest.main()
