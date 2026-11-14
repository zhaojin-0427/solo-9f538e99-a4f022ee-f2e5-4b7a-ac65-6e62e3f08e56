"""Preview planning and atomic publication of daily PM2.5 batches."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import connect
from .parsing import CsvFormatError, Observation, ParsedBatch, parse_csv

STATUS_PREVIEW = "preview"
STATUS_PUBLISHED = "published"
STATUS_STALE = "stale"
STATUS_CONFLICT = "conflict"


class AppError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class PreviewNotFound(AppError):
    status_code = 404
    code = "preview_not_found"


class PreviewStale(AppError):
    status_code = 409
    code = "preview_stale"


class BatchConflict(AppError):
    status_code = 409
    code = "batch_conflict"


class PreviewRejected(AppError):
    status_code = 409
    code = "preview_rejected"


class PreviewNotPublishable(AppError):
    status_code = 400
    code = "preview_not_publishable"


@dataclass(frozen=True)
class ExistingRecord:
    revision: int
    value: str

    def as_dict(self) -> dict:
        return {"revision": self.revision, "value": self.value}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _current_version(connection: sqlite3.Connection, date: str) -> int:
    row = connection.execute(
        "SELECT version FROM day_versions WHERE date = ?", (date,)
    ).fetchone()
    return int(row["version"]) if row else 0


def _existing_records(
    connection: sqlite3.Connection, date: str
) -> dict[tuple[str, str], ExistingRecord]:
    rows = connection.execute(
        "SELECT station, hour, revision, value FROM published_records WHERE date = ?",
        (date,),
    ).fetchall()
    return {
        (row["station"], row["hour"]): ExistingRecord(
            revision=int(row["revision"]), value=row["value"]
        )
        for row in rows
    }


def _record(
    observation: Observation,
    action: str,
    *,
    reason: str,
    existing: ExistingRecord | None = None,
) -> dict:
    return {
        "station": observation.station,
        "hour": observation.hour,
        "timestamp": observation.timestamp,
        "revision": observation.revision,
        "value": observation.value,
        "action": action,
        "reason": reason,
        "row": observation.row_number,
        "existing": existing.as_dict() if existing else None,
    }


def _conflict(
    *,
    station: str,
    hour: str,
    revision: int,
    reason: str,
    values: list[str],
    rows: list[int],
    existing_value: str | None = None,
) -> dict:
    return {
        "station": station,
        "hour": hour,
        "revision": revision,
        "reason": reason,
        "values": values,
        "rows": rows,
        "existing_value": existing_value,
    }


def build_plan(
    batch: ParsedBatch,
    existing: dict[tuple[str, str], ExistingRecord],
) -> dict:
    """Classify every uploaded row and build the set of writes to apply.

    Rules:
      * highest revision per (station, hour) wins;
      * duplicate revisions with different values reject the whole batch;
      * rows never overwrite a higher published revision;
      * a revision equal to the published one is only acceptable with the
        identical value (otherwise it is a conflict).
    """
    by_key: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for observation in batch.observations:
        by_key[observation.key].append(observation)

    conflicts: list[dict] = []
    records: list[dict] = []
    plan_writes: dict[tuple[str, str], Observation] = {}
    conflict_row_numbers: set[int] = set()

    for key in sorted(by_key):
        rows = sorted(by_key[key], key=lambda item: item.row_number)
        station, hour = key
        published = existing.get(key)

        # 1. Same revision, different values inside the batch: whole batch fails.
        revisions: dict[int, list[Observation]] = defaultdict(list)
        for observation in rows:
            revisions[observation.revision].append(observation)
        for revision, revision_rows in sorted(revisions.items()):
            values = sorted({item.value for item in revision_rows})
            if len(values) > 1:
                involved = [item.row_number for item in revision_rows]
                conflict_row_numbers.update(involved)
                conflicts.append(
                    _conflict(
                        station=station,
                        hour=hour,
                        revision=revision,
                        reason="revision_value_mismatch_in_batch",
                        values=values,
                        rows=involved,
                    )
                )

        # 2. Same revision as the published record with a different value is
        #    equally irreconcilable, even if another row in this batch is newer.
        if published is not None:
            clashing = [
                item for item in rows
                if item.revision == published.revision and item.value != published.value
            ]
            if clashing:
                involved = [item.row_number for item in clashing]
                conflict_row_numbers.update(involved)
                conflicts.append(
                    _conflict(
                        station=station,
                        hour=hour,
                        revision=published.revision,
                        reason="revision_value_mismatch_with_published",
                        values=sorted({item.value for item in clashing}),
                        rows=involved,
                        existing_value=published.value,
                    )
                )

        winner_revision = max(revisions)
        winner_rows = revisions[winner_revision]
        winner = winner_rows[0]

        for observation in rows:
            if observation.row_number in conflict_row_numbers:
                records.append(
                    _record(
                        observation,
                        "conflict",
                        reason="revision_value_mismatch",
                        existing=published,
                    )
                )
                continue

            if observation.revision < winner_revision:
                records.append(
                    _record(
                        observation,
                        "ignore",
                        reason="superseded_in_batch",
                        existing=published,
                    )
                )
                continue

            if observation.row_number != winner.row_number:
                records.append(
                    _record(
                        observation,
                        "ignore",
                        reason="duplicate_in_batch",
                        existing=published,
                    )
                )
                continue

            if published is None:
                plan_writes[key] = observation
                records.append(_record(observation, "add", reason="new"))
            elif winner_revision > published.revision:
                plan_writes[key] = observation
                records.append(
                    _record(observation, "replace", reason="newer_revision",
                            existing=published)
                )
            elif winner_revision == published.revision:  # values are equal here
                records.append(
                    _record(observation, "ignore", reason="already_published",
                            existing=published)
                )
            else:
                records.append(
                    _record(observation, "ignore", reason="older_than_published",
                            existing=published)
                )

    records.sort(key=lambda item: item["row"])

    counts = {"added": 0, "replaced": 0, "ignored": 0, "conflicts": 0}
    for item in records:
        if item["action"] == "add":
            counts["added"] += 1
        elif item["action"] == "replace":
            counts["replaced"] += 1
        elif item["action"] == "ignore":
            counts["ignored"] += 1
        else:
            counts["conflicts"] += 1
    counts["pending"] = 0 if conflicts else counts["added"] + counts["replaced"]
    counts["rows"] = len(records)

    return {
        "records": records,
        "conflicts": conflicts,
        "counts": counts,
        # A rejected batch must never carry executable writes, even though the
        # per-row classification is still shown to explain the decision.
        "writes": [] if conflicts else [
            {
                "station": observation.station,
                "hour": observation.hour,
                "revision": observation.revision,
                "value": observation.value,
            }
            for _, observation in sorted(plan_writes.items())
        ],
        "has_conflicts": bool(conflicts),
    }


def _public_preview(
    preview_id: str | None,
    batch_date: str,
    version: int,
    plan: dict,
    *,
    status: str,
    created_at: str,
    result: dict | None = None,
) -> dict:
    payload = {
        "preview_id": preview_id,
        "date": batch_date,
        "day_version": version,
        "status": status,
        "counts": plan["counts"],
        "records": plan["records"],
        "conflicts": plan["conflicts"],
        "created_at": created_at,
    }
    if result is not None:
        payload["result"] = result
    return payload


def create_preview(db_path: str | Path, raw: bytes) -> dict:
    batch = parse_csv(raw)

    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = _current_version(connection, batch.date)
            existing = _existing_records(connection, batch.date)
            plan = build_plan(batch, existing)

            if plan["has_conflicts"]:
                connection.rollback()
                raise BatchConflict(
                    "同站同小时存在修订号相同但数值不一致的记录，整批已拒绝",
                    details=_public_preview(
                        None,
                        batch.date,
                        version,
                        plan,
                        status=STATUS_CONFLICT,
                        created_at=_utc_now(),
                    ),
                )

            preview_id = uuid.uuid4().hex
            created_at = _utc_now()
            public = _public_preview(
                preview_id,
                batch.date,
                version,
                plan,
                status=STATUS_PREVIEW,
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO previews
                    (id, date, day_version, status, preview_json, plan_json,
                     result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    preview_id,
                    batch.date,
                    version,
                    STATUS_PREVIEW,
                    json.dumps(public, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def _load_preview(connection: sqlite3.Connection, preview_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM previews WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        raise PreviewNotFound("预览不存在或已过期，请重新上传 CSV")
    return row


def get_preview(db_path: str | Path, preview_id: str) -> dict:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _load_preview(connection, preview_id)
            # Surface invalidation as soon as it is observed, without ever
            # touching published records.
            if row["status"] == STATUS_PREVIEW:
                current_version = _current_version(connection, row["date"])
                if current_version != int(row["day_version"]):
                    connection.execute(
                        "UPDATE previews SET status = ?, updated_at = ? WHERE id = ?",
                        (STATUS_STALE, _utc_now(), preview_id),
                    )
                    connection.commit()
                    row = _load_preview(connection, preview_id)
            connection.commit()
            public = json.loads(row["preview_json"])
            public["status"] = row["status"]
            if row["result_json"]:
                public["result"] = json.loads(row["result_json"])
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def confirm_preview(db_path: str | Path, preview_id: str) -> dict:
    """Apply a preview once, atomically, and only if the day is unchanged."""
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _load_preview(connection, preview_id)
            status = row["status"]

            # Repeated confirmation is a no-op and returns the original result.
            if status == STATUS_PUBLISHED:
                connection.commit()
                return {
                    "preview_id": row["id"],
                    "date": row["date"],
                    "status": STATUS_PUBLISHED,
                    "already_published": True,
                    "counts": json.loads(row["preview_json"])["counts"],
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }

            if status == STATUS_STALE:
                connection.commit()
                raise PreviewStale("该预览已失效，该日数据已被更新，请重新上传 CSV")

            if status == STATUS_CONFLICT:
                connection.commit()
                raise PreviewRejected("该批次存在冲突，已被整批拒绝，请修正后重新上传")

            if status != STATUS_PREVIEW:
                connection.rollback()
                raise PreviewNotPublishable("该预览状态不可确认")

            current_version = _current_version(connection, row["date"])
            if current_version != int(row["day_version"]):
                now = _utc_now()
                connection.execute(
                    "UPDATE previews SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_STALE, now, preview_id),
                )
                connection.commit()
                raise PreviewStale(
                    "该预览已失效，该日数据已被他人更新，请重新上传 CSV 获取最新预览"
                )

            plan = json.loads(row["plan_json"])
            writes = plan["writes"]
            now = _utc_now()
            batch_id = row["id"]

            # Verify planned writes are still consistent with the published
            # snapshot; the version guard should make this redundant.
            planned_writes = {
                (item["station"], item["hour"]): item for item in writes
            }
            for (station, hour), existing in _existing_records(connection, row["date"]).items():
                planned = planned_writes.get((station, hour))
                if planned and planned["revision"] <= existing.revision:
                    connection.rollback()
                    raise PreviewStale("该预览已失效，所需写入不再适用，请重新上传 CSV")

            for item in writes:
                connection.execute(
                    """
                    INSERT INTO published_records
                        (date, station, hour, revision, value, batch_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (date, station, hour) DO UPDATE SET
                        revision = excluded.revision,
                        value = excluded.value,
                        batch_id = excluded.batch_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["date"],
                        item["station"],
                        item["hour"],
                        item["revision"],
                        item["value"],
                        batch_id,
                        now,
                    ),
                )

            new_version = current_version
            if writes:
                new_version = current_version + 1
                connection.execute(
                    """
                    INSERT INTO day_versions (date, version, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (date) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (row["date"], new_version, now),
                )

            result = {
                "date": row["date"],
                "published": len(writes),
                "added": plan["counts"]["added"],
                "replaced": plan["counts"]["replaced"],
                "ignored": plan["counts"]["ignored"],
                "published_at": now,
                "day_version": new_version,
                "batch_id": batch_id,
            }
            public = json.loads(row["preview_json"])
            public["day_version"] = new_version
            public["counts"] = plan["counts"]
            connection.execute(
                """
                UPDATE previews
                SET status = ?, day_version = ?, preview_json = ?,
                    result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    STATUS_PUBLISHED,
                    new_version,
                    json.dumps(public, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    now,
                    preview_id,
                ),
            )
            connection.commit()
            return {
                "preview_id": preview_id,
                "date": row["date"],
                "status": STATUS_PUBLISHED,
                "already_published": False,
                "counts": plan["counts"],
                "result": result,
            }
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def get_day(db_path: str | Path, day: str) -> dict:
    connection = connect(db_path)
    try:
        version = _current_version(connection, day)
        rows = connection.execute(
            """
            SELECT station, hour, revision, value, batch_id, updated_at
            FROM published_records
            WHERE date = ?
            ORDER BY station, hour
            """,
            (day,),
        ).fetchall()
        records = [
            {
                "station": row["station"],
                "hour": row["hour"],
                "timestamp": f"{day}T{row['hour']}:00",
                "revision": int(row["revision"]),
                "value": row["value"],
                "batch_id": row["batch_id"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
        return {
            "date": day,
            "day_version": version,
            "count": len(records),
            "records": records,
        }
    finally:
        connection.close()


def list_days(db_path: str | Path) -> dict:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT p.date AS date,
                   COUNT(*) AS record_count,
                   COALESCE(v.version, 0) AS version
            FROM published_records p
            LEFT JOIN day_versions v ON v.date = p.date
            GROUP BY p.date
            ORDER BY p.date DESC
            """
        ).fetchall()
        return {
            "days": [
                {
                    "date": row["date"],
                    "record_count": int(row["record_count"]),
                    "day_version": int(row["version"]),
                }
                for row in rows
            ]
        }
    finally:
        connection.close()


__all__ = [
    "AppError",
    "BatchConflict",
    "CsvFormatError",
    "PreviewNotFound",
    "PreviewRejected",
    "PreviewStale",
    "confirm_preview",
    "create_preview",
    "get_day",
    "get_preview",
    "list_days",
]
