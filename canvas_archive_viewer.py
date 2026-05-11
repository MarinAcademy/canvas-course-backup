#!/usr/bin/env python3
"""
Create portable, read-only Canvas course archives with a static emergency viewer.

This companion script keeps the IMS Common Cartridge export, then adds API
snapshots for rosters, grades, assignments, submissions, discussions, modules,
and files. The output contains sensitive student data; keep it out of git.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from xml.etree import ElementTree
from urllib.parse import unquote, urlsplit

from canvas_course_backup import (
    CanvasClient,
    CanvasError,
    collect_course_ids,
    discover_courses,
    get_attachment_url,
    get_courses,
    get_teacher_names,
    load_dotenv,
    log,
    sanitize_filename,
    start_export,
    wait_for_export,
)


CANVAS_FILE_RE = re.compile(r"/files/(\d+)(?:/download)?")
SCRIPT_RE = re.compile(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", re.IGNORECASE)
EVENT_ATTR_RE = re.compile(r"\s+on[a-zA-Z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
JAVASCRIPT_URL_RE = re.compile(r"\s+(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


@dataclass
class ArchiveResult:
    course_id: int
    course_name: str
    bundle_dir: str
    status: str
    imscc_status: str = ""
    viewer_status: str = ""
    failed_categories: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ArchiveContext:
    client: CanvasClient | None
    course: dict[str, Any]
    args: argparse.Namespace
    root: Path
    data_dir: Path
    files_dir: Path
    viewer_dir: Path
    issues: list[dict[str, str]] = field(default_factory=list)
    local_links_by_url: dict[str, str] = field(default_factory=dict)
    local_links_by_file_id: dict[str, str] = field(default_factory=dict)

    @property
    def course_id(self) -> int:
        return int(self.course["id"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    if not any(arg in ("-h", "--help") for arg in argv):
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Archive Canvas courses, grades, submissions, files, and a static viewer."
    )
    parser.add_argument("--base-url", default=os.getenv("CANVAS_BASE_URL"), help="Canvas base URL.")
    parser.add_argument(
        "--token",
        default=os.getenv("CANVAS_API_TOKEN"),
        help="Canvas API token. Prefer CANVAS_API_TOKEN or .env.",
    )
    parser.add_argument(
        "--account-id",
        default="self",
        help="Canvas account ID for auto-discovery. Default: self.",
    )
    parser.add_argument("--course-id", action="append", default=[], help="Course ID. Repeatable.")
    parser.add_argument("--course-ids", default="", help="Comma- or whitespace-separated course IDs.")
    parser.add_argument("--course-file", type=Path, help="Text or CSV file containing course IDs.")
    parser.add_argument(
        "--archive-output-dir",
        type=Path,
        default=Path("canvas-archives"),
        help="Directory where archive bundles are written. Default: canvas-archives.",
    )
    parser.add_argument(
        "--enrollment-term-id",
        action="append",
        default=[],
        help="Enrollment term ID for auto-discovery. Repeatable.",
    )
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        choices=["created", "claimed", "available", "completed", "deleted", "all"],
        help="Course workflow state for auto-discovery. Default: available.",
    )
    parser.add_argument("--no-date-filter", action="store_true", help="Do not filter to active courses.")
    parser.add_argument("--include-empty", action="store_true", help="Include courses with no enrollments.")
    parser.add_argument("--published-only", action="store_true", help="Only auto-discover published courses.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing downloaded files.")
    parser.add_argument("--poll-interval", type=int, default=15, help="IMSCC export poll interval.")
    parser.add_argument("--timeout-minutes", type=int, default=90, help="Per-course IMSCC timeout.")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent course archives. Default: 2.")
    parser.add_argument(
        "--include-discussions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Archive discussion topics, entries, replies, and attachments. Default: true.",
    )
    parser.add_argument(
        "--include-course-files",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Archive course file metadata and downloads. Default: true.",
    )
    parser.add_argument(
        "--include-submission-attachments",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download file attachments from submissions. Default: true.",
    )
    parser.add_argument("--skip-imscc", action="store_true", help="Skip IMSCC export/download.")
    parser.add_argument(
        "--generate-viewer",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Generate static HTML viewer. Default: true.",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        help="Build a static viewer from existing data/*.json fixture files without calling Canvas.",
    )
    parser.add_argument(
        "--validate-archive",
        type=Path,
        help="Validate an existing course archive bundle without calling Canvas.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List target bundles without archiving.")

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

    if args.workers < 1:
        log("--workers must be 1 or greater.", file=sys.stderr)
        return 2

    if args.validate_archive:
        return validate_archive_command(args.validate_archive)

    if args.fixture_dir:
        return build_viewer_from_fixture(args)

    if not args.base_url:
        log("Missing --base-url or CANVAS_BASE_URL.", file=sys.stderr)
        return 2
    if not args.token:
        log("Missing --token or CANVAS_API_TOKEN.", file=sys.stderr)
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

    args.archive_output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Found {len(courses)} course(s). Output directory: {args.archive_output_dir.resolve()}")

    results = archive_courses(client, courses, args)
    write_archive_manifest(args.archive_output_dir / "canvas_archive_manifest.csv", results)
    failures = [result for result in results if result.status == "failed"]
    partials = [result for result in results if result.status == "partial"]
    completed = [result for result in results if result.status in ("archived", "dry-run")]
    log(
        f"Summary: {len(completed)} archived, {len(partials)} partial, {len(failures)} failed. "
        f"Manifest: {(args.archive_output_dir / 'canvas_archive_manifest.csv').resolve()}"
    )
    return 1 if failures else 0


def archive_courses(
    client: CanvasClient, courses: list[dict[str, Any]], args: argparse.Namespace
) -> list[ArchiveResult]:
    total = len(courses)
    workers = min(args.workers, total)
    if workers == 1:
        results = []
        for index, course in enumerate(courses, start=1):
            result = archive_course(client, course, args, index, total)
            log_archive_result(result)
            results.append(result)
        return results

    log(f"Archiving with up to {workers} concurrent course archive(s).")
    ordered_results: list[ArchiveResult | None] = [None] * total
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(archive_course, client, course, args, index, total): index
            for index, course in enumerate(courses, start=1)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                course = courses[index - 1]
                result = ArchiveResult(
                    course_id=int(course.get("id") or 0),
                    course_name=str(course.get("name") or course.get("course_code") or "unknown"),
                    bundle_dir="",
                    status="failed",
                    message=f"Unexpected error: {exc}",
                )
            ordered_results[index - 1] = result
            log_archive_result(result)
    return [result for result in ordered_results if result is not None]


def archive_course(
    client: CanvasClient,
    course: dict[str, Any],
    args: argparse.Namespace,
    index: int,
    total: int,
) -> ArchiveResult:
    course_id = int(course["id"])
    course_name = str(course.get("name") or course.get("course_code") or f"course-{course_id}")
    root = course_bundle_dir(args.archive_output_dir, course)
    ctx = ArchiveContext(
        client=client,
        course=course,
        args=args,
        root=root,
        data_dir=root / "data",
        files_dir=root / "files",
        viewer_dir=root / "viewer",
    )

    log(f"({index}/{total}) Archiving {course_name} [{course_id}]...")
    if args.dry_run:
        return ArchiveResult(
            course_id=course_id,
            course_name=course_name,
            bundle_dir=str(root),
            status="dry-run",
            message=f"would write {root}",
        )

    for directory in (ctx.data_dir, ctx.files_dir, ctx.viewer_dir):
        directory.mkdir(parents=True, exist_ok=True)

    category_status: dict[str, str] = {}
    imscc_status = "skipped" if args.skip_imscc else archive_imscc(ctx)
    if imscc_status in ("downloaded", "exists"):
        category_status["imscc_content"] = run_category(
            ctx, "imscc_content", lambda: archive_imscc_content(ctx)
        )
    else:
        category_status["imscc_content"] = imscc_status

    write_json(ctx.data_dir / "course.json", course)
    category_status["modules"] = run_category(ctx, "modules", lambda: archive_modules(ctx))
    category_status["enrollments"] = run_category(ctx, "enrollments", lambda: archive_enrollments(ctx))
    category_status["assignments"] = run_category(ctx, "assignments", lambda: archive_assignments(ctx))
    category_status["submissions"] = run_category(ctx, "submissions", lambda: archive_submissions(ctx))

    if args.include_discussions:
        category_status["discussions"] = run_category(ctx, "discussions", lambda: archive_discussions(ctx))
    else:
        category_status["discussions"] = "skipped"
        write_json(ctx.data_dir / "discussions.json", {"topics": [], "skipped": True})

    if args.include_course_files:
        category_status["files"] = run_category(ctx, "files", lambda: archive_course_files(ctx))
    else:
        category_status["files"] = "skipped"
        write_json(ctx.data_dir / "files.json", {"files": [], "skipped": True})

    write_json(ctx.data_dir / "issues.json", ctx.issues)
    viewer_status = "skipped"
    if args.generate_viewer:
        viewer_status = run_category(ctx, "viewer", lambda: generate_viewer(ctx))
        write_json(ctx.data_dir / "issues.json", ctx.issues)

    failed_categories = [
        name for name, status in category_status.items() if status == "failed"
    ]
    if imscc_status == "failed":
        failed_categories.append("imscc")
    if viewer_status == "failed":
        failed_categories.append("viewer")
    for issue in ctx.issues:
        category = issue.get("category")
        if category and category not in failed_categories:
            failed_categories.append(category)

    status = "archived" if not failed_categories else "partial"
    return ArchiveResult(
        course_id=course_id,
        course_name=course_name,
        bundle_dir=str(root),
        status=status,
        imscc_status=imscc_status,
        viewer_status=viewer_status,
        failed_categories=failed_categories,
        message="archive complete" if status == "archived" else "some categories failed",
    )


def archive_imscc(ctx: ArchiveContext) -> str:
    destination = ctx.root / "course.imscc"
    if destination.exists() and not ctx.args.overwrite:
        return "exists"

    assert ctx.client is not None
    try:
        log(f"  course {ctx.course_id}: starting IMSCC export")
        export = start_export(ctx.client, ctx.course_id)
        completed_export = wait_for_export(
            ctx.client,
            ctx.course_id,
            int(export["id"]),
            poll_interval=ctx.args.poll_interval,
            timeout_minutes=ctx.args.timeout_minutes,
        )
        log(f"  course {ctx.course_id}: IMSCC export ready")
        ctx.client.download(
            get_attachment_url(completed_export),
            destination,
            progress_label=f"course {ctx.course_id} IMSCC",
        )
        return "downloaded"
    except CanvasError as exc:
        ctx.issues.append({"category": "imscc", "message": str(exc)})
        return "failed"


def archive_imscc_content(ctx: ArchiveContext) -> None:
    imscc_path = ctx.root / "course.imscc"
    if not imscc_path.exists():
        write_json(ctx.data_dir / "imscc.json", {"resources": [], "organizations": [], "missing": True})
        return

    extract_dir = ctx.files_dir / "imscc_content"
    extract_dir.mkdir(parents=True, exist_ok=True)
    safe_extract_zip(imscc_path, extract_dir)
    manifest_path = extract_dir / "imsmanifest.xml"
    if not manifest_path.exists():
        write_json(
            ctx.data_dir / "imscc.json",
            {"resources": [], "organizations": [], "error": "imsmanifest.xml not found"},
        )
        return

    write_json(ctx.data_dir / "imscc.json", parse_imscc_manifest(manifest_path))


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if destination_root not in target.parents and target != destination_root:
                raise CanvasError(f"Unsafe path in IMSCC archive: {member.filename}")
            archive.extract(member, destination)


def parse_imscc_manifest(manifest_path: Path) -> dict[str, Any]:
    tree = ElementTree.parse(manifest_path)
    root = tree.getroot()
    resources = []
    for resource in find_elements_by_local_name(root, "resource"):
        files = [
            child.attrib.get("href")
            for child in list(resource)
            if local_name(child.tag) == "file" and child.attrib.get("href")
        ]
        resources.append(
            {
                "identifier": resource.attrib.get("identifier"),
                "type": resource.attrib.get("type"),
                "href": resource.attrib.get("href"),
                "files": files,
            }
        )

    organizations = []
    for organization in find_elements_by_local_name(root, "organization"):
        for item in list(organization):
            if local_name(item.tag) == "item":
                organizations.append(parse_manifest_item(item))
    return {"resources": resources, "organizations": organizations}


def parse_manifest_item(item: ElementTree.Element) -> dict[str, Any]:
    title = ""
    children = []
    for child in list(item):
        if local_name(child.tag) == "title":
            title = child.text or ""
        elif local_name(child.tag) == "item":
            children.append(parse_manifest_item(child))
    return {
        "identifier": item.attrib.get("identifier"),
        "identifierref": item.attrib.get("identifierref"),
        "title": title,
        "children": children,
    }


def find_elements_by_local_name(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if local_name(element.tag) == name]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def run_category(ctx: ArchiveContext, category: str, func: Callable[[], Any]) -> str:
    log(f"  course {ctx.course_id}: {category} started")
    try:
        func()
        log(f"  course {ctx.course_id}: {category} complete")
        return "ok"
    except CanvasError as exc:
        ctx.issues.append({"category": category, "message": str(exc)})
        write_json(ctx.data_dir / f"{category}.error.json", {"error": str(exc)})
        log(f"  course {ctx.course_id}: {category} failed: {exc}")
        return "failed"
    except Exception as exc:
        ctx.issues.append({"category": category, "message": f"Unexpected error: {exc}"})
        write_json(ctx.data_dir / f"{category}.error.json", {"error": f"Unexpected error: {exc}"})
        log(f"  course {ctx.course_id}: {category} failed: {exc}")
        return "failed"


def archive_modules(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    modules = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/modules",
        params={"per_page": 100, "include[]": ["items", "content_details"]},
    )
    for module in modules:
        if "items" not in module:
            module["items"] = ctx.client.get_paginated(
                f"/api/v1/courses/{ctx.course_id}/modules/{module['id']}/items",
                params={"per_page": 100, "include[]": ["content_details"]},
            )
    write_json(ctx.data_dir / "modules.json", {"modules": modules})


def archive_enrollments(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    enrollments = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/enrollments",
        params={
            "per_page": 100,
            "include[]": ["user", "avatar_url", "current_points"],
            "state[]": ["active", "invited", "creation_pending", "completed", "inactive"],
        },
    )
    write_json(ctx.data_dir / "enrollments.json", {"enrollments": enrollments})


def archive_assignments(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    assignment_groups = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/assignment_groups",
        params={"per_page": 100, "include[]": ["assignments"]},
    )
    assignments = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/assignments",
        params={"per_page": 100, "include[]": ["all_dates", "overrides", "score_statistics"]},
    )
    write_json(
        ctx.data_dir / "assignments.json",
        {"assignment_groups": assignment_groups, "assignments": assignments},
    )


def archive_submissions(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    assignments = read_json(ctx.data_dir / "assignments.json").get("assignments", [])
    existing = read_json(ctx.data_dir / "submissions.json")
    all_submissions: list[dict[str, Any]] = existing.get("submissions", []) if isinstance(existing, dict) else []
    errors: list[dict[str, str]] = existing.get("errors", []) if isinstance(existing, dict) else []
    completed_assignment_ids = {
        int(value)
        for value in (existing.get("completed_assignment_ids", []) if isinstance(existing, dict) else [])
    }
    archived_assignment_ids = {
        int(submission["assignment_id"])
        for submission in all_submissions
        if str(submission.get("assignment_id", "")).isdigit()
    }
    completed_assignment_ids.update(archived_assignment_ids)
    log(f"  course {ctx.course_id}: fetching submissions for {len(assignments)} assignment(s)")

    for index, assignment in enumerate(assignments, start=1):
        assignment_id = int(assignment["id"])
        assignment_name = assignment.get("name") or f"assignment {assignment_id}"
        if assignment_id in completed_assignment_ids and not ctx.args.overwrite:
            log(
                f"  course {ctx.course_id}: submissions {index}/{len(assignments)} "
                f"already archived, skipping {assignment_name} [{assignment_id}]"
            )
            continue
        log(
            f"  course {ctx.course_id}: submissions {index}/{len(assignments)} "
            f"{assignment_name} [{assignment_id}]"
        )
        try:
            submissions = ctx.client.get_paginated(
                f"/api/v1/courses/{ctx.course_id}/assignments/{assignment_id}/submissions",
                params={
                    "per_page": 100,
                    "include[]": [
                        "submission_history",
                        "submission_comments",
                        "rubric_assessment",
                        "assignment",
                        "user",
                    ],
                },
            )
            for submission in submissions:
                submission["assignment_id"] = assignment_id
                if ctx.args.include_submission_attachments:
                    download_submission_attachments(ctx, assignment_id, submission)
            all_submissions = [
                submission
                for submission in all_submissions
                if int(submission.get("assignment_id") or 0) != assignment_id
            ]
            all_submissions.extend(submissions)
            completed_assignment_ids.add(assignment_id)
            write_submissions_checkpoint(
                ctx.data_dir / "submissions.json",
                all_submissions,
                errors,
                completed_assignment_ids,
            )
            log(
                f"  course {ctx.course_id}: submissions {index}/{len(assignments)} "
                f"complete ({len(submissions)} submission record(s))"
            )
        except CanvasError as exc:
            errors.append({"assignment_id": str(assignment_id), "message": str(exc)})
            log(
                f"  course {ctx.course_id}: submissions {index}/{len(assignments)} "
                f"failed for assignment {assignment_id}: {exc}"
            )
            write_submissions_checkpoint(
                ctx.data_dir / "submissions.json",
                all_submissions,
                errors,
                completed_assignment_ids,
            )

    write_submissions_checkpoint(
        ctx.data_dir / "submissions.json",
        all_submissions,
        errors,
        completed_assignment_ids,
    )
    if errors:
        ctx.issues.extend({"category": "submissions", **error} for error in errors)


def write_submissions_checkpoint(
    path: Path,
    submissions: list[dict[str, Any]],
    errors: list[dict[str, str]],
    completed_assignment_ids: set[int],
) -> None:
    write_json(
        path,
        {
            "submissions": submissions,
            "errors": errors,
            "completed_assignment_ids": sorted(completed_assignment_ids),
        },
    )


def download_submission_attachments(
    ctx: ArchiveContext, assignment_id: int, submission: dict[str, Any]
) -> None:
    for attachment in submission_attachments(submission):
        if not isinstance(attachment, dict):
            continue
        download_attachment(
            ctx,
            attachment,
            Path("submission_attachments")
            / f"assignment_{assignment_id}"
            / f"user_{submission.get('user_id', 'unknown')}",
        )


def submission_attachments(submission: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = []
    seen: set[str] = set()

    for attachment in submission.get("attachments") or []:
        if isinstance(attachment, dict):
            key = str(attachment.get("id") or attachment.get("url") or len(seen))
            if key not in seen:
                seen.add(key)
                attachments.append(attachment)

    for history_item in submission.get("submission_history") or []:
        if not isinstance(history_item, dict):
            continue
        for attachment in history_item.get("attachments") or []:
            if isinstance(attachment, dict):
                key = str(attachment.get("id") or attachment.get("url") or len(seen))
                if key not in seen:
                    seen.add(key)
                    attachments.append(attachment)

    return attachments


def archive_discussions(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    topics = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/discussion_topics",
        params={"per_page": 100, "include[]": ["all_dates", "sections", "sections_user_count"]},
    )
    for topic in topics:
        topic_id = int(topic["id"])
        log(f"  course {ctx.course_id}: discussion topic {topic_id} entries")
        try:
            topic["entries"] = ctx.client.get_paginated(
                f"/api/v1/courses/{ctx.course_id}/discussion_topics/{topic_id}/entries",
                params={"per_page": 100},
            )
            for entry in topic["entries"]:
                download_discussion_entry_attachments(ctx, topic_id, entry)
                if entry.get("has_more_replies"):
                    entry["recent_replies"] = ctx.client.get_paginated(
                        f"/api/v1/courses/{ctx.course_id}/discussion_topics/{topic_id}/entries/{entry['id']}/replies",
                        params={"per_page": 100},
                    )
                for reply in entry.get("recent_replies") or []:
                    download_discussion_entry_attachments(ctx, topic_id, reply)
        except CanvasError as exc:
            topic["archive_error"] = str(exc)
            ctx.issues.append(
                {"category": "discussions", "topic_id": str(topic_id), "message": str(exc)}
            )
    write_json(ctx.data_dir / "discussions.json", {"topics": topics})


def download_discussion_entry_attachments(
    ctx: ArchiveContext, topic_id: int, entry: dict[str, Any]
) -> None:
    if isinstance(entry.get("attachment"), dict):
        download_attachment(
            ctx,
            entry["attachment"],
            Path("discussion_attachments") / f"topic_{topic_id}" / f"entry_{entry['id']}",
        )


def archive_course_files(ctx: ArchiveContext) -> None:
    assert ctx.client is not None
    files = ctx.client.get_paginated(
        f"/api/v1/courses/{ctx.course_id}/files",
        params={"per_page": 100, "include[]": ["user"]},
    )
    log(f"  course {ctx.course_id}: downloading {len(files)} course file(s)")
    for file_record in files:
        download_attachment(ctx, file_record, Path("course_files"))
    write_json(ctx.data_dir / "files.json", {"files": files})


def download_attachment(ctx: ArchiveContext, attachment: dict[str, Any], relative_dir: Path) -> None:
    url = attachment.get("url")
    if not url:
        return

    file_id = str(attachment.get("id") or attachment.get("file_id") or "")
    filename = str(
        attachment.get("display_name")
        or attachment.get("filename")
        or attachment.get("name")
        or f"file_{file_id or int(time.time())}"
    )
    local_name = sanitize_filename(f"{file_id}_{filename}" if file_id else filename)
    relative_path = PurePosixPath("files", *relative_dir.parts, local_name).as_posix()
    destination = ctx.root / relative_path

    if destination.exists() and not ctx.args.overwrite:
        mark_attachment_downloaded(ctx, attachment, str(url), file_id, relative_path)
        return

    try:
        assert ctx.client is not None
        ctx.client.download(str(url), destination)
        mark_attachment_downloaded(ctx, attachment, str(url), file_id, relative_path)
    except CanvasError as exc:
        attachment["intended_local_path"] = relative_path
        attachment["archive_download_error"] = str(exc)
        ctx.issues.append(
            {
                "category": "download",
                "file_id": file_id,
                "filename": filename,
                "message": str(exc),
            }
        )


def mark_attachment_downloaded(
    ctx: ArchiveContext,
    attachment: dict[str, Any],
    url: str,
    file_id: str,
    relative_path: str,
) -> None:
    attachment["local_path"] = relative_path
    attachment["downloaded"] = True
    ctx.local_links_by_url[url] = relative_path
    if file_id:
        ctx.local_links_by_file_id[file_id] = relative_path


def generate_viewer(ctx: ArchiveContext) -> None:
    data = load_archive_data(ctx.data_dir)
    ctx.viewer_dir.mkdir(parents=True, exist_ok=True)
    write_text(ctx.viewer_dir / "styles.css", viewer_css())
    write_text(ctx.viewer_dir / "index.html", render_index(ctx, data))
    write_text(ctx.viewer_dir / "content.html", render_content(ctx, data))
    write_text(ctx.viewer_dir / "modules.html", render_modules(ctx, data))
    write_text(ctx.viewer_dir / "assignments.html", render_assignments(ctx, data))
    write_text(ctx.viewer_dir / "gradebook.html", render_gradebook(ctx, data))
    write_text(ctx.viewer_dir / "students.html", render_students(ctx, data))
    write_text(ctx.viewer_dir / "discussions.html", render_discussions(ctx, data))
    write_text(ctx.viewer_dir / "files.html", render_files(ctx, data))


def build_viewer_from_fixture(args: argparse.Namespace) -> int:
    data_dir = args.fixture_dir / "data" if (args.fixture_dir / "data").exists() else args.fixture_dir
    course = read_json(data_dir / "course.json")
    if not course:
        log(f"Missing fixture course.json in {data_dir}", file=sys.stderr)
        return 2
    root = args.archive_output_dir / "fixture-viewer"
    ctx = ArchiveContext(
        client=None,
        course=course,
        args=args,
        root=root,
        data_dir=data_dir,
        files_dir=root / "files",
        viewer_dir=root / "viewer",
    )
    root.mkdir(parents=True, exist_ok=True)
    generate_viewer(ctx)
    log(f"Generated fixture viewer: {ctx.viewer_dir / 'index.html'}")
    return 0


def validate_archive_command(archive_root: Path) -> int:
    report = validate_archive(archive_root)
    log(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["missing_required"] or report["missing_links"] else 0


def validate_archive(archive_root: Path) -> dict[str, Any]:
    required_files = [
        Path("data/course.json"),
        Path("data/enrollments.json"),
        Path("data/assignments.json"),
        Path("data/submissions.json"),
        Path("data/modules.json"),
        Path("data/files.json"),
        Path("data/issues.json"),
        Path("viewer/index.html"),
        Path("viewer/gradebook.html"),
        Path("viewer/students.html"),
        Path("viewer/files.html"),
    ]
    missing_required = [
        path.as_posix() for path in required_files if not (archive_root / path).exists()
    ]
    missing_links = find_missing_viewer_links(archive_root)
    data_counts = archive_data_counts(archive_root / "data")
    return {
        "archive": str(archive_root),
        "status": "ok" if not missing_required and not missing_links else "failed",
        "missing_required": missing_required,
        "missing_links": missing_links,
        "data_counts": data_counts,
        "viewer_html_files": len(list((archive_root / "viewer").glob("*.html"))),
    }


def archive_data_counts(data_dir: Path) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    mapping = {
        "issues": "issues",
        "enrollments": "enrollments",
        "assignments": "assignments",
        "submissions": "submissions",
        "discussions": "topics",
        "files": "files",
        "modules": "modules",
        "imscc": "resources",
    }
    for filename, key in mapping.items():
        data = read_json(data_dir / f"{filename}.json")
        if isinstance(data, list):
            counts[filename] = len(data)
        elif isinstance(data, dict) and isinstance(data.get(key), list):
            counts[filename] = len(data[key])
        else:
            counts[filename] = None
    return counts


def find_missing_viewer_links(archive_root: Path) -> list[dict[str, str]]:
    viewer_dir = archive_root / "viewer"
    if not viewer_dir.exists():
        return [{"source": str(viewer_dir), "target": "", "reason": "viewer directory missing"}]

    missing: list[dict[str, str]] = []
    archive_root_resolved = archive_root.resolve()
    for html_file in viewer_dir.glob("*.html"):
        parser = ViewerLinkParser(html_file, archive_root_resolved, missing)
        parser.feed(html_file.read_text(encoding="utf-8", errors="ignore"))
    return missing


class ViewerLinkParser(HTMLParser):
    def __init__(
        self,
        source: Path,
        archive_root: Path,
        missing: list[dict[str, str]],
    ) -> None:
        super().__init__()
        self.source = source
        self.archive_root = archive_root
        self.missing = missing

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key not in ("href", "src") or not value:
                continue
            if value.startswith(("http:", "https:", "mailto:", "#", "javascript:")):
                continue
            target_path = unquote(urlsplit(value).path)
            target = (self.source.parent / target_path).resolve()
            try:
                target.relative_to(self.archive_root)
            except ValueError:
                self.missing.append(
                    {"source": str(self.source), "target": value, "reason": "outside archive"}
                )
                continue
            if not target.exists():
                self.missing.append(
                    {"source": str(self.source), "target": value, "reason": "missing"}
                )


def load_archive_data(data_dir: Path) -> dict[str, Any]:
    filenames = [
        "course",
        "modules",
        "enrollments",
        "assignments",
        "submissions",
        "discussions",
        "files",
        "imscc",
        "issues",
    ]
    return {name: read_json(data_dir / f"{name}.json") for name in filenames}


def render_index(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    course = data["course"]
    enrollments = data["enrollments"].get("enrollments", [])
    assignments = data["assignments"].get("assignments", [])
    submissions = data["submissions"].get("submissions", [])
    topics = data["discussions"].get("topics", [])
    files = data["files"].get("files", [])
    issues = data["issues"] if isinstance(data["issues"], list) else []
    teachers = get_teacher_names(course)
    cards = [
        stat_card("Students", count_enrollment_type(enrollments, "StudentEnrollment")),
        stat_card("Teachers", count_enrollment_type(enrollments, "TeacherEnrollment") or len(teachers)),
        stat_card("Assignments", len(assignments)),
        stat_card("Submissions", len(submissions)),
        stat_card("Discussions", len(topics)),
        stat_card("Course files", len(files)),
        stat_card("Issues", len(issues)),
    ]
    body = f"""
    <section class="page-header">
      <p class="eyebrow">Canvas archive</p>
      <h1>{escape(course_title(course))}</h1>
      <p>{escape(', '.join(teachers) if teachers else 'No teachers recorded')}</p>
      <p class="muted">Course ID {escape(str(course.get('id', '')))} · Generated {escape(dt.datetime.now().isoformat(timespec='seconds'))}</p>
    </section>
    <section class="stats">{''.join(cards)}</section>
    <section>
      <h2>Archive Status</h2>
      {render_issues(issues)}
    </section>
    """
    return page(ctx, "Overview", body)


def render_content(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    imscc = data["imscc"]
    resources = imscc.get("resources", [])
    resources_by_id = {resource.get("identifier"): resource for resource in resources}
    parts = ["<section><h1>Course Content</h1>"]
    if imscc.get("missing"):
        parts.append("<p class='muted'>IMSCC content was not archived.</p>")
    elif imscc.get("error"):
        parts.append(f"<p class='error'>{escape(imscc['error'])}</p>")
    else:
        organizations = imscc.get("organizations", [])
        if organizations:
            parts.append("<h2>Manifest Outline</h2>")
            parts.append(render_manifest_items(ctx, organizations, resources_by_id))
        if resources:
            parts.append("<h2>Resources</h2><div class='table-wrap'><table><thead><tr><th>Resource</th><th>Type</th><th>Files</th></tr></thead><tbody>")
            for resource in resources:
                href = resource.get("href")
                title = href or resource.get("identifier") or "Resource"
                link = imscc_resource_link(ctx, href, title)
                file_count = len(resource.get("files") or [])
                parts.append(
                    f"<tr><td>{link}<div class='muted'>ID {escape(resource.get('identifier'))}</div></td>"
                    f"<td>{escape(resource.get('type'))}</td><td>{file_count}</td></tr>"
                )
            parts.append("</tbody></table></div>")
        if not organizations and not resources:
            parts.append("<p class='muted'>No IMSCC manifest content found.</p>")
    parts.append("</section>")
    return page(ctx, "Content", "".join(parts))


def render_manifest_items(
    ctx: ArchiveContext, items: list[dict[str, Any]], resources_by_id: dict[str, dict[str, Any]]
) -> str:
    parts = ["<ol>"]
    for item in items:
        resource = resources_by_id.get(item.get("identifierref") or "", {})
        href = resource.get("href")
        title = item.get("title") or href or item.get("identifier") or "Untitled"
        parts.append("<li>")
        parts.append(imscc_resource_link(ctx, href, title))
        if item.get("identifierref"):
            parts.append(f" <span class='muted'>Resource {escape(item.get('identifierref'))}</span>")
        if item.get("children"):
            parts.append(render_manifest_items(ctx, item["children"], resources_by_id))
        parts.append("</li>")
    parts.append("</ol>")
    return "".join(parts)


def imscc_resource_link(ctx: ArchiveContext, href: str | None, title: Any) -> str:
    if not href:
        return escape(title)
    target = ctx.files_dir / "imscc_content" / href
    if target.exists():
        return f"<a href='{escape(relative_url(ctx.viewer_dir, target))}'>{escape(title)}</a>"
    return escape(title)


def render_modules(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    modules = data["modules"].get("modules", [])
    parts = ["<section><h1>Modules</h1>"]
    if not modules:
        parts.append("<p class='muted'>No modules archived.</p>")
    for module in modules:
        parts.append(f"<article class='panel'><h2>{escape(module.get('name'))}</h2><ol>")
        for item in module.get("items") or []:
            title = escape(item.get("title") or item.get("type") or "Untitled item")
            parts.append(
                f"<li><span class='badge'>{escape(item.get('type', 'Item'))}</span> "
                f"{title} <span class='muted'>ID {escape(item.get('id'))}</span></li>"
            )
        parts.append("</ol></article>")
    parts.append("</section>")
    return page(ctx, "Modules", "".join(parts))


def render_assignments(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    assignments = data["assignments"].get("assignments", [])
    rows = []
    for assignment in assignments:
        rows.append(
            "<tr>"
            f"<td>{escape(assignment.get('name'))}<div class='muted'>ID {escape(assignment.get('id'))}</div></td>"
            f"<td>{escape(assignment.get('points_possible'))}</td>"
            f"<td>{escape(assignment.get('due_at'))}</td>"
            f"<td>{escape(', '.join(assignment.get('submission_types') or []))}</td>"
            "</tr>"
        )
    body = table_page(
        "Assignments",
        ["Assignment", "Points", "Due", "Submission Types"],
        rows,
        empty="No assignments archived.",
    )
    return page(ctx, "Assignments", body)


def render_gradebook(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    students = student_enrollments(data)
    assignments = data["assignments"].get("assignments", [])
    submissions_by_key = {
        (str(sub.get("user_id")), str(sub.get("assignment_id"))): sub
        for sub in data["submissions"].get("submissions", [])
    }
    header = ["Student", "Current", "Final"] + [str(a.get("name") or a.get("id")) for a in assignments]
    rows = []
    for enrollment in students:
        user = enrollment.get("user") or {}
        grades = enrollment.get("grades") or {}
        cells = [
            student_link(user),
            escape(grades.get("current_score")),
            escape(grades.get("final_score")),
        ]
        for assignment in assignments:
            sub = submissions_by_key.get((str(user.get("id")), str(assignment.get("id"))), {})
            score = sub.get("score")
            grade = sub.get("grade")
            cells.append(escape(grade if grade not in (None, "") else score))
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return page(ctx, "Gradebook", table_page("Gradebook", header, rows, empty="No student grades archived."))


def render_students(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    students = student_enrollments(data)
    submissions_by_user: dict[str, list[dict[str, Any]]] = {}
    for submission in data["submissions"].get("submissions", []):
        submissions_by_user.setdefault(str(submission.get("user_id")), []).append(submission)

    rows = []
    detail_pages = []
    for enrollment in students:
        user = enrollment.get("user") or {}
        user_id = str(user.get("id"))
        user_submissions = submissions_by_user.get(user_id, [])
        rows.append(
            "<tr>"
            f"<td>{student_link(user)}</td>"
            f"<td>{escape(user.get('login_id') or user.get('sis_user_id'))}</td>"
            f"<td>{escape((enrollment.get('grades') or {}).get('current_score'))}</td>"
            f"<td>{len(user_submissions)}</td>"
            "</tr>"
        )
        detail_pages.append((student_page_name(user), render_student_detail(ctx, data, user, enrollment, user_submissions)))

    for filename, content in detail_pages:
        write_text(ctx.viewer_dir / filename, content)

    return page(
        ctx,
        "Students",
        table_page("Students", ["Student", "Login/SIS", "Current Score", "Submissions"], rows),
    )


def render_student_detail(
    ctx: ArchiveContext,
    data: dict[str, Any],
    user: dict[str, Any],
    enrollment: dict[str, Any],
    submissions: list[dict[str, Any]],
) -> str:
    assignment_by_id = {
        str(assignment.get("id")): assignment for assignment in data["assignments"].get("assignments", [])
    }
    parts = [
        f"<section><h1>{escape(user.get('sortable_name') or user.get('name'))}</h1>",
        f"<p class='muted'>User ID {escape(user.get('id'))}</p>",
        "<h2>Grades</h2>",
        "<dl class='details'>",
    ]
    for key, value in (enrollment.get("grades") or {}).items():
        parts.append(f"<dt>{escape(key)}</dt><dd>{escape(value)}</dd>")
    parts.append("</dl><h2>Submissions</h2>")
    if not submissions:
        parts.append("<p class='muted'>No submissions archived.</p>")
    for submission in submissions:
        assignment = assignment_by_id.get(str(submission.get("assignment_id")), {})
        parts.append("<article class='panel'>")
        parts.append(f"<h3>{escape(assignment.get('name') or submission.get('assignment_id'))}</h3>")
        parts.append(
            f"<p class='muted'>Score {escape(submission.get('score'))} · Grade {escape(submission.get('grade'))} · "
            f"Submitted {escape(submission.get('submitted_at'))}</p>"
        )
        if submission.get("body"):
            parts.append(f"<div class='content'>{safe_canvas_html(ctx, submission['body'])}</div>")
        parts.append(render_attachment_list(submission.get("attachments") or [], ctx.viewer_dir, ctx.root))
        comments = submission.get("submission_comments") or []
        if comments:
            parts.append("<h4>Comments</h4>")
            for comment in comments:
                parts.append(
                    f"<blockquote>{safe_canvas_html(ctx, comment.get('comment') or '')}"
                    f"<footer>{escape(comment.get('author_name'))} · {escape(comment.get('created_at'))}</footer></blockquote>"
                )
        parts.append("</article>")
    parts.append("</section>")
    return page(ctx, str(user.get("name") or "Student"), "".join(parts))


def render_discussions(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    topics = data["discussions"].get("topics", [])
    parts = ["<section><h1>Discussions</h1>"]
    if data["discussions"].get("skipped"):
        parts.append("<p class='muted'>Discussion archiving was skipped.</p>")
    elif not topics:
        parts.append("<p class='muted'>No discussion topics archived.</p>")
    for topic in topics:
        parts.append(f"<article class='panel'><h2>{escape(topic.get('title'))}</h2>")
        if topic.get("message"):
            parts.append(f"<div class='content'>{safe_canvas_html(ctx, topic['message'])}</div>")
        if topic.get("archive_error"):
            parts.append(f"<p class='error'>{escape(topic['archive_error'])}</p>")
        for entry in topic.get("entries") or []:
            parts.append(render_discussion_entry(ctx, entry))
        parts.append("</article>")
    parts.append("</section>")
    return page(ctx, "Discussions", "".join(parts))


def render_discussion_entry(ctx: ArchiveContext, entry: dict[str, Any]) -> str:
    parts = [
        "<div class='discussion-entry'>",
        f"<p><strong>{escape(entry.get('user_name'))}</strong> <span class='muted'>{escape(entry.get('created_at'))}</span></p>",
        f"<div class='content'>{safe_canvas_html(ctx, entry.get('message') or '')}</div>",
        render_attachment_list([entry["attachment"]] if isinstance(entry.get("attachment"), dict) else [], ctx.viewer_dir, ctx.root),
    ]
    replies = entry.get("recent_replies") or []
    if replies:
        parts.append("<div class='replies'>")
        for reply in replies:
            parts.append(render_discussion_entry(ctx, reply))
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def render_files(ctx: ArchiveContext, data: dict[str, Any]) -> str:
    files = data["files"].get("files", [])
    rows = []
    for file_record in files:
        link = file_link(file_record, ctx.viewer_dir, ctx.root)
        rows.append(
            "<tr>"
            f"<td>{link}</td>"
            f"<td>{escape(file_record.get('content-type'))}</td>"
            f"<td>{escape(file_record.get('size'))}</td>"
            f"<td>{escape(file_record.get('updated_at'))}</td>"
            "</tr>"
        )
    body = table_page("Files", ["File", "Type", "Size", "Updated"], rows, empty="No course files archived.")
    return page(ctx, "Files", body)


def page(ctx: ArchiveContext, title: str, body: str) -> str:
    course = course_title(ctx.course)
    nav = """
      <nav class="sidebar">
        <a href="index.html">Overview</a>
        <a href="content.html">Content</a>
        <a href="modules.html">Modules</a>
        <a href="assignments.html">Assignments</a>
        <a href="gradebook.html">Gradebook</a>
        <a href="students.html">Students</a>
        <a href="discussions.html">Discussions</a>
        <a href="files.html">Files</a>
      </nav>
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · {escape(course)}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {nav}
  <main>{body}</main>
