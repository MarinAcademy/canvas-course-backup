#!/usr/bin/env python3
"""
Backup Canvas courses as IMS Common Cartridge (.imscc) exports.

Environment variables:
  CANVAS_BASE_URL    Example: https://school.instructure.com
  CANVAS_API_TOKEN   Canvas access token for an account admin

The script automatically loads a .env file from the current directory when
present. Values already set in the shell take precedence.

Examples:
  # Auto-discover current courses in the root account and back them up.
  CANVAS_BASE_URL=https://school.instructure.com CANVAS_API_TOKEN=... \
    ./canvas_course_backup.py --output-dir ./canvas-backups --workers 3

  # Back up specific course IDs.
  ./canvas_course_backup.py --base-url https://school.instructure.com \
    --course-ids 123,456,789 --output-dir ./canvas-backups

  # Back up courses from a file with one Canvas course ID per line.
  ./canvas_course_backup.py --course-file courses.txt --output-dir ./canvas-backups

  # Discover current courses for a known enrollment term.
  ./canvas_course_backup.py --account-id self --enrollment-term-id 42
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Iterable


LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
LOG_LOCK = Lock()


class CanvasError(RuntimeError):
    pass


@dataclass
class BackupResult:
    course_id: int
    course_name: str
    teachers: str
    filename: str
    status: str
    export_id: int | None = None
    message: str = ""


def log(message: str, *, file: Any = sys.stdout) -> None:
    with LOG_LOCK:
        print(message, file=file, flush=True)


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as input_file:
        for line_number, raw_line in enumerate(input_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                raise CanvasError(f"Invalid .env line {line_number}: expected KEY=VALUE")

            key, value = line.split("=", 1)
            key = key.strip()
            value = parse_dotenv_value(value.strip())

            if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise CanvasError(f"Invalid .env variable name on line {line_number}: {key!r}")
            os.environ.setdefault(key, value)


def parse_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            return bytes(value, "utf-8").decode("unicode_escape")
        return value

    return strip_unquoted_comment(value).strip()


def strip_unquoted_comment(value: str) -> str:
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


class CanvasClient:
    def __init__(self, base_url: str, token: str, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        body, _headers = self._request_json("GET", path_or_url, params=params)
        return body

    def post_json(self, path: str, data: dict[str, Any]) -> Any:
        body, _headers = self._request_json("POST", path, data=data)
        return body

    def get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        url: str | None = path
        query = params
        records: list[dict[str, Any]] = []

        while url:
            body, headers = self._request_json("GET", url, params=query)
            if not isinstance(body, list):
                raise CanvasError(f"Expected a list response from {url}, got {type(body).__name__}")
            records.extend(body)
            links = self._parse_link_header(headers.get("Link", ""))
            url = links.get("next")
            query = None

        return records

    def download(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_destination = destination.with_suffix(destination.suffix + ".part")
        request = self._build_request("GET", url, accept="application/octet-stream")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                with tmp_destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CanvasError(f"HTTP {exc.code} while downloading export: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise CanvasError(f"Network error while downloading export: {exc.reason}") from exc

        tmp_destination.replace(destination)

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        request = self._build_request(method, path_or_url, params=params, data=data)

        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return None, dict(response.headers.items())
                    return json.loads(raw.decode("utf-8")), dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    self._sleep_before_retry(attempt, retry_after)
                    continue
                body = exc.read().decode("utf-8", errors="replace")
                raise CanvasError(f"HTTP {exc.code} {method} {path_or_url}: {body[:1000]}") from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    self._sleep_before_retry(attempt, None)
                    continue
                raise CanvasError(f"Network error {method} {path_or_url}: {exc.reason}") from exc

        raise CanvasError(f"Request failed after retries: {method} {path_or_url}")

    def _build_request(
        self,
        method: str,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> urllib.request.Request:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        if params:
            encoded_params = urllib.parse.urlencode(params, doseq=True)
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{encoded_params}"

        encoded_data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "User-Agent": "canvas-course-backup/1.0",
        }

        if data is not None:
            encoded_data = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        return urllib.request.Request(url, data=encoded_data, headers=headers, method=method)

    @staticmethod
    def _parse_link_header(link_header: str) -> dict[str, str]:
        return {rel: url for url, rel in LINK_RE.findall(link_header)}

    @staticmethod
    def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            delay = int(retry_after)
        else:
            delay = min(2**attempt, 30)
        time.sleep(delay)


def parse_args(argv: list[str]) -> argparse.Namespace:
    if not any(arg in ("-h", "--help") for arg in argv):
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Export Canvas courses as IMS Common Cartridge (.imscc) backup files."
    )
    parser.add_argument("--base-url", default=os.getenv("CANVAS_BASE_URL"), help="Canvas base URL.")
    parser.add_argument(
        "--token",
        default=os.getenv("CANVAS_API_TOKEN"),
        help="Canvas API token. Prefer CANVAS_API_TOKEN so it is not stored in shell history.",
    )
    parser.add_argument(
        "--account-id",
        default="self",
        help="Canvas account ID for auto-discovery. Default: self.",
    )
    parser.add_argument(
        "--course-id",
        action="append",
        default=[],
        help="Canvas course ID to back up. Can be repeated.",
    )
    parser.add_argument(
        "--course-ids",
        default="",
        help="Comma- or whitespace-separated Canvas course IDs to back up.",
    )
    parser.add_argument(
        "--course-file",
        type=Path,
        help="Text or CSV file containing Canvas course IDs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("canvas-course-backups"),
        help="Directory where .imscc files and manifest are written.",
    )
    parser.add_argument(
        "--enrollment-term-id",
        action="append",
        default=[],
        help="Enrollment term ID to include during auto-discovery. Can be repeated.",
    )
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        choices=["created", "claimed", "available", "completed", "deleted", "all"],
        help="Course workflow state for auto-discovery. Default: available.",
    )
    parser.add_argument(
        "--no-date-filter",
        action="store_true",
        help="Do not filter auto-discovered courses to courses/terms active today.",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include courses with no enrollments during auto-discovery.",
    )
    parser.add_argument(
        "--published-only",
        action="store_true",
        help="Only auto-discover published courses.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing .imscc file instead of skipping it.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Seconds between export status checks. Default: 15.",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=90,
        help="Maximum minutes to wait for each course export. Default: 90.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help=(
            "Number of courses to export/download concurrently. Use 1 for sequential "
            "backups. Default: 2."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching courses and target filenames without creating exports.",
    )
    args = parser.parse_args(argv)
    if args.state is None:
        args.state = ["available"]
    return args


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except CanvasError as exc:
        log(f"Error: {exc}", file=sys.stderr)
        return 2

    if not args.base_url:
        log("Missing --base-url or CANVAS_BASE_URL.", file=sys.stderr)
        return 2
    if not args.token:
        log("Missing --token or CANVAS_API_TOKEN.", file=sys.stderr)
        return 2
    if args.workers < 1:
        log("--workers must be 1 or greater.", file=sys.stderr)
        return 2

    client = CanvasClient(args.base_url, args.token)
    course_ids = collect_course_ids(args)

    try:
        if course_ids:
            log(f"Fetching metadata for {len(course_ids)} explicitly requested course(s)...")
            courses = get_courses(client, course_ids, args.workers)
        else:
            log("Auto-discovering current courses from the Canvas account...")
            courses = discover_courses(client, args)
    except CanvasError as exc:
        log(f"Error: {exc}", file=sys.stderr)
        return 1

    if not courses:
        log("No courses matched the request.")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Found {len(courses)} course(s). Output directory: {args.output_dir.resolve()}")

    results = backup_courses(client, courses, args)

    write_manifest(args.output_dir / "canvas_backup_manifest.csv", results)
    failures = [result for result in results if result.status == "failed"]
    skipped = [result for result in results if result.status == "skipped"]
    completed = [result for result in results if result.status in ("downloaded", "dry-run")]
    log(
        f"Summary: {len(completed)} completed, {len(skipped)} skipped, "
        f"{len(failures)} failed. Manifest: {(args.output_dir / 'canvas_backup_manifest.csv').resolve()}"
    )
    return 1 if failures else 0


def backup_courses(
    client: CanvasClient,
    courses: list[dict[str, Any]],
    args: argparse.Namespace,
) -> list[BackupResult]:
    total = len(courses)
    workers = min(args.workers, total)

    if workers == 1:
        results: list[BackupResult] = []
        for index, course in enumerate(courses, start=1):
            result = backup_course(client, course, args, index, total)
            results.append(result)
            log(f"[{result.status}] course {result.course_id}: {result.message}")
        return results

    log(f"Backing up with up to {workers} concurrent course export(s).")
    ordered_results: list[BackupResult | None] = [None] * total
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(backup_course, client, course, args, index, total): (index, course)
            for index, course in enumerate(courses, start=1)
        }
        for future in as_completed(futures):
            index, course = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = make_failed_result(course, f"Unexpected error: {exc}")
            ordered_results[index - 1] = result
            log(f"[{result.status}] course {result.course_id}: {result.message}")

    return [result for result in ordered_results if result is not None]


def get_courses(client: CanvasClient, course_ids: list[int], workers: int) -> list[dict[str, Any]]:
    if workers == 1 or len(course_ids) == 1:
        return [get_course(client, course_id) for course_id in course_ids]

    ordered_courses: list[dict[str, Any] | None] = [None] * len(course_ids)
    with ThreadPoolExecutor(max_workers=min(workers, len(course_ids))) as executor:
        futures = {
            executor.submit(get_course, client, course_id): index
            for index, course_id in enumerate(course_ids)
        }
        for future in as_completed(futures):
            index = futures[future]
            ordered_courses[index] = future.result()

    return [course for course in ordered_courses if course is not None]


def make_failed_result(course: dict[str, Any], message: str) -> BackupResult:
    course_id = int(course.get("id") or 0)
    course_name = str(course.get("name") or course.get("course_code") or f"course-{course_id}")
    teacher_names = get_teacher_names(course)
    return BackupResult(
        course_id=course_id,
        course_name=course_name,
        teachers=", ".join(teacher_names) if teacher_names else "No teachers",
        filename=build_filename(course, teacher_names) if course_id else "",
        status="failed",
        message=message,
    )


def collect_course_ids(args: argparse.Namespace) -> list[int]:
    raw_values: list[str] = []
    raw_values.extend(args.course_id)
    if args.course_ids:
        raw_values.extend(re.split(r"[\s,]+", args.course_ids.strip()))
    if args.course_file:
        raw_values.extend(read_course_ids_from_file(args.course_file))

    course_ids: list[int] = []
    seen: set[int] = set()
    for value in raw_values:
        if not value:
            continue
        match = re.search(r"\d+", value)
        if not match:
            raise SystemExit(f"Could not parse a course ID from: {value}")
        course_id = int(match.group(0))
        if course_id not in seen:
            seen.add(course_id)
            course_ids.append(course_id)
    return course_ids


def read_course_ids_from_file(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"Course file does not exist: {path}")

    values: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as input_file:
        sample = input_file.read(4096)
        input_file.seek(0)
        if "," in sample:
            reader = csv.reader(input_file)
            for row in reader:
                values.extend(cell.strip() for cell in row if cell.strip())
        else:
            values.extend(line.strip() for line in input_file if line.strip())
    return values


def discover_courses(client: CanvasClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    today = dt.date.today().isoformat()
    params: dict[str, Any] = {
        "per_page": 100,
        "include[]": ["teachers", "term"],
        "state[]": args.state,
        "completed": "false",
        "sort": "course_name",
        "order": "asc",
    }

    if not args.include_empty:
        params["with_enrollments"] = "true"
    if args.published_only:
        params["published"] = "true"
    if args.enrollment_term_id:
        params["enrollment_term_id[]"] = args.enrollment_term_id
    if not args.no_date_filter:
        params["starts_before"] = today
        params["ends_after"] = today

    return client.get_paginated(f"/api/v1/accounts/{args.account_id}/courses", params=params)


def get_course(client: CanvasClient, course_id: int) -> dict[str, Any]:
    course = client.get_json(
        f"/api/v1/courses/{course_id}",
        params={"include[]": ["teachers", "term"]},
    )
    if not isinstance(course, dict):
        raise CanvasError(f"Unexpected course response for {course_id}")
    if not course.get("teachers"):
        course["teachers"] = get_teacher_summaries(client, course_id)
    return course


def get_teacher_summaries(client: CanvasClient, course_id: int) -> list[dict[str, Any]]:
    teachers = client.get_paginated(
        f"/api/v1/courses/{course_id}/users",
        params={
            "per_page": 100,
            "enrollment_type[]": ["teacher"],
            "enrollment_state[]": ["active"],
        },
    )
    return teachers


def backup_course(
    client: CanvasClient,
    course: dict[str, Any],
    args: argparse.Namespace,
    index: int,
    total: int,
) -> BackupResult:
    course_id = int(course["id"])
    course_name = str(course.get("name") or course.get("course_code") or f"course-{course_id}")
    teacher_names = get_teacher_names(course)
    filename = build_filename(course, teacher_names)
    destination = args.output_dir / filename
    teacher_text = ", ".join(teacher_names) if teacher_names else "No teachers"

    if destination.exists() and not args.overwrite:
        return BackupResult(
            course_id=course_id,
            course_name=course_name,
            teachers=teacher_text,
            filename=filename,
            status="skipped",
            message=f"{destination.name} already exists",
        )

    log(f"({index}/{total}) Exporting {course_name} [{course_id}]...")
    if args.dry_run:
        return BackupResult(
            course_id=course_id,
            course_name=course_name,
            teachers=teacher_text,
            filename=filename,
            status="dry-run",
            message=f"would write {destination.name}",
        )

    try:
        export = start_export(client, course_id)
        export_id = int(export["id"])
        completed_export = wait_for_export(
            client,
            course_id,
            export_id,
            poll_interval=args.poll_interval,
            timeout_minutes=args.timeout_minutes,
        )
        attachment_url = get_attachment_url(completed_export)
        client.download(attachment_url, destination)
    except CanvasError as exc:
        return BackupResult(
            course_id=course_id,
            course_name=course_name,
            teachers=teacher_text,
            filename=filename,
            status="failed",
            export_id=locals().get("export_id"),
            message=str(exc),
        )

    return BackupResult(
        course_id=course_id,
        course_name=course_name,
        teachers=teacher_text,
        filename=filename,
        status="downloaded",
        export_id=export_id,
        message=f"saved {destination.name}",
    )


def start_export(client: CanvasClient, course_id: int) -> dict[str, Any]:
    export = client.post_json(
        f"/api/v1/courses/{course_id}/content_exports",
        data={"export_type": "common_cartridge", "skip_notifications": "true"},
    )
    if not isinstance(export, dict) or "id" not in export:
        raise CanvasError(f"Unexpected export response for course {course_id}: {export}")
    return export


def wait_for_export(
    client: CanvasClient,
    course_id: int,
    export_id: int,
    poll_interval: int,
    timeout_minutes: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_minutes * 60
    while time.monotonic() < deadline:
        export = client.get_json(f"/api/v1/courses/{course_id}/content_exports/{export_id}")
        state = export.get("workflow_state")
        if state == "exported":
            return export
        if state == "failed":
            raise CanvasError(f"Export {export_id} failed for course {course_id}")
        log(
            f"  course {course_id} export {export_id} state: {state}; "
            f"checking again in {poll_interval}s"
        )
        time.sleep(poll_interval)

    raise CanvasError(
        f"Timed out after {timeout_minutes} minutes waiting for export {export_id} "
        f"for course {course_id}"
    )


def get_attachment_url(export: dict[str, Any]) -> str:
    attachment = export.get("attachment")
    if not isinstance(attachment, dict) or not attachment.get("url"):
        raise CanvasError("Export finished but did not include a downloadable attachment URL")
    return str(attachment["url"])


def get_teacher_names(course: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for teacher in course.get("teachers") or []:
        if not isinstance(teacher, dict):
            continue
        name = teacher.get("display_name") or teacher.get("name") or teacher.get("sortable_name")
        if name and str(name) not in names:
            names.append(str(name))
    return names


def build_filename(course: dict[str, Any], teacher_names: list[str]) -> str:
    course_id = int(course["id"])
    course_name = str(course.get("name") or course.get("course_code") or f"course-{course_id}")
    course_code = str(course.get("course_code") or "").strip()
    teacher_part = ", ".join(teacher_names) if teacher_names else "No teachers"

    if course_code and course_code != course_name:
        course_part = f"{course_name} [{course_code}]"
    else:
        course_part = course_name

    base = f"{course_part} - {teacher_part} - course_{course_id}"
    sanitized = sanitize_filename(base)
    if len(sanitized) > 180:
        sanitized = f"{sanitized[:160].rstrip()} - course_{course_id}"
    return f"{sanitized}.imscc"


def sanitize_filename(value: str) -> str:
    value = UNSAFE_FILENAME_RE.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "canvas-course-export"


def write_manifest(path: Path, results: Iterable[BackupResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=[
                "course_id",
                "course_name",
                "teachers",
                "filename",
                "status",
                "export_id",
                "message",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "course_id": result.course_id,
                    "course_name": result.course_name,
                    "teachers": result.teachers,
                    "filename": result.filename,
                    "status": result.status,
                    "export_id": result.export_id or "",
                    "message": result.message,
                }
            )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
