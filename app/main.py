"""字幕断句校验台 — FastAPI 入口。

路由：
  GET  /                     前端页面
  POST /api/analyze          上传字幕并校验（multipart）
  GET  /api/issues           按语言/类型筛选问题
  GET  /api/languages        语言聚合
  GET  /api/analyses         最近任务
  GET  /api/analyses/{id}    任务详情（含修订 cue）
  GET  /api/export/{id}.vtt  导出修订后的 WebVTT
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from .analyzer import analyze, render_webvtt
from .parser import LANG_LABELS, parse_subtitles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动即初始化数据库
    yield


app = FastAPI(title="字幕断句校验台", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
async def analyze_upload(
    file: UploadFile = File(...),
    max_chars: int = Form(21),
    min_dwell_ms: int = Form(1000),
    cps_max: float = Form(17.0),
    merge_gap_ms: int = Form(250),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")

    cues = parse_subtitles(content)
    if not cues:
        raise HTTPException(400, "未解析到任何字幕 cue，请确认是 WebVTT 或 SRT 格式")

    filename = file.filename or "subtitle.vtt"
    fmt = "srt" if filename.lower().endswith(".srt") else "vtt"

    result = analyze(
        cues,
        max_chars=max_chars,
        min_dwell_ms=min_dwell_ms,
        cps_max=cps_max,
        merge_gap_ms=merge_gap_ms,
    )
    revised_vtt = render_webvtt(result)

    result_dict = {
        "cues": [
            {
                "index": i,
                "start_ms": c.start_ms,
                "end_ms": c.end_ms,
                "text": c.text,
                "original_indices": c.original_indices,
                "cps": round(c.cps, 2),
                "max_line_len": c.max_line_len,
                "issues": [
                    {"type": x.type, "severity": x.severity, "message": x.message}
                    for x in c.issues
                ],
            }
            for i, c in enumerate(result.cues)
        ],
        "issues": [
            {
                "cue_index": x.cue_index,
                "type": x.type,
                "severity": x.severity,
                "message": x.message,
                "detail": x.detail,
            }
            for x in result.issues
        ],
        "stats": result.stats,
    }

    upload_id = db.create_upload(filename, fmt, content, result.language, len(cues))
    analysis_id = db.create_analysis(
        upload_id,
        {
            "max_chars": max_chars,
            "min_dwell_ms": min_dwell_ms,
            "cps_max": cps_max,
            "merge_gap_ms": merge_gap_ms,
        },
        result_dict,
        revised_vtt,
        result.language,
    )

    return {
        "analysis_id": analysis_id,
        "upload_id": upload_id,
        "language": result.language,
        "language_label": LANG_LABELS.get(result.language, result.language),
        "stats": result.stats,
        "export_url": f"/api/export/{analysis_id}.vtt",
    }


@app.get("/api/issues")
def list_issues(
    language: str = Query("all"),
    type: str = Query("all"),
):
    return {"issues": db.list_issues(language, type)}


@app.get("/api/languages")
def languages():
    facets = db.language_facets()
    for f in facets:
        f["label"] = LANG_LABELS.get(f["language"], f["language"])
    return {"languages": facets}


@app.get("/api/analyses")
def analyses():
    return {"analyses": db.list_recent_analyses()}


@app.get("/api/analyses/{analysis_id}")
def analysis_detail(analysis_id: int):
    data = db.get_analysis(analysis_id)
    if data is None:
        raise HTTPException(404, "分析不存在")
    return data


@app.get("/api/export/{analysis_id}.vtt", response_class=PlainTextResponse)
def export_vtt(analysis_id: int):
    vtt = db.get_revised_vtt(analysis_id)
    if vtt is None:
        raise HTTPException(404, "分析不存在")
    return PlainTextResponse(
        vtt,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="revised_{analysis_id}.vtt"'
        },
    )


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
