import argparse
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canvas_archive_viewer as archive
from canvas_course_backup import load_dotenv, sanitize_filename


class ArchiveViewerTests(unittest.TestCase):
    def test_dotenv_does_not_override_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "CANVAS_BASE_URL=https://from-file.example\n"
                "EXAMPLE_QUOTED=\"hello world\"\n"
                "EXAMPLE_COMMENT=value # comment\n",
                encoding="utf-8",
            )
            old_base = archive.os.environ.get("CANVAS_BASE_URL")
            try:
                archive.os.environ["CANVAS_BASE_URL"] = "https://already-set.example"
                load_dotenv(env_path)
                self.assertEqual(
                    archive.os.environ["CANVAS_BASE_URL"], "https://already-set.example"
                )
                self.assertEqual(archive.os.environ["EXAMPLE_QUOTED"], "hello world")
                self.assertEqual(archive.os.environ["EXAMPLE_COMMENT"], "value")
            finally:
                if old_base is None:
                    archive.os.environ.pop("CANVAS_BASE_URL", None)
                else:
                    archive.os.environ["CANVAS_BASE_URL"] = old_base
                archive.os.environ.pop("EXAMPLE_QUOTED", None)
                archive.os.environ.pop("EXAMPLE_COMMENT", None)

    def test_sanitize_filename_removes_unsafe_characters(self) -> None:
        self.assertEqual(sanitize_filename('Course: "A/B"?'), "Course_ _A_B_")

    def test_parse_imscc_manifest_extracts_resources_and_outline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "imsmanifest.xml"
            manifest.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1">
  <organizations>
    <organization identifier="org1">
      <item identifier="item1" identifierref="res1"><title>Welcome</title></item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="res1" type="webcontent" href="pages/welcome.html">
      <file href="pages/welcome.html" />
    </resource>
  </resources>
</manifest>
""",
                encoding="utf-8",
            )
            parsed = archive.parse_imscc_manifest(manifest)
            self.assertEqual(parsed["resources"][0]["href"], "pages/welcome.html")
            self.assertEqual(parsed["organizations"][0]["title"], "Welcome")

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "bad.imscc"
            with zipfile.ZipFile(zip_path, "w") as imscc:
                imscc.writestr("../evil.txt", "bad")
            with self.assertRaises(archive.CanvasError):
                archive.safe_extract_zip(zip_path, Path(tmp) / "out")

    def test_rewrite_canvas_links_uses_downloaded_file_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = archive.ArchiveContext(
                client=None,
                course={"id": 1, "name": "Demo"},
                args=argparse.Namespace(),
                root=root,
                data_dir=root / "data",
                files_dir=root / "files",
                viewer_dir=root / "viewer",
                local_links_by_file_id={"42": "files/course_files/42_handout.pdf"},
            )
            rewritten = archive.safe_canvas_html(
                ctx,
                '<p onclick="bad()"><a href="https://canvas.example/files/42/download">Open</a></p>',
            )
            self.assertNotIn("onclick", rewritten)
            self.assertIn("../files/course_files/42_handout.pdf", rewritten)

    def test_submission_attachments_include_history_without_duplicates(self) -> None:
        submission = {
            "attachments": [{"id": 1, "filename": "first.pdf"}],
            "submission_history": [
                {"attachments": [{"id": 1, "filename": "first.pdf"}]},
                {"attachments": [{"id": 2, "filename": "second.pdf"}]},
            ],
        }
        attachments = archive.submission_attachments(submission)
        self.assertEqual([attachment["id"] for attachment in attachments], [1, 2])

    def test_write_submissions_checkpoint_records_completed_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "submissions.json"
            archive.write_submissions_checkpoint(
                path,
                submissions=[{"assignment_id": 10, "user_id": 1}],
                errors=[{"assignment_id": "11", "message": "Timeout"}],
                completed_assignment_ids={10},
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["completed_assignment_ids"], [10])
            self.assertEqual(data["errors"][0]["assignment_id"], "11")

    def test_fixture_viewer_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture"
            data = fixture / "data"
            data.mkdir(parents=True)
            write_json(data / "course.json", {"id": 10, "name": "Demo Course", "course_code": "DEMO"})
            write_json(data / "modules.json", {"modules": []})
            write_json(data / "enrollments.json", {"enrollments": []})
            write_json(data / "assignments.json", {"assignments": [], "assignment_groups": []})
            write_json(data / "submissions.json", {"submissions": [], "errors": []})
            write_json(data / "discussions.json", {"topics": []})
            write_json(data / "files.json", {"files": []})
            write_json(data / "imscc.json", {"resources": [], "organizations": []})
            write_json(data / "issues.json", [])

            args = argparse.Namespace(fixture_dir=fixture, archive_output_dir=Path(tmp) / "out")
            self.assertEqual(archive.build_viewer_from_fixture(args), 0)
            index = args.archive_output_dir / "fixture-viewer" / "viewer" / "index.html"
            self.assertTrue(index.exists())
            self.assertIn("Demo Course", index.read_text(encoding="utf-8"))

    def test_validate_archive_reports_counts_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            data = root / "data"
            viewer = root / "viewer"
            files = root / "files"
            data.mkdir(parents=True)
            viewer.mkdir()
            files.mkdir()
            for name, payload in {
                "course": {"id": 1},
                "enrollments": {"enrollments": [{"id": 1}]},
                "assignments": {"assignments": [{"id": 2}]},
                "submissions": {"submissions": [{"id": 3}]},
                "modules": {"modules": []},
                "files": {"files": [{"id": 4}]},
                "issues": [],
                "discussions": {"topics": []},
                "imscc": {"resources": []},
            }.items():
                write_json(data / f"{name}.json", payload)
            (files / "handout.pdf").write_text("ok", encoding="utf-8")
            for name in ["index", "gradebook", "students", "files"]:
                (viewer / f"{name}.html").write_text(
                    '<a href="../files/handout.pdf">handout</a>', encoding="utf-8"
                )

            report = archive.validate_archive(root)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["data_counts"]["submissions"], 1)
            self.assertEqual(report["missing_links"], [])


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
