"""End-to-end API tests for preview, publish, and day queries."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def upload(client, text: str, filename: str = "day.csv"):
    return client.post(
        "/api/previews",
        files={"file": (filename, text.encode("utf-8"), "text/csv")},
    )


def sample_rows(date: str = "2026-11-13") -> str:
    return (
        "station,timestamp,revision,value\n"
        f"SY-001,{date}T00:00:00,1,12.4\n"
        f"SY-001,{date}T01:00:00,1,10.8\n"
        f"SY-001,{date}T01:00:00,2,11.2\n"
        f"SY-001,{date}T01:00:00,2,11.2\n"
        f"SY-002,{date}T00:00:00,3,18.0\n"
        f"SY-002,{date}T01:00:00,1,16.5\n"
    )


def publish(client, text: str) -> dict:
    response = upload(client, text)
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview["status"] == "preview"
    confirm = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert confirm.status_code == 200, confirm.text
    return preview


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200
    assert "小时观测发布台" in response.text


def test_preview_classifies_add_replace_ignore(client):
    first = publish(client, sample_rows())
    result = client.get(f"/api/previews/{first['preview_id']}").json()
    assert result["status"] == "published"

    updated = (
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n"
        "SY-001,2026-11-13T01:00:00,3,11.5\n"
        "SY-002,2026-11-13T00:00:00,3,18.0\n"
        "SY-002,2026-11-13T01:00:00,0,16.5\n"
    )
    response = upload(client, updated)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["counts"] == {
        "added": 0,
        "replaced": 2,
        "ignored": 2,
        "conflicts": 0,
        "pending": 2,
        "rows": 4,
    }
    actions = {(r["station"], r["hour"], r["revision"]): r["action"] for r in data["records"]}
    assert actions[("SY-001", "00:00", 2)] == "replace"
    assert actions[("SY-001", "01:00", 3)] == "replace"
    assert actions[("SY-002", "00:00", 3)] == "ignore"
    assert actions[("SY-002", "01:00", 0)] == "ignore"
    reasons = {r["station"] + r["hour"]: r["reason"] for r in data["records"]}
    assert reasons["SY-00200:00"] == "already_published"
    assert reasons["SY-00201:00"] == "older_than_published"

    confirm = client.post(f"/api/previews/{data['preview_id']}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["result"]["replaced"] == 2

    day = client.get("/api/days/2026-11-13").json()
    values = {(r["station"], r["hour"]): (r["revision"], r["value"]) for r in day["records"]}
    assert values[("SY-001", "00:00")] == (2, "12.9")
    assert values[("SY-001", "01:00")] == (3, "11.5")
    assert values[("SY-002", "00:00")] == (3, "18")
    assert values[("SY-002", "01:00")] == (1, "16.5")
    assert day["day_version"] == 2
    assert day["count"] == 4


def test_highest_revision_wins_and_repeats_ignored(client):
    preview = publish(client, sample_rows())
    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    by_hour = {(r["station"], r["hour"]): r for r in day["records"]}
    assert by_hour[("SY-001", "01:00")]["revision"] == 2
    assert by_hour[("SY-001", "01:00")]["value"] == "11.2"

    result = client.get(f"/api/previews/{preview['preview_id']}").json()
    ignored = [r for r in result["records"] if r["action"] == "ignore"]
    assert any(r["reason"] == "superseded_in_batch" for r in ignored)
    assert any(r["reason"] == "duplicate_in_batch" for r in ignored)


def test_conflict_rejects_entire_batch_without_changing_data(client):
    publish(client, sample_rows())

    conflicting = (
        "station,timestamp,revision,value\n"
        "SY-003,2026-11-13T02:00:00,1,25.0\n"
        "SY-001,2026-11-13T01:00:00,2,11.9\n"
        "SY-001,2026-11-13T01:00:00,2,12.3\n"
    )
    response = upload(client, conflicting)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "batch_conflict"
    detail = body["detail"]
    assert detail["status"] == "conflict"
    assert detail["preview_id"] is None
    assert detail["counts"]["conflicts"] == 2
    assert detail["counts"]["pending"] == 0
    assert detail["counts"]["added"] == 1  # classified, but never written
    assert detail["conflicts"][0]["revision"] == 2
    assert set(detail["conflicts"][0]["values"]) == {"11.9", "12.3"}

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert all(r["station"] != "SY-003" for r in day["records"])
    assert day["day_version"] == 1


def test_same_revision_as_published_with_different_value_conflicts(client):
    publish(client, sample_rows())
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-002,2026-11-13T00:00:00,3,18.5\n",
    )
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"][0]["reason"] == (
        "revision_value_mismatch_with_published"
    )
    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert next(r for r in day["records"] if r["station"] == "SY-002")["value"] == "18"


def test_format_errors_never_create_preview_or_touch_published_data(client):
    publish(client, sample_rows())

    bad_cases = [
        ("missing column", "station,timestamp,revision\nSY-001,2026-11-13T00:00:00,1\n"),
        ("bad hour", "station,timestamp,revision,value\nSY-001,2026-11-13T00:30:00,1,9\n"),
        ("bad value", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1,abc\n"),
        ("negative value", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1,-1\n"),
        ("bad revision", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1.5,9\n"),
        ("two days", "station,timestamp,revision,value\n"
                     "SY-001,2026-11-13T00:00:00,1,9\n"
                     "SY-001,2026-11-14T00:00:00,1,9\n"),
        ("empty file", ""),
        ("header only", "station,timestamp,revision,value\n"),
    ]
    for label, text in bad_cases:
        response = upload(client, text)
        assert response.status_code == 400, (label, response.text)
        body = response.json()
        assert body["error"] == "csv_format_error", label
        assert body["detail"]["errors"], label
        assert body["detail"]["errors"][0]["message"], label

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert day["day_version"] == 1
    assert client.get("/api/days/2026-11-14").json()["count"] == 0


def test_stale_preview_after_someone_else_publishes(client):
    publish(client, sample_rows())

    ours = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n",
    )
    assert ours.status_code == 201
    our_preview_id = ours.json()["preview_id"]

    other = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-002,2026-11-13T05:00:00,1,8.0\n",
    )
    assert other.status_code == 201
    other_id = other.json()["preview_id"]
    assert client.post(f"/api/previews/{other_id}/confirm").status_code == 200

    confirm = client.post(f"/api/previews/{our_preview_id}/confirm")
    assert confirm.status_code == 409
    assert confirm.json()["error"] == "preview_stale"

    day = client.get("/api/days/2026-11-13").json()
    values = {(r["station"], r["hour"]): r["value"] for r in day["records"]}
    assert values[("SY-001", "00:00")] == "12.4"  # our stale write never applied
    assert values[("SY-002", "05:00")] == "8"
    assert day["day_version"] == 2

    # A stale preview stays stale on later attempts.
    again = client.post(f"/api/previews/{our_preview_id}/confirm")
    assert again.status_code == 409
    assert client.get(f"/api/previews/{our_preview_id}").json()["status"] == "stale"

    # Re-uploading now yields a fresh preview that can be published.
    retry = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n",
    )
    assert retry.status_code == 201
    retry_id = retry.json()["preview_id"]
    assert client.post(f"/api/previews/{retry_id}/confirm").status_code == 200
    assert client.get("/api/days/2026-11-13").json()["day_version"] == 3


def test_repeated_confirm_does_not_republish(client):
    publish(client, sample_rows())
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-003,2026-11-13T03:00:00,1,21.0\n",
    ).json()

    first = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert first.status_code == 200
    assert first.json()["result"]["published"] == 1

    second = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert second.status_code == 200
    assert second.json()["already_published"] is True
    assert second.json()["result"] == first.json()["result"]

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 5
    assert day["day_version"] == 2


def test_unknown_preview_is_404(client):
    response = client.post("/api/previews/does-not-exist/confirm")
    assert response.status_code == 404
    assert response.json()["error"] == "preview_not_found"
    assert client.get("/api/previews/does-not-exist").status_code == 404


def test_day_query_validation_and_empty_day(client):
    assert client.get("/api/days/2026-11-13").json() == {
        "date": "2026-11-13", "day_version": 0, "count": 0, "records": [],
    }
    assert client.get("/api/days/2026-13-13").status_code == 400
    assert client.get("/api/days/not-a-date").status_code == 400
    download = client.get("/api/days/2026-11-13.json")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]


def test_reuploaded_identical_rows_are_ignored_and_create_no_version(client):
    publish(client, sample_rows())
    response = upload(client, sample_rows())
    assert response.status_code == 201
    data = response.json()
    assert data["counts"]["pending"] == 0
    assert data["counts"]["ignored"] == 6
    assert data["day_version"] == 1


def test_value_canonicalization(client):
    publish(client, sample_rows())
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.40\n",
    )
    assert response.status_code == 201
    assert response.json()["counts"]["replaced"] == 1
    assert client.post(
        f"/api/previews/{response.json()['preview_id']}/confirm"
    ).status_code == 200
    day = client.get("/api/days/2026-11-13").json()
    record = next(r for r in day["records"] if (r["station"], r["hour"]) == ("SY-001", "00:00"))
    assert record["value"] == "12.4"


def test_publish_failure_rolls_back_all_writes(client, monkeypatch):
    publish(client, sample_rows())
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,5,99\n"
        "SY-002,2026-11-13T06:00:00,1,7\n",
    ).json()

    import app.service as service

    original = service._current_version

    def fail_mid_transaction(connection, date):
        # Simulate a storage failure after the first write has been staged.
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "_current_version", fail_mid_transaction)
    response = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert response.status_code == 500
    assert response.json()["error"] == "storage_error"
    monkeypatch.setattr(service, "_current_version", original)

    day = client.get("/api/days/2026-11-13").json()
    assert day["day_version"] == 1
    assert day["count"] == 4
    assert client.get(f"/api/previews/{preview['preview_id']}").json()["status"] == "preview"

    # The same preview can still be published once storage recovers.
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm").status_code == 200


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-13T00:00",
        "2026-11-13 00:00:00Z",
        "2026-11-13T00:00:00.000",
        "2026-11-13T00:00:00+00:00",
    ],
)
def test_supported_timestamp_variants(client, timestamp):
    response = upload(
        client,
        f"station,timestamp,revision,value\nSY-001,{timestamp},1,9.5\n",
    )
    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-11-13"
    assert response.json()["records"][0]["timestamp"] == "2026-11-13T00:00:00"


def test_date_column_with_time_only_cells(client):
    response = upload(
        client,
        "station,date,time,revision,value\n"
        "SY-001,2026-11-13,00:00,1,9.5\n"
        "SY-001,2026-11-13,05:00,1,10.5\n",
    )
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["added"] == 2


def test_chinese_headers_semicolon_and_gb18030(client):
    text = (
        "站点;整点时间;修订号;数值\n"
        "SY-001;2026-11-13T00:00:00;1;9.5\n"
        "SY-002;2026-11-13T01:00:00;2;11\n"
    )
    response = client.post(
        "/api/previews",
        files={"file": ("day.csv", text.encode("gb18030"), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["added"] == 2


def test_row_level_errors_are_all_reported(client):
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        ",2026-11-13T00:00:00,1,9.5\n"
        "SY-001,2026-11-13T01:00:00,,9.5\n"
        "SY-002,2026-11-13T02:00:00,1,notanumber\n",
    )
    assert response.status_code == 400
    errors = response.json()["detail"]["errors"]
    assert response.json()["detail"]["error_count"] == 3
    codes = {item["code"] for item in errors}
    assert codes == {"empty_station", "empty_revision", "invalid_value"}


def test_tabs_and_ignore_of_identical_revision_inside_batch(client):
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,9.5\n"
        "SY-001,2026-11-13T00:00:00,2,9.50\n"
        "SY-001,2026-11-13T00:00:00,1,9.0\n",
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["counts"] == {
        "added": 1, "replaced": 0, "ignored": 2, "conflicts": 0,
        "pending": 1, "rows": 3,
    }
    reasons = [record["reason"] for record in data["records"]]
    # 9.50 canonicalises to 9.5, so the second row is a pure duplicate.
    assert reasons == ["new", "duplicate_in_batch", "superseded_in_batch"]


def test_two_previews_for_same_day_cannot_both_commit(client):
    from concurrent.futures import ThreadPoolExecutor

    publish(client, sample_rows())
    first = upload(
        client,
        "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,2,12.9\n",
    ).json()
    second = upload(
        client,
        "station,timestamp,revision,value\nSY-002,2026-11-13T00:00:00,4,18.5\n",
    ).json()

    def confirm(preview_id):
        response = client.post(f"/api/previews/{preview_id}/confirm")
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(confirm, [first["preview_id"], second["preview_id"]]))

    assert sorted(statuses) == [200, 409]
    day = client.get("/api/days/2026-11-13").json()
    assert day["day_version"] == 2  # exactly one batch was applied
    applied = [
        sid for sid, status in zip(
            [first["preview_id"], second["preview_id"]], statuses
        ) if status == 200
    ]
    assert client.get(f"/api/previews/{applied[0]}").json()["status"] == "published"
