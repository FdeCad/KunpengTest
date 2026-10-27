#!/usr/bin/env python3
"""Validate Kunpeng test-result example files without third-party dependencies.

结果文件直接复用 openeuler-docker-images 的目录，与 Dockerfile 同级：

    <应用分类>/<应用名>/<应用版本>/<openEuler 版本>/<hardware>-<YYYYMMDDTHHMMSSZ>.json

检查项：
  * 每个结果文件是合法 JSON，且符合文档中的字段契约
  * 目录中的应用名 / 应用版本与文件字段一致
  * 文件名中的 hardware / test_time 与字段一致
  * upstream_unsupported 与 unsupported_reasons 一致
  * schema 文件可解析

退出码 0 = 全部通过，1 = 存在错误。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "examples" / "results"
SCHEMA = ROOT / "schema" / "test-result.schema.json"

HARDWARE = {"kunpeng920", "kunpeng950"}
CATEGORY = {"functional", "performance"}
RESULT = {"pass", "fail", "skip", "blocked", "error"}
RUN_STATUS = {"completed", "aborted", "error"}
SOFTWARE_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}$")
REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        err(f"{rel(path)}: invalid JSON: {exc}")
        return None


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def parse_dt(value, where: str):
    if not isinstance(value, str):
        err(f"{where}: timestamp must be a string")
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        err(f"{where}: invalid ISO 8601 timestamp: {value!r}")
        return None
    if dt.tzinfo is None:
        warn(f"{where}: timestamp has no timezone, treated as UTC")
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compact_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        err(f"{where}: unknown field(s): {', '.join(unknown)}")


def check_result(path: Path, data: dict) -> None:
    where = rel(path)
    if not isinstance(data, dict):
        err(f"{where}: top-level value must be an object")
        return

    allowed = {
        "schema_version", "software", "version", "hardware",
        "test_time", "finished_at", "run_status",
        "upstream_unsupported", "unsupported_reasons", "test_cases",
        "job_url", "extensions",
    }
    check_keys(data, allowed, where)

    for key in (
        "schema_version", "software", "version", "hardware",
        "test_time", "upstream_unsupported", "unsupported_reasons", "test_cases",
    ):
        if key not in data:
            err(f"{where}: missing required field {key}")

    if data.get("schema_version") != "1.0":
        err(f"{where}.schema_version: expected '1.0'")

    software = data.get("software")
    if not isinstance(software, str) or not SOFTWARE_RE.match(software):
        err(f"{where}.software: invalid value {software!r}")

    version = data.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        err(f"{where}.version: invalid value {version!r}")

    hardware = data.get("hardware")
    if hardware not in HARDWARE:
        err(f"{where}.hardware: invalid value {hardware!r}")

    run_start = parse_dt(data.get("test_time"), f"{where}.test_time")
    run_end = None
    if "finished_at" in data:
        run_end = parse_dt(data["finished_at"], f"{where}.finished_at")
        if run_start and run_end and run_end < run_start:
            err(f"{where}.finished_at: earlier than test_time")

    run_status = data.get("run_status", "completed")
    if run_status not in RUN_STATUS:
        err(f"{where}.run_status: invalid value {run_status!r}")

    # 目录与文件名：<分类>/<应用名>/<应用版本>/<openEuler 版本>/<hardware>-<时间>.json
    try:
        parts = path.relative_to(RESULTS).parts
    except ValueError:
        parts = ()
    if len(parts) < 5:
        err(
            f"{where}: expected "
            f"<category>/<software>/<version>/<oe-version>/<file>.json"
        )
    else:
        filename = parts[-1]
        dir_version = parts[-3]
        dir_software = parts[-4]
        if dir_software != software:
            err(f"{where}: directory software {dir_software!r} != field {software!r}")
        if dir_version != version:
            err(f"{where}: directory version {dir_version!r} != field {version!r}")
        if run_start:
            expected = f"{hardware}-{compact_time(run_start)}"
            stem = filename[:-5] if filename.endswith(".json") else filename
            pattern = re.compile(
                "^" + re.escape(expected) + r"(-[A-Za-z0-9]{6})?$"
            )
            if not pattern.match(stem):
                err(
                    f"{where}: filename stem {stem!r} does not match "
                    f"{expected!r} (or expected + -<6 alnum>)"
                )

    unsupported = data.get("upstream_unsupported")
    if not isinstance(unsupported, bool):
        err(f"{where}.upstream_unsupported: must be boolean")
    reasons = data.get("unsupported_reasons")
    if not isinstance(reasons, list):
        err(f"{where}.unsupported_reasons: must be an array")
        reasons = []
    if unsupported is True and not reasons:
        err(f"{where}: upstream_unsupported=true but unsupported_reasons is empty")
    if unsupported is False and reasons:
        err(f"{where}: upstream_unsupported=false but unsupported_reasons is not empty")
    for i, reason in enumerate(reasons):
        rw = f"{where}.unsupported_reasons[{i}]"
        if not isinstance(reason, dict):
            err(f"{rw}: must be an object")
            continue
        check_keys(reason, {"code", "message", "evidence", "checked_at"}, rw)
        code = reason.get("code")
        if not isinstance(code, str) or not REASON_RE.match(code):
            err(f"{rw}.code: invalid value {code!r}")
        if not isinstance(reason.get("message"), str) or not reason.get("message"):
            err(f"{rw}.message: required non-empty string")
        if "evidence" in reason:
            evidence = reason["evidence"]
            if not isinstance(evidence, list) or not all(
                isinstance(x, str) for x in evidence
            ):
                err(f"{rw}.evidence: must be an array of strings")
        if "checked_at" in reason:
            parse_dt(reason["checked_at"], f"{rw}.checked_at")

    cases = data.get("test_cases")
    if not isinstance(cases, list):
        err(f"{where}.test_cases: must be an array")
        cases = []
    if len(cases) > 5000:
        err(f"{where}.test_cases: exceeds 5000 items")

    names: set[str] = set()
    for i, case in enumerate(cases):
        cw = f"{where}.test_cases[{i}]"
        if not isinstance(case, dict):
            err(f"{cw}: must be an object")
            continue
        check_keys(
            case,
            {"id", "name", "description", "category", "result", "test_time",
             "duration_ms", "message", "log_url", "metrics", "thresholds"},
            cw,
        )
        for key in ("name", "description", "category", "result", "test_time"):
            if key not in case:
                err(f"{cw}: missing required field {key}")
        name = case.get("name")
        if not isinstance(name, str) or not name:
            err(f"{cw}.name: required non-empty string")
        elif name in names:
            err(f"{cw}.name: duplicate case name {name!r}")
        else:
            names.add(name)
        if not isinstance(case.get("description"), str) or not case.get("description"):
            err(f"{cw}.description: required non-empty string")
        if case.get("category") not in CATEGORY:
            err(f"{cw}.category: invalid value {case.get('category')!r}")
        if case.get("result") not in RESULT:
            err(f"{cw}.result: invalid value {case.get('result')!r}")
        case_time = parse_dt(case.get("test_time"), f"{cw}.test_time")
        if case_time and run_start:
            if case_time < run_start - timedelta(minutes=5):
                warn(f"{cw}.test_time: earlier than run test_time")
        if case_time and run_end and case_time > run_end + timedelta(minutes=5):
            warn(f"{cw}.test_time: later than finished_at")
        if "id" in case and (
            not isinstance(case["id"], str) or not CASE_ID_RE.match(case["id"])
        ):
            err(f"{cw}.id: invalid value {case['id']!r}")
        if "duration_ms" in case and (
            not is_number(case["duration_ms"]) or case["duration_ms"] < 0
        ):
            err(f"{cw}.duration_ms: must be a non-negative number")
        metrics = case.get("metrics")
        if metrics is not None:
            if not isinstance(metrics, dict) or not all(
                is_number(v) for v in metrics.values()
            ):
                err(f"{cw}.metrics: must be an object of numbers")
        thresholds = case.get("thresholds")
        if thresholds is not None:
            if not isinstance(thresholds, dict):
                err(f"{cw}.thresholds: must be an object")
            else:
                for key, value in thresholds.items():
                    if not isinstance(value, dict) or not set(value) <= {"min", "max"}:
                        err(f"{cw}.thresholds.{key}: must contain only min/max")
                    elif not value or not all(is_number(v) for v in value.values()):
                        err(f"{cw}.thresholds.{key}: min/max must be numbers")

    if run_status == "completed" and not cases:
        warn(f"{where}: completed run has no test_cases")


def validate_results() -> dict[Path, dict]:
    files = sorted(RESULTS.rglob("*.json"))
    if not files:
        err("examples/results: no JSON files found")
        return {}
    loaded = {}
    for path in files:
        data = load_json(path)
        if data is not None:
            loaded[path] = data
            check_result(path, data)
    return loaded


def main() -> int:
    if not SCHEMA.is_file():
        err("schema/test-result.schema.json: missing")
    else:
        load_json(SCHEMA)

    results = validate_results()

    for message in warnings:
        print(f"WARN  {message}")
    for message in errors:
        print(f"ERROR {message}")

    print()
    print(f"result files : {len(results)}")
    print(f"warnings     : {len(warnings)}")
    print(f"errors       : {len(errors)}")
    if errors:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
