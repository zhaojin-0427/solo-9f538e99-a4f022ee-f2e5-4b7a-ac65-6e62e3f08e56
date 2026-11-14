"""Integrity digests: storage at publish time, verification, and lockdown."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-11-13"


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client, db_path


def upload(client, text: str):
    return client.post(
        "/api/previews",
        files={"file": ("day.csv", text.encode("utf-8"), "text/csv")},
    )


def sample_rows(date: str = DAY) -> str:
    return (
        "station,timestamp,revision,value\n"
        f"SY-001,{date}T00:00:00,1,12.4\n"
        f"SY-001,{date}T01:00:00,1,10.8\n"
        f"SY-001,{date}T01:00:00,2,11.2\n"
        f"SY-002,{date}T00:00:00,3,18.0\n"
        f"SY-002,{date}T01:00:00,1,16.5\n"
    )


def publish(client, text: str) -> dict:
    response = upload(client, text)
    assert response.status_code == 201, response.text
    preview = response.json()
    confirm = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert confirm.status_code == 200, confirm.text
    return preview


def tamper(db_path, sql: str, params: tuple = ()) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def query(db_path, sql: str, params: tuple = ()):
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def insert_legacy_day(db_path, date: str = "2026-11-10") -> None:
    """Simulate rows published before integrity digests existed."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = "2026-11-10T01:00:00+00:00"
        connection.execute(
            "INSERT INTO day_versions (date, version, updated_at) VALUES (?, 1, ?)",
            (date, now),
        )
        for station, hour, revision, value in (
            ("SY-001", "00:00", 1, "12.4"),
            ("SY-001", "01:00", 2, "11.2"),
        ):
            connection.execute(
                """
                INSERT INTO published_records
                    (date, station, hour, revision, value, batch_id, updated_at)
                VALUES (?, ?, ?, ?, ?, 'legacy-batch', ?)
                """,
                (date, station, hour, revision, value, now),
            )
        connection.commit()
    finally:
        connection.close()


def test_publish_stores_digest_and_verifies_ok(env):
    client, _ = env
    publish(client, sample_rows())

    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert detail["reason"] is None
    assert detail["day_version"] == 1
    assert detail["digest_version"] == 1
    assert detail["record_count"] == 4
    assert detail["expected_record_count"] == 4
    assert detail["issues"] == []
    assert len(detail["expected_digest"]) == 64
    assert detail["actual_digest"] == detail["expected_digest"]

    assert [v["version"] for v in detail["versions"]] == [1]
    assert detail["versions"][0]["record_count"] == 4
    assert detail["versions"][0]["digest"] == detail["expected_digest"]

    overview = client.get("/api/integrity").json()
    entry = next(day for day in overview["days"] if day["date"] == DAY)
    assert entry["status"] == "ok"
    assert entry["issue_count"] == 0
    assert entry["digest_version"] == 1


def test_each_publish_stores_a_digest_for_its_own_version(env):
    client, _ = env
    publish(client, sample_rows())
    first = client.get(f"/api/integrity/{DAY}").json()

    publish(
        client,
        "station,timestamp,revision,value\n"
        f"SY-003,{DAY}T03:00:00,1,21.0\n",
    )
    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert detail["day_version"] == 2
    assert detail["digest_version"] == 2
    assert detail["record_count"] == 5
    assert [v["version"] for v in detail["versions"]] == [1, 2]
    assert detail["expected_digest"] != first["expected_digest"]


@pytest.mark.parametrize(
    "field,forged",
    [("value", "999"), ("revision", 7), ("batch_id", "forged-batch")],
)
def test_tampered_records_lock_day_queries(env, field, forged):
    client, db_path = env
    publish(client, sample_rows())
    tamper(
        db_path,
        f"UPDATE published_records SET {field} = ? "
        "WHERE date = ? AND station = 'SY-001' AND hour = '00:00'",
        (forged, DAY),
    )

    for path in (f"/api/days/{DAY}", f"/api/days/{DAY}.json"):
        response = client.get(path)
        assert response.status_code == 409, path
        body = response.json()
        assert body["error"] == "integrity_check_failed"
        assert "records" not in body  # readings must not leak as normal data
        assert body["detail"]["status"] == "mismatch"

    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "mismatch"
    assert detail["reason"] == "digest_mismatch"
    assert detail["actual_digest"] != detail["expected_digest"]
    assert len(detail["issues"]) == 1
    issue = detail["issues"][0]
    assert issue["issue"] == "record_modified"
    assert (issue["station"], issue["hour"]) == ("SY-001", "00:00")
    assert issue["current"][field] == forged

    overview = client.get("/api/integrity").json()
    entry = next(day for day in overview["days"] if day["date"] == DAY)
    assert entry["status"] == "mismatch"
    assert entry["issue_count"] == 1


