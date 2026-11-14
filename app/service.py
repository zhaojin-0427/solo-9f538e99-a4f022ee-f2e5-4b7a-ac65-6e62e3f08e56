"""Preview planning and atomic publication of daily PM2.5 batches."""

from __future__ import annotations

import hashlib
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

INTEGRITY_OK = "ok"
INTEGRITY_MISMATCH = "mismatch"
INTEGRITY_UNVERIFIED = "unverified"
INTEGRITY_NO_DATA = "no_data"


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


class IntegrityViolation(AppError):
    status_code = 409
    code = "integrity_check_failed"


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


# ---------------------------------------------------------------------------
# Integrity digests
#
# Every successful publish stores, inside the same transaction, a digest of
# the day's full record set for the new version.  Verification recomputes the
# digest from the live tables and compares; a mismatch means the published
# readings were altered after they were stored.
# ---------------------------------------------------------------------------


def _day_records_for_digest(
    connection: sqlite3.Connection, date: str
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT station, hour, revision, value, batch_id
        FROM published_records
        WHERE date = ?
        ORDER BY station, hour
        """,
        (date,),
    ).fetchall()
    return [
        {
            "station": row["station"],
            "hour": row["hour"],
            "revision": int(row["revision"]),
            "value": row["value"],
            "batch_id": row["batch_id"],
        }
        for row in rows
    ]


def _entry_hash(record: dict) -> str:
    """Hash one record's identity fields: station, hour, revision, value, batch."""
    line = json.dumps(
        {
            "station": record["station"],
            "hour": record["hour"],
            "revision": record["revision"],
            "value": record["value"],
            "batch_id": record["batch_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _compute_digest(records: list[dict]) -> tuple[str, list[dict]]:
    """Return (day digest, per-entry hashes) for a day's records."""
    entries = [
        {
            "station": record["station"],
            "hour": record["hour"],
            "entry_hash": _entry_hash(record),
        }
        for record in sorted(records, key=lambda item: (item["station"], item["hour"]))
    ]
    combined = "\n".join(entry["entry_hash"] for entry in entries)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest(), entries


def _store_digest_locked(
    connection: sqlite3.Connection, date: str, version: int, now: str
) -> None:
    """Snapshot the day's just-written state as the digest for ``version``.

    Must run inside the publish transaction so version, records and digest
    always come from the same committed state.
    """
    records = _day_records_for_digest(connection, date)
    digest, entries = _compute_digest(records)
    connection.execute(
        """
        INSERT INTO day_digests (date, version, digest, record_count, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (date, version, digest, len(records), now),
    )
    connection.executemany(
        """
        INSERT INTO day_digest_entries (date, version, station, hour, entry_hash)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (date, version, entry["station"], entry["hour"], entry["entry_hash"])
            for entry in entries
        ],
    )


def _verify_day_locked(connection: sqlite3.Connection, date: str) -> dict:
    """Verify a day's live records against the digest of its current version.

    Caller must hold a transaction so the version, the records and the digest
    are read from one committed snapshot even if a publish interleaves.
    """
    checked_at = _utc_now()
    version = _current_version(connection, date)
    records = _day_records_for_digest(connection, date)
    digest_row = connection.execute(
        """
        SELECT version, digest, record_count, created_at
        FROM day_digests
        WHERE date = ? AND version = ?
        """,
        (date, version),
    ).fetchone()

    result: dict[str, Any] = {
        "date": date,
        "day_version": version,
        "record_count": len(records),
        "checked_at": checked_at,
        "digest_version": None,
        "digest_created_at": None,
        "expected_record_count": None,
        "expected_digest": None,
        "actual_digest": None,
        "issues": [],
    }

    if digest_row is None:
        has_any_digest = connection.execute(
            "SELECT 1 FROM day_digests WHERE date = ? LIMIT 1", (date,)
        ).fetchone()
        if version == 0 and not records and has_any_digest is None:
            result["status"] = INTEGRITY_NO_DATA
            result["reason"] = "no_published_records"
        else:
            # Days published before digests existed (or with their current
            # version's digest missing) cannot be verified and must never be
            # reported as passed.
            result["status"] = INTEGRITY_UNVERIFIED
            result["reason"] = (
                "missing_digest_for_current_version"
                if has_any_digest is not None
                else "legacy_no_digest"
            )
        return result

    digest, entries = _compute_digest(records)
    result.update(
        {
            "digest_version": int(digest_row["version"]),
            "digest_created_at": digest_row["created_at"],
            "expected_record_count": int(digest_row["record_count"]),
            "expected_digest": digest_row["digest"],
            "actual_digest": digest,
        }
    )

    if digest == digest_row["digest"] and len(records) == int(digest_row["record_count"]):
        result["status"] = INTEGRITY_OK
        result["reason"] = None
        return result

    expected_entries = connection.execute(
        """
        SELECT station, hour, entry_hash
        FROM day_digest_entries
        WHERE date = ? AND version = ?
        """,
        (date, version),
    ).fetchall()
    expected_map = {
        (row["station"], row["hour"]): row["entry_hash"] for row in expected_entries
    }
    current_map = {(entry["station"], entry["hour"]): entry["entry_hash"] for entry in entries}
    record_map = {(record["station"], record["hour"]): record for record in records}

    issues: list[dict] = []
    for key in sorted(expected_map.keys() | current_map.keys()):
        station, hour = key
        current = record_map.get(key)
        current_payload = (
            {
                "revision": current["revision"],
                "value": current["value"],
                "batch_id": current["batch_id"],
            }
            if current is not None
            else None
        )
        if key not in current_map:
            issues.append(
                {
                    "station": station,
                    "hour": hour,
                    "issue": "record_missing",
                    "current": None,
                }
            )
        elif key not in expected_map:
            issues.append(
                {
                    "station": station,
                    "hour": hour,
                    "issue": "record_unexpected",
                    "current": current_payload,
                }
            )
        elif expected_map[key] != current_map[key]:
            issues.append(
                {
                    "station": station,
                    "hour": hour,
                    "issue": "record_modified",
                    "current": current_payload,
                }
            )
    if not issues:
        # Overall digest differs but no entry-level detail is available.
        issues.append(
            {"station": None, "hour": None, "issue": "digest_mismatch", "current": None}
        )

    result["status"] = INTEGRITY_MISMATCH
    result["reason"] = "digest_mismatch"
    result["issues"] = issues
    return result


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

            # A backfill must never mask corrupted data: verify the current
            # version against its stored digest before applying any write.
            # On mismatch the whole batch is rejected and the transaction is
            # rolled back, so published records and digests stay untouched.
            integrity = _verify_day_locked(connection, row["date"])
            if integrity["status"] == INTEGRITY_MISMATCH:
                connection.rollback()
                raise IntegrityViolation(
                    "当前已发布数据未通过完整性核验，疑似在入库后被改动；"
                    "本批已整批拒绝，已发布数据与完整性摘要保持不变",
                    details=integrity,
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
                # Commit the integrity digest for the new version inside the
                # same transaction, so version, records and digest always
                # describe the same committed state.
                _store_digest_locked(connection, row["date"], new_version, now)

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
        # One transaction: version, records and digest come from a single
        # committed snapshot even if a publish commits concurrently.
        connection.execute("BEGIN")
        try:
            integrity = _verify_day_locked(connection, day)
            if integrity["status"] == INTEGRITY_MISMATCH:
                raise IntegrityViolation(
                    "该日已发布数据未通过完整性核验，疑似在入库后被改动；"
                    "为保护分析结果，本次不返回这些读数，请先在核验页确认",
                    details=integrity,
                )
            version = integrity["day_version"]
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
            connection.commit()
            return {
                "date": day,
                "day_version": version,
                "count": len(records),
                "records": records,
            }
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def _integrity_summary(result: dict) -> dict:
    return {
        "date": result["date"],
        "day_version": result["day_version"],
        "record_count": result["record_count"],
        "status": result["status"],
        "reason": result["reason"],
        "digest_version": result["digest_version"],
        "digest_created_at": result["digest_created_at"],
        "issue_count": len(result["issues"]),
        "checked_at": result["checked_at"],
    }


def get_integrity_overview(db_path: str | Path) -> dict:
    """Verify every known day; each day is checked inside one transaction."""
    connection = connect(db_path)
    try:
        connection.execute("BEGIN")
        try:
            dates = [
                row["date"]
                for row in connection.execute(
                    """
                    SELECT date FROM published_records
                    UNION
                    SELECT date FROM day_versions
                    UNION
                    SELECT date FROM day_digests
                    ORDER BY date DESC
                    """
                ).fetchall()
            ]
            days = [
                _integrity_summary(_verify_day_locked(connection, date))
                for date in dates
            ]
            checked_at = _utc_now()
            connection.commit()
            return {"checked_at": checked_at, "days": days}
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def get_integrity_detail(db_path: str | Path, day: str) -> dict:
    """Full verification report for one day, including its version history."""
    connection = connect(db_path)
    try:
        connection.execute("BEGIN")
        try:
            result = _verify_day_locked(connection, day)
            result["versions"] = [
                {
                    "version": int(row["version"]),
                    "digest": row["digest"],
                    "record_count": int(row["record_count"]),
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    """
                    SELECT version, digest, record_count, created_at
                    FROM day_digests
                    WHERE date = ?
                    ORDER BY version
                    """,
                    (day,),
                ).fetchall()
            ]
            connection.commit()
            return result
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
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
    "IntegrityViolation",
    "PreviewNotFound",
    "PreviewRejected",
    "PreviewStale",
    "confirm_preview",
    "create_preview",
    "get_day",
    "get_integrity_detail",
    "get_integrity_overview",
    "get_preview",
    "list_days",
]
