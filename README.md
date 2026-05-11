# Canvas Course Backup

Backup Canvas LMS courses as IMS Common Cartridge (`.imscc`) exports.

The script can either:

- Discover current courses from an institution/account-level Canvas admin token.
- Back up a specific list of Canvas course IDs.

Downloaded files are named with the course name, course code, teacher names, and Canvas course ID. A CSV manifest is written alongside the exports.

This repo also includes `canvas_archive_viewer.py`, a companion tool that creates a read-only emergency archive with course content, roster/grade snapshots, submissions, comments, attachments, discussions, files, and a static HTML viewer.

## Requirements

- Python 3.10 or newer
- A Canvas API access token with permission to read the target courses and create content exports
- No third-party Python packages

## Setup

Copy the sample environment file and fill in your Canvas instance details:

```bash
cp .env.example .env
```

The script automatically loads `.env` from the current directory when it runs. Values already set in your shell take precedence over `.env`.

You can also pass credentials directly with `--base-url` and `--token`, but `.env` or shell environment variables are safer because tokens are less likely to end up in shell history.

## Usage

Back up current courses discovered from the admin account:

```bash
./canvas_course_backup.py --output-dir ./canvas-backups
```

Back up current courses with three concurrent exports/downloads:

```bash
./canvas_course_backup.py --output-dir ./canvas-backups --workers 3
```

Back up specific courses:

```bash
./canvas_course_backup.py --course-ids 12345,23456,34567 --output-dir ./canvas-backups
```

Back up courses listed in a file:

```bash
./canvas_course_backup.py --course-file courses.txt --output-dir ./canvas-backups
```

Use a specific account or enrollment term while discovering courses:

```bash
./canvas_course_backup.py --account-id self --enrollment-term-id 42 --output-dir ./canvas-backups
```

Preview matching courses and filenames without creating Canvas exports:

```bash
./canvas_course_backup.py --dry-run --output-dir ./canvas-backups
```

## Emergency Archive Viewer

Create a portable archive bundle and static viewer for current courses:

```bash
./canvas_archive_viewer.py --archive-output-dir ./canvas-archives --workers 2
```

Archive specific courses:

```bash
./canvas_archive_viewer.py --course-ids 12345,23456 --archive-output-dir ./canvas-archives
```

Each course bundle contains:

```text
course.imscc
data/
files/
viewer/index.html
```

The `data/` directory stores JSON snapshots for course metadata, modules, enrollments/grades, assignments, submissions, discussions, course files, IMSCC manifest content, and archive issues. The `files/` directory stores downloaded course files, submission attachments, discussion attachments, and extracted IMSCC content. The `viewer/` directory is a static admin-only HTML interface.

Useful archive options:

```text
--archive-output-dir PATH              Output directory, default: canvas-archives
--include-discussions / --no-include-discussions
--include-course-files / --no-include-course-files
--include-submission-attachments / --no-include-submission-attachments
--skip-imscc                           Metadata-only rerun
--generate-viewer / --no-generate-viewer
--fixture-dir PATH                     Build viewer from existing data/*.json without Canvas
--validate-archive PATH                Validate an existing archive bundle
```

Quizzes are intentionally not archived in v1 beyond whatever assignment/submission grade metadata Canvas exposes through the normal assignment and submission APIs.

Validate a completed archive bundle:

```bash
./canvas_archive_viewer.py --validate-archive "canvas-archives/Course Name - course_12345"
```

## What Counts as "Current"

When no course IDs are provided, the script discovers courses with these default filters:

- `state=available`
- `completed=false`
- courses or terms active on today's date
- courses with enrollments

It does not require courses to be published unless you pass:

```bash
./canvas_course_backup.py --published-only --output-dir ./canvas-backups
```

To remove the date filter:

```bash
./canvas_course_backup.py --no-date-filter --output-dir ./canvas-backups
```

To include empty courses:

```bash
./canvas_course_backup.py --include-empty --output-dir ./canvas-backups
```

## Concurrency

Use `--workers` to export and download multiple courses at once:

```bash
./canvas_course_backup.py --workers 4 --output-dir ./canvas-backups
```

The default is `2`. For many institutions, `3` or `4` is a reasonable starting point. Higher values may trigger Canvas throttling or put unnecessary load on the Canvas export queue.

Temporary API errors are retried automatically for:

- `429`
- `500`
- `502`
- `503`
- `504`

If a course still fails after retries, the terminal output and manifest will show `failed`.

## Output

Each successful backup creates an `.imscc` file such as:

```text
Biology 101 [BIO-101] - Jane Smith, Alex Lee - course_12345.imscc
```

The script also writes:

```text
canvas_backup_manifest.csv
```

The manifest includes:

- course ID
- course name
- teacher names
- filename
- status
- Canvas export ID
- message

## Useful Options

```text
--base-url URL              Canvas base URL
--token TOKEN               Canvas API token
--account-id ID             Account used for auto-discovery, default: self
--course-id ID              Course ID, repeatable
--course-ids IDS            Comma- or whitespace-separated course IDs
--course-file PATH          Text or CSV file of course IDs
--output-dir PATH           Output directory
--enrollment-term-id ID     Enrollment term filter, repeatable
--state STATE               Course workflow state, repeatable
--no-date-filter            Do not require courses/terms to be active today
--include-empty             Include courses with no enrollments
--published-only            Only discover published courses
--overwrite                 Replace existing .imscc files
--poll-interval SECONDS     Export status polling interval
--timeout-minutes MINUTES   Per-course export timeout
--workers N                 Concurrent course exports/downloads
--dry-run                   Preview without creating exports
```

## Security Notes

- Do not commit `.env`, API tokens, `.imscc` exports, or backup manifests.
- Do not commit `canvas-archives/`; it can contain student grades, submissions, comments, and downloaded files.
- Use the least-privileged Canvas token that can read the target courses and create exports.
- Treat exported `.imscc` files as sensitive course content.
- If a token is ever committed or shared, revoke it in Canvas and create a new one.

## Publishing Checklist

Before publishing to GitHub:

- Confirm `.env` is not present in `git status`.
- Confirm no `.imscc` files or backup directories are staged.
- Choose a license and add a `LICENSE` file if this will be public.
- Run:

```bash
python3 -m py_compile canvas_course_backup.py
python3 -m py_compile canvas_archive_viewer.py
python3 -m unittest
./canvas_course_backup.py --help
./canvas_archive_viewer.py --help
```