def test_deleted_and_injected_records_are_pinpointed(env):
    client, db_path = env
    publish(client, sample_rows())
    tamper(
        db_path,
        "DELETE FROM published_records WHERE date = ? AND station = 'SY-002' AND hour = '00:00'",
        (DAY,),
    )
    tamper(
        db_path,
        """
        INSERT INTO published_records
            (date, station, hour, revision, value, batch_id, updated_at)
        VALUES (?, 'SY-009', '09:00', 1, '55', 'rogue-batch', '2026-11-14T00:00:00+00:00')
        """,
        (DAY,),
    )

    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "mismatch"
    by_key = {(i["station"], i["hour"]): i for i in detail["issues"]}
    assert by_key[("SY-002", "00:00")]["issue"] == "record_missing"
    assert by_key[("SY-002", "00:00")]["current"] is None
    assert by_key[("SY-009", "09:00")]["issue"] == "record_unexpected"
    assert by_key[("SY-009", "09:00")]["current"]["value"] == "55"


def test_backfill_is_rejected_when_current_version_is_corrupt(env):
    client, db_path = env
    publish(client, sample_rows())
    before = client.get(f"/api/integrity/{DAY}").json()

    tamper(
        db_path,
        "UPDATE published_records SET value = '999' "
        "WHERE date = ? AND station = 'SY-001' AND hour = '00:00'",
        (DAY,),
    )

    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        f"SY-003,{DAY}T03:00:00,1,21.0\n",
    ).json()
    confirm = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert confirm.status_code == 409
    body = confirm.json()
    assert body["error"] == "integrity_check_failed"
    assert body["detail"]["status"] == "mismatch"

    # The failed batch changed nothing: data, version and digest are intact.
    assert query(db_path, "SELECT COUNT(*) FROM published_records WHERE date = ?", (DAY,)) == [(4,)]
    assert query(db_path, "SELECT version FROM day_versions WHERE date = ?", (DAY,)) == [(1,)]
    assert query(db_path, "SELECT station FROM published_records WHERE station = 'SY-003'") == []
    after = client.get(f"/api/integrity/{DAY}").json()
    assert after["day_version"] == 1
    assert after["expected_digest"] == before["expected_digest"]
    assert [v["version"] for v in after["versions"]] == [1]

    # The preview stays confirmable: once the data is repaired it can publish.
    assert client.get(f"/api/previews/{preview['preview_id']}").json()["status"] == "preview"
    tamper(
        db_path,
        "UPDATE published_records SET value = '12.4' "
        "WHERE date = ? AND station = 'SY-001' AND hour = '00:00'",
        (DAY,),
    )
    retry = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert retry.status_code == 200, retry.text
    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert detail["day_version"] == 2
    assert [v["version"] for v in detail["versions"]] == [1, 2]


def test_legacy_day_is_unverified_and_backfill_only_covers_new_version(env):
    client, db_path = env
    legacy_date = "2026-11-10"
    insert_legacy_day(db_path, legacy_date)

    detail = client.get(f"/api/integrity/{legacy_date}").json()
    assert detail["status"] == "unverified"
    assert detail["reason"] == "legacy_no_digest"
    assert detail["digest_version"] is None
    assert detail["versions"] == []

    # Unverified data is still queryable; it is simply never marked as passed.
    day = client.get(f"/api/days/{legacy_date}")
    assert day.status_code == 200
    assert day.json()["count"] == 2

    # A successful backfill establishes a digest for the new version only.
    publish(
        client,
        "station,timestamp,revision,value\n"
        f"SY-001,{legacy_date}T00:00:00,2,12.9\n",
    )
    detail = client.get(f"/api/integrity/{legacy_date}").json()
    assert detail["status"] == "ok"
    assert detail["day_version"] == 2
    assert detail["digest_version"] == 2
    # The legacy version 1 was never retroactively marked as verified.
    assert [v["version"] for v in detail["versions"]] == [2]


