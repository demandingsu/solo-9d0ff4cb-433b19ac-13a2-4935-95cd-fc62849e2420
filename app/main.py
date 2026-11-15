"""FastAPI 入口：上传核查、查询现役版本、历史记录、版本恢复、静态页面。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .evaluator import FormatError, build_report, parse_dependencies, parse_sources
from .store import (ConflictError, SnapshotNotFound, SnapshotStore,
                    UnrecoverableError)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
EXAMPLES_DIR = Path(os.environ.get("EXAMPLES_DIR", BASE_DIR.parent / "examples"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "dwcheck.db"))

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 单文件 20MB 上限

store = SnapshotStore(DB_PATH)
app = FastAPI(title="环境监测数仓字段变更核查", version="1.1.0")


class RestoreRequest(BaseModel):
    # 发起恢复时页面上的现役版本 id；与库中现役不一致则 409（乐观围栏）
    base_id: Optional[int] = None


async def _read_json(upload: UploadFile, label: str) -> Any:
    raw = await upload.read()
    if not raw:
        raise FormatError(f"{label}：文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise FormatError(f"{label}：文件超过 20MB 上限")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FormatError(f"{label}：不是合法的 JSON 文件（{e}）") from e


def build_payload(name: str, sources_before_raw: Any, sources_after_raw: Any,
                  dependencies_raw: Any) -> dict:
    """解析 -> 评估 -> 组装自包含快照内容。任何 FormatError 都在此抛出。"""
    src_before = parse_sources(sources_before_raw)
    src_after = parse_sources(sources_after_raw)
    deps = parse_dependencies(dependencies_raw)
    report = build_report(deps, src_before, src_after)
    return {
        "name": name,
        "sources_before": sources_before_raw,
        "sources_after": sources_after_raw,
        "dependencies": dependencies_raw,
        "report": report,
    }


@app.post("/api/snapshots")
async def create_snapshot(
    sources_before: UploadFile = File(..., description="变更前源字段清单 JSON"),
    sources_after: UploadFile = File(..., description="变更后源字段清单 JSON"),
    dependencies: UploadFile = File(..., description="跨作业字段依赖 JSON"),
    name: str = Form(""),
    base_id: Optional[int] = Form(None, description="发起核查时的现役版本 id（乐观围栏）"),
):
    """上传三份清单并核查。成功则整版替换现役版本；失败保留旧版并留痕。

    带 base_id 时，若现役版本已被他人（例如一次历史恢复）抢先替换，本次结果不发布、
    返回 409——迟到的核查不会覆盖恢复结果。
    """
    snap_name = name.strip() or f"核查-{dependencies.filename or '未命名'}"
    try:
        payload = build_payload(
            snap_name,
            await _read_json(sources_before, "变更前源字段清单"),
            await _read_json(sources_after, "变更后源字段清单"),
            await _read_json(dependencies, "跨作业字段依赖"),
        )
    except FormatError as e:
        store.record_failed(snap_name, str(e))
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        snap_id = store.publish(snap_name, payload, base_id=base_id)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"id": snap_id, "name": snap_name, "summary": payload["report"]["summary"]}


@app.get("/api/snapshots/current")
async def get_current():
    """现役版本全量内容（查询只命中单一版本，绝不混版）。"""
    cur = store.current()
    if not cur:
        raise HTTPException(status_code=404, detail="暂无现役版本，请先上传核查")
    return cur


@app.get("/api/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: int):
    """查看某个历史快照的完整内容与结论；失败记录只返回留痕信息（无 payload）。"""
    snap = store.get_snapshot(snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail=f"历史版本 #{snapshot_id} 不存在")
    return snap


@app.post("/api/snapshots/{snapshot_id}/restore")
async def restore_snapshot(snapshot_id: int, req: RestoreRequest):
    """从历史成功版本恢复：完整内容原样复制为新现役版本，并记录来源。

    - 失败记录不可恢复（400）；版本不存在（404）；
    - base_id 与现役不一致（409），原版本保留，提示冲突。
    """
    try:
        result = store.restore(snapshot_id, base_id=req.base_id)
    except SnapshotNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except UnrecoverableError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return result


@app.get("/api/snapshots")
async def list_snapshots():
    return {"items": store.history()}


@app.get("/api/health")
async def health():
    return {"status": "up"}


@app.get("/examples/{filename}")
async def get_example(filename: str):
    if filename not in {"sources_before.json", "sources_after.json", "dependencies.json"}:
        raise HTTPException(status_code=404, detail="未知示例文件")
    path = EXAMPLES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="示例文件未部署")
    return FileResponse(path, media_type="application/json", filename=filename)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
