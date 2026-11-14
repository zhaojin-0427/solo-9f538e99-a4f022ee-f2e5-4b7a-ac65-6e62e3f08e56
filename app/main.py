"""FastAPI application for browser-based PM2.5 publishing."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import default_db_path, initialize
from .parsing import MAX_FILE_BYTES, CsvFormatError
from . import service

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def create_app(db_path: str | Path | None = None) -> FastAPI:
    resolved_db_path = str(db_path) if db_path is not None else str(default_db_path())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(resolved_db_path)
        yield

    app = FastAPI(
        title="PM2.5 小时观测发布",
        description="单日 CSV 预览、确认发布与按日 JSON 查询",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db_path = resolved_db_path

    @app.exception_handler(service.AppError)
    async def handle_app_error(request: Request, exc: service.AppError):
        body: dict = {"error": exc.code, "message": exc.message}
        if exc.details is not None:
            body["detail"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(CsvFormatError)
    async def handle_csv_error(request: Request, exc: CsvFormatError):
        return JSONResponse(
            status_code=400,
            content={
                "error": "csv_format_error",
                "message": exc.message,
                "detail": {"errors": exc.errors, "error_count": exc.error_count},
            },
        )

    @app.exception_handler(sqlite3.Error)
    async def handle_sqlite_error(request: Request, exc: sqlite3.Error):
        # Operational failures must never look like a successful publish.
        return JSONResponse(
            status_code=500,
            content={
                "error": "storage_error",
                "message": "发布存储暂时不可用，已发布数据未被本次请求改动，请稍后重试",
            },
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/previews/{preview_id}")
    async def read_preview(preview_id: str) -> dict:
        return service.get_preview(app.state.db_path, preview_id)

    @app.post("/api/previews")
    async def upload_preview(file: UploadFile = File(...)) -> JSONResponse:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise CsvFormatError(
                    "CSV 文件过大",
                    [{
                        "row": None,
                        "field": None,
                        "code": "file_too_large",
                        "message": f"文件不得超过 {MAX_FILE_BYTES // 1024 // 1024} MiB",
                    }],
                )
            chunks.append(chunk)
        preview = service.create_preview(app.state.db_path, b"".join(chunks))
        return JSONResponse(status_code=201, content=preview)

    @app.post("/api/previews/{preview_id}/confirm")
    async def confirm(preview_id: str) -> dict:
        return service.confirm_preview(app.state.db_path, preview_id)

    @app.get("/api/days")
    async def days() -> dict:
        return service.list_days(app.state.db_path)

    def _validate_day(day: str) -> JSONResponse | None:
        if not DATE_PATTERN.match(day):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_date", "message": "日期格式必须为 YYYY-MM-DD"},
            )
        try:
            date_type.fromisoformat(day)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_date", "message": "日期不是有效的日历日期"},
            )
        return None

    @app.get("/api/days/{day}.json")
    async def day_json(day: str) -> JSONResponse:
        invalid = _validate_day(day)
        if invalid is not None:
            return invalid
        return JSONResponse(
            content=service.get_day(app.state.db_path, day),
            headers={"Content-Disposition": f'attachment; filename="{day}-pm25.json"'},
        )

    @app.get("/api/days/{day}")
    async def day(day: str) -> dict:
        invalid = _validate_day(day)
        if invalid is not None:
            return invalid
        return service.get_day(app.state.db_path, day)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(os.environ.get("PM25_DB_PATH"))