def test_zero_write_and_repeated_confirms_do_not_add_digests(env):
    client, _ = env
    first = publish(client, sample_rows())

    # Re-uploading identical rows applies no writes and bumps no version.
    identical = upload(client, sample_rows()).json()
    assert identical["counts"]["pending"] == 0
    assert client.post(f"/api/previews/{identical['preview_id']}/confirm").status_code == 200

    # Repeating the original confirm is a no-op as well.
    again = client.post(f"/api/previews/{first['preview_id']}/confirm")
    assert again.status_code == 200
    assert again.json()["already_published"] is True

    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert detail["day_version"] == 1
    assert [v["version"] for v in detail["versions"]] == [1]


def test_digest_write_failure_rolls_back_records_version_and_digest(env, monkeypatch):
    client, _ = env
    publish(client, sample_rows())
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        f"SY-003,{DAY}T03:00:00,1,21.0\n",
    ).json()

    import app.service as service

    original = service._store_digest_locked

    def fail_mid_transaction(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "_store_digest_locked", fail_mid_transaction)
    response = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert response.status_code == 500
    assert response.json()["error"] == "storage_error"
    monkeypatch.setattr(service, "_store_digest_locked", original)

    # Nothing leaked into the committed state: no record, no version, no digest.
    day = client.get(f"/api/days/{DAY}").json()
    assert day["count"] == 4
    assert day["day_version"] == 1
    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert [v["version"] for v in detail["versions"]] == [1]
    assert client.get(f"/api/previews/{preview['preview_id']}").json()["status"] == "preview"

    assert client.post(f"/api/previews/{preview['preview_id']}/confirm").status_code == 200
    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert [v["version"] for v in detail["versions"]] == [1, 2]


def test_verify_and_publish_interleave_without_torn_reads(env):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = env
    publish(client, sample_rows())

    def publisher() -> None:
        for index in range(5):
            text = (
                "station,timestamp,revision,value\n"
                f"SY-9{index:02d},{DAY}T1{index}:00:00,1,{10 + index}\n"
            )
            response = upload(client, text)
            assert response.status_code == 201, response.text
            confirm = client.post(f"/api/previews/{response.json()['preview_id']}/confirm")
            assert confirm.status_code == 200, confirm.text

    def verifier() -> list[str]:
        statuses = []
        for _ in range(30):
            response = client.get(f"/api/integrity/{DAY}")
            assert response.status_code == 200
            statuses.append(response.json()["status"])
        return statuses

    with ThreadPoolExecutor(max_workers=2) as pool:
        verify_future = pool.submit(verifier)
        publish_future = pool.submit(publisher)
        statuses = verify_future.result()
        publish_future.result()

    # Verification never observed a half-committed publish.
    assert statuses
    assert set(statuses) == {"ok"}

    detail = client.get(f"/api/integrity/{DAY}").json()
    assert detail["status"] == "ok"
    assert detail["day_version"] == 6
    # Every committed version carries its own digest.
    assert [v["version"] for v in detail["versions"]] == [1, 2, 3, 4, 5, 6]


def test_overview_shows_mixed_statuses(env):
    client, db_path = env
    publish(client, sample_rows())  # ok
    insert_legacy_day(db_path, "2026-11-10")  # unverified
    publish(client, sample_rows("2026-11-11"))
    tamper(
        db_path,
        "UPDATE published_records SET value = '1' "
        "WHERE date = '2026-11-11' AND station = 'SY-001' AND hour = '00:00'",
    )  # mismatch

    overview = client.get("/api/integrity").json()
    statuses = {day["date"]: day["status"] for day in overview["days"]}
    assert statuses[DAY] == "ok"
    assert statuses["2026-11-10"] == "unverified"
    assert statuses["2026-11-11"] == "mismatch"


def test_integrity_detail_for_unknown_or_invalid_day(env):
    client, _ = env
    detail = client.get("/api/integrity/2026-01-01").json()
    assert detail["status"] == "no_data"
    assert detail["day_version"] == 0
    assert detail["record_count"] == 0
    assert detail["versions"] == []

    assert client.get("/api/integrity/not-a-date").status_code == 400
    assert client.get("/api/integrity/2026-13-01").status_code == 400
