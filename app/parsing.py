"""CSV parsing and validation for single-day PM2.5 observation batches."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DATA_ROWS = 20_000
MAX_ERRORS = 50
MAX_STATION_LENGTH = 120
MAX_REVISION = 2_147_483_647

CSV_DELIMITERS = ",;\t|"

_HEADER_ALIASES = {
    "station": {
        "station", "site", "stationid", "stationname", "站点", "站点名称",
        "站点编号", "监测站点", "监测点", "站号", "站点名",
    },
    "timestamp": {
        "timestamp", "datetime", "time", "observedat", "observationtime",
        "hour", "hourtime", "时间", "整点时间", "观测时间", "时间戳",
        "观测时刻", "日期时间",
    },
    "revision": {
        "revision", "rev", "version", "修订号", "修订版本", "版本号", "修订",
    },
    "value": {
        "value", "pm25", "pm2.5", "pm2.5浓度", "数值", "浓度", "测量值",
        "观测值", "pm25浓度", "pm25值", "pm2.5值",
    },
    "date": {"date", "day", "日期", "观测日期"},
}

_FULL_DATETIME = re.compile(
    r"^([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})[ T]([0-9]{1,2})"
    r"(?::([0-9]{2})(?::([0-9]{2})(?:\.([0-9]+))?)?)?(?:Z|[+-]00:?00)?$"
)
_TIME_ONLY = re.compile(
    r"^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]+))?)?$"
)
_DATE_ONLY = re.compile(r"^([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})$")
_REVISION = re.compile(r"^\+?[0-9]+$")
_DECIMAL = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


class CsvFormatError(Exception):
    """Raised when the uploaded batch cannot be parsed at all."""

    def __init__(self, message: str, errors: list[dict], error_count: int | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors
        self.error_count = error_count if error_count is not None else len(errors)


@dataclass(frozen=True)
class Observation:
    row_number: int
    station: str
    date: str
    hour: str
    revision: int
    value: str  # canonical non-scientific decimal string

    @property
    def timestamp(self) -> str:
        return f"{self.date}T{self.hour}:00"

    @property
    def key(self) -> tuple[str, str]:
        return (self.station, self.hour)

    def as_internal(self) -> dict:
        return {
            "row": self.row_number,
            "station": self.station,
            "date": self.date,
            "hour": self.hour,
            "revision": self.revision,
            "value": self.value,
        }


@dataclass
class ParsedBatch:
    date: str
    observations: list[Observation]


def _canonical_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().lower().replace("﻿", ""))


_CANONICAL_ALIASES = {
    logical: {_canonical_header(alias) for alias in aliases}
    for logical, aliases in _HEADER_ALIASES.items()
}


def _decode(raw: bytes) -> str:
    if not raw:
        raise CsvFormatError(
            "CSV 文件为空",
            [{"row": None, "field": None, "code": "empty_file", "message": "CSV 文件为空"}],
        )
    if b"\x00" in raw:
        raise CsvFormatError(
            "文件不是有效的文本 CSV",
            [{"row": None, "field": None, "code": "binary_file",
              "message": "文件中包含二进制空字符，请上传文本 CSV"}],
        )
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError(
        "无法识别文件编码，请使用 UTF-8 或 GB18030",
        [{"row": None, "field": None, "code": "invalid_encoding",
          "message": "无法识别文件编码，请使用 UTF-8 或 GB18030"}],
    )


def _detect_delimiter(text: str) -> str:
    sample = "\n".join(line for line in text.splitlines()[:20] if line.strip())
    try:
        return csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        # Fall back to the delimiter that appears most often in the header line.
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        counts = {delimiter: first_line.count(delimiter) for delimiter in CSV_DELIMITERS}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def _error(row: int | None, field: str | None, code: str, message: str) -> dict:
    return {"row": row, "field": field, "code": code, "message": message}


def _parse_date(value: str) -> date_type | None:
    match = _DATE_ONLY.match(value.strip())
    if not match:
        return None
    try:
        return date_type(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_observation_time(value: str, row_date: str | None) -> tuple[str, str] | None:
    """Return (date, 'HH:00') or None when invalid."""
    text = value.strip()
    if not text:
        return None

    full = _FULL_DATETIME.match(text)
    if full:
        year, month, day, hour = (int(part) for part in full.group(1, 2, 3, 4))
        minute = int(full.group(5) or 0)
        second = int(full.group(6) or 0)
        fraction = full.group(7) or "0"
        if minute != 0 or second != 0 or int(fraction) != 0:
            return None
        try:
            parsed_date = date_type(year, month, day)
            parsed_datetime = datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour)
        except ValueError:
            return None
        if row_date is not None and parsed_date.isoformat() != row_date:
            return None
        return parsed_date.isoformat(), f"{parsed_datetime.hour:02d}:00"

    time_only = _TIME_ONLY.match(text)
    if time_only:
        hour = int(time_only.group(1))
        minute = int(time_only.group(2))
        second = int(time_only.group(3) or 0)
        fraction = time_only.group(4) or "0"
        if minute != 0 or second != 0 or int(fraction) != 0 or hour > 23 or row_date is None:
            return None
        return row_date, f"{hour:02d}:00"

    return None


def _parse_decimal(value: str) -> tuple[str | None, str | None]:
    text = value.strip()
    if not text:
        return None, "数值不能为空"
    if not _DECIMAL.match(text):
        return None, f"数值 {value!r} 不是有效数字"
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, f"数值 {value!r} 不是有效数字"
    if not number.is_finite():
        return None, "数值必须是有限数字"
    if number < 0:
        return None, "PM2.5 数值不能为负数"
    if abs(number) > Decimal("1000000000"):
        return None, "数值超出支持范围（绝对值不得超过 1e9）"
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -9:
        return None, "数值小数位过多（最多 9 位）"
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in ("", "-0", "+0"):
        canonical = "0"
    return canonical, None


def parse_csv(raw: bytes) -> ParsedBatch:
    if len(raw) > MAX_FILE_BYTES:
        raise CsvFormatError(
            "CSV 文件过大",
            [_error(None, None, "file_too_large",
                    f"文件不得超过 {MAX_FILE_BYTES // 1024 // 1024} MiB")],
        )

    text = _decode(raw)
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    header: list[str] | None = None
    header_line = 0
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        header = row
        header_line = reader.line_num
        break

    if header is None:
        raise CsvFormatError(
            "CSV 缺少表头",
            [_error(None, None, "missing_header", "CSV 文件只有空行，缺少表头")],
        )

    normalized_headers = [_canonical_header(cell) for cell in header]
    indices: dict[str, int] = {}
    header_errors: list[dict] = []

    for index, canonical in enumerate(normalized_headers):
        matched: str | None = None
        for logical, aliases in _CANONICAL_ALIASES.items():
            if canonical in aliases:
                matched = logical
                break
        if matched is None and canonical.startswith(("pm2.5", "pm25", "pm2_5")):
            matched = "value"
        if matched is None:
            continue
        if matched in indices:
            header_errors.append(
                _error(header_line, matched, "duplicate_column",
                       f"表头包含多个“{matched}”列")
            )
        else:
            indices[matched] = index

    missing = [field for field in ("station", "timestamp", "revision", "value")
               if field not in indices]
    for field in missing:
        label = {
            "station": "站点", "timestamp": "整点时间",
            "revision": "修订号", "value": "数值",
        }[field]
        header_errors.append(
            _error(header_line, field, "missing_column", f"缺少必需列：{label}")
        )

    if header_errors:
        raise CsvFormatError("CSV 表头格式错误", header_errors)

    observations: list[Observation] = []
    errors: list[dict] = list(header_errors)
    error_count = 0
    batch_date: str | None = None
    data_rows = 0

    def add_error(row: int | None, field: str | None, code: str, message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_ERRORS:
            errors.append(_error(row, field, code, message))

    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        line_number = reader.line_num
        data_rows += 1
        if data_rows > MAX_DATA_ROWS:
            add_error(line_number, None, "too_many_rows",
                      f"单次最多上传 {MAX_DATA_ROWS} 行观测记录")
            break

        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        elif len(row) > len(header):
            extras = row[len(header):]
            if any(cell.strip() for cell in extras):
                add_error(line_number, None, "too_many_columns",
                          "该行列数多于表头列数")
                continue
            row = row[:len(header)]

        station_value = row[indices["station"]].strip()
        if not station_value:
            add_error(line_number, "station", "empty_station", "站点不能为空")
        elif len(station_value) > MAX_STATION_LENGTH:
            add_error(line_number, "station", "station_too_long",
                      f"站点名称不得超过 {MAX_STATION_LENGTH} 个字符")
        elif any(ord(char) < 32 and char not in "\t" for char in station_value):
            add_error(line_number, "station", "invalid_station",
                      "站点名称包含非法控制字符")

        row_date_from_column: str | None = None
        if "date" in indices:
            raw_date = row[indices["date"]].strip()
            parsed_date = _parse_date(raw_date)
            if parsed_date is None:
                add_error(line_number, "date", "invalid_date",
                          f"日期 {raw_date!r} 不是有效的 YYYY-MM-DD")
            else:
                row_date_from_column = parsed_date.isoformat()

        revision_text = row[indices["revision"]].strip()
        revision: int | None = None
        if not revision_text:
            add_error(line_number, "revision", "empty_revision", "修订号不能为空")
        elif not _REVISION.match(revision_text):
            add_error(line_number, "revision", "invalid_revision",
                      f"修订号 {revision_text!r} 必须是非负整数")
        else:
            revision = int(revision_text)
            if revision > MAX_REVISION:
                add_error(line_number, "revision", "revision_out_of_range",
                          f"修订号不得超过 {MAX_REVISION}")
                revision = None

        value, value_error = _parse_decimal(row[indices["value"]])
        if value_error:
            add_error(line_number, "value", "invalid_value", value_error)

        timestamp_text = row[indices["timestamp"]].strip()
        parsed_time = _parse_observation_time(timestamp_text, row_date_from_column)
        if parsed_time is None:
            add_error(
                line_number, "timestamp", "invalid_timestamp",
                f"整点时间 {timestamp_text!r} 格式无效"
                "（需为完整日期时间，或配合 date 列使用 HH:00）",
            )

        if station_value and revision is not None and value and parsed_time:
            row_date, row_hour = parsed_time
            if batch_date is None:
                batch_date = row_date
            if batch_date != row_date:
                add_error(
                    line_number, "timestamp", "multiple_dates",
                    f"每次上传仅允许单日数据（本批日期为 {batch_date}，此行日期为 {row_date}）",
                )
            else:
                observations.append(
                    Observation(
                        row_number=line_number,
                        station=station_value,
                        date=row_date,
                        hour=row_hour,
                        revision=revision,
                        value=value,
                    )
                )

    if error_count:
        raise CsvFormatError(
            "CSV 格式错误，整批未创建预览",
            errors,
            error_count=error_count,
        )
    if not observations:
        raise CsvFormatError(
            "CSV 没有可发布的数据行",
            [_error(header_line, None, "no_data", "CSV 没有可发布的数据行")],
        )
    if batch_date is None:
        raise CsvFormatError(
            "无法确定数据日期",
            [_error(None, "timestamp", "missing_date", "无法从整点时间确定观测日期")],
        )

    return ParsedBatch(date=batch_date, observations=observations)