</body>
</html>
"""


def table_page(title: str, headers: list[str], rows: list[str], empty: str = "No data archived.") -> str:
    if not rows:
        return f"<section><h1>{escape(title)}</h1><p class='muted'>{escape(empty)}</p></section>"
    header_html = "".join(f"<th>{escape(header)}</th>" for header in headers)
    return f"<section><h1>{escape(title)}</h1><div class='table-wrap'><table><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"


def viewer_css() -> str:
    return """
:root { color-scheme: light; --ink:#202124; --muted:#5f6368; --line:#dadce0; --bg:#f8fafd; --panel:#fff; --accent:#0b57d0; }
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); }
.sidebar { position: fixed; inset: 0 auto 0 0; width: 220px; padding: 24px 16px; background: #fff; border-right: 1px solid var(--line); display: flex; flex-direction: column; gap: 6px; }
.sidebar a { color: var(--ink); text-decoration: none; padding: 9px 10px; border-radius: 6px; }
.sidebar a:hover { background: #eef3fd; color: var(--accent); }
main { margin-left: 220px; padding: 32px; max-width: 1440px; }
h1, h2, h3 { line-height: 1.2; }
.eyebrow, .muted { color: var(--muted); }
.page-header { border-bottom: 1px solid var(--line); margin-bottom: 24px; padding-bottom: 20px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }
.stat, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
.stat strong { display: block; font-size: 28px; }
.badge { display: inline-block; font-size: 12px; padding: 2px 6px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); }
.table-wrap { overflow: auto; background: #fff; border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; min-width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 9px 10px; text-align: left; vertical-align: top; }
th { background: #f1f3f4; position: sticky; top: 0; }
.details { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; }
.details dt { color: var(--muted); }
.content { max-width: 900px; }
.discussion-entry { border-top: 1px solid var(--line); padding: 12px 0; }
.replies { margin-left: 24px; border-left: 3px solid var(--line); padding-left: 16px; }
blockquote { border-left: 3px solid var(--line); margin: 12px 0; padding-left: 12px; }
footer { color: var(--muted); font-size: 13px; margin-top: 4px; }
.error { color: #b3261e; }
@media (max-width: 760px) {
  .sidebar { position: static; width: auto; flex-direction: row; flex-wrap: wrap; }
  main { margin-left: 0; padding: 20px; }
}
"""


def safe_canvas_html(ctx: ArchiveContext, value: str) -> str:
    cleaned = SCRIPT_RE.sub("", value)
    cleaned = EVENT_ATTR_RE.sub("", cleaned)
    cleaned = JAVASCRIPT_URL_RE.sub("", cleaned)
    return rewrite_canvas_links(ctx, cleaned)


def rewrite_canvas_links(ctx: ArchiveContext, value: str) -> str:
    for source_url, local_path in ctx.local_links_by_url.items():
        value = value.replace(source_url, relative_url(ctx.viewer_dir, ctx.root / local_path))

    def replace_file_match(match: re.Match[str]) -> str:
        file_id = match.group(1)
        local_path = ctx.local_links_by_file_id.get(file_id)
        if not local_path:
            return match.group(0)
        return relative_url(ctx.viewer_dir, ctx.root / local_path)

    return CANVAS_FILE_RE.sub(replace_file_match, value)


def render_attachment_list(attachments: list[dict[str, Any]], from_dir: Path, root: Path) -> str:
    links = []
    for attachment in attachments:
        local_path = attachment.get("local_path")
        label = attachment.get("display_name") or attachment.get("filename") or attachment.get("name") or "attachment"
        if local_path and (root / local_path).exists():
            links.append(f"<li><a href='{escape(relative_url(from_dir, root / local_path))}'>{escape(label)}</a></li>")
        elif attachment.get("url"):
            links.append(f"<li>{escape(label)} <span class='muted'>not downloaded</span></li>")
    return f"<ul>{''.join(links)}</ul>" if links else ""


def render_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "<p>No archive issues recorded.</p>"
    items = [
        f"<li><strong>{escape(issue.get('category'))}</strong>: {escape(issue.get('message'))}</li>"
        for issue in issues
    ]
    return f"<ul class='error'>{''.join(items)}</ul>"


def stat_card(label: str, value: Any) -> str:
    return f"<div class='stat'><span>{escape(label)}</span><strong>{escape(value)}</strong></div>"


def student_enrollments(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        enrollment
        for enrollment in data["enrollments"].get("enrollments", [])
        if enrollment.get("type") == "StudentEnrollment"
    ]


def count_enrollment_type(enrollments: list[dict[str, Any]], enrollment_type: str) -> int:
    return sum(1 for enrollment in enrollments if enrollment.get("type") == enrollment_type)


def student_link(user: dict[str, Any]) -> str:
    filename = student_page_name(user)
    label = user.get("sortable_name") or user.get("name") or f"User {user.get('id')}"
    return f"<a href='{escape(filename)}'>{escape(label)}</a>"


def student_page_name(user: dict[str, Any]) -> str:
    user_id = str(user.get("id") or "unknown")
    return f"student_{sanitize_filename(user_id)}.html"


def file_link(file_record: dict[str, Any], from_dir: Path, root: Path) -> str:
    label = file_record.get("display_name") or file_record.get("filename") or file_record.get("id")
    local_path = file_record.get("local_path")
    if local_path and (root / local_path).exists():
        return f"<a href='{escape(relative_url(from_dir, root / local_path))}'>{escape(label)}</a>"
    return escape(label)


def course_bundle_dir(output_dir: Path, course: dict[str, Any]) -> Path:
    course_id = int(course["id"])
    name = str(course.get("name") or course.get("course_code") or f"course-{course_id}")
    return output_dir / sanitize_filename(f"{name} - course_{course_id}")


def course_title(course: dict[str, Any]) -> str:
    code = str(course.get("course_code") or "").strip()
    name = str(course.get("name") or code or f"Course {course.get('id')}")
    return f"{name} [{code}]" if code and code != name else name


def relative_url(from_dir: Path, target: Path) -> str:
    return PurePosixPath(os.path.relpath(target, from_dir)).as_posix()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output_file:
        json.dump(data, output_file, indent=2, sort_keys=True, ensure_ascii=False)
        output_file.write("\n")


def read_json(path: Path) -> Any:
    if not path.exists():
        return {} if path.name != "issues.json" else []
    with path.open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def log_archive_result(result: ArchiveResult) -> None:
    failed = f" failed={','.join(result.failed_categories)}" if result.failed_categories else ""
    log(f"[{result.status}] course {result.course_id}: {result.message}{failed}")


def write_archive_manifest(path: Path, results: Iterable[ArchiveResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=[
                "course_id",
                "course_name",
                "bundle_dir",
                "status",
                "imscc_status",
                "viewer_status",
                "failed_categories",
                "message",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "course_id": result.course_id,
                    "course_name": result.course_name,
                    "bundle_dir": result.bundle_dir,
                    "status": result.status,
                    "imscc_status": result.imscc_status,
                    "viewer_status": result.viewer_status,
                    "failed_categories": ",".join(result.failed_categories),
                    "message": result.message,
                }
            )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
