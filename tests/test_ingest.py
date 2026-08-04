"""Ingest gating.

The metadata gate is the only place a document without a known revision can be
stopped. Once it is indexed, the agent will answer from it confidently and cite
it — and a citation to a superseded datasheet reads exactly like a citation to a
current one. So the refusal path gets tests, not just the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiplat import config
from services.ingest import ingest


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


@pytest.fixture
def captured_uploads(monkeypatch):
    """Records what would have gone to S3, without going to S3."""
    uploads: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ingest, "upload", lambda bucket, key, markdown, attributes: uploads.append((key, attributes))
    )
    return uploads


def document(tmp_path: Path, name: str, metadata: dict | None = None) -> Path:
    path = tmp_path / name
    path.write_text("# Title\n\nSome content.\n", encoding="utf-8")
    if metadata is not None:
        path.with_name(f"{name}.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    return path


class TestRequiredMetadata:
    def test_reports_every_missing_key(self):
        assert ingest.refuse_incomplete({}, ["version", "effective_date"]) == [
            "version",
            "effective_date",
        ]

    def test_blank_counts_as_missing(self):
        """An empty string is how a half-filled template arrives."""
        assert ingest.refuse_incomplete({"version": "   "}, ["version"]) == ["version"]

    def test_complete_metadata_passes(self):
        attributes = {"version": "3", "effective_date": "2026-01-01"}
        assert ingest.refuse_incomplete(attributes, list(attributes)) == []

    def test_nothing_required_refuses_nothing(self):
        assert ingest.refuse_incomplete({}, []) == []


class TestSidecar:
    def test_reads_metadata_next_to_the_document(self, tmp_path):
        path = document(tmp_path, "spec.md", {"version": "2", "effective_date": "2026-03-01"})
        assert ingest.read_document_metadata(path)["version"] == "2"

    def test_absent_sidecar_is_not_an_error(self, tmp_path):
        assert ingest.read_document_metadata(document(tmp_path, "notes.md")) == {}

    def test_malformed_sidecar_fails_loudly(self, tmp_path):
        """Silently ignoring it would index a document the author meant to gate."""
        path = document(tmp_path, "spec.md")
        path.with_name("spec.md.meta.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit, match="not valid JSON"):
            ingest.read_document_metadata(path)


class TestDocumentDiscovery:
    def test_sidecars_are_not_ingested_as_documents(self, tmp_path):
        document(tmp_path, "spec.md", {"version": "1"})
        assert [p.name for p in ingest.documents_under(tmp_path)] == ["spec.md"]

    def test_unsupported_extensions_are_skipped(self, tmp_path):
        document(tmp_path, "keep.md")
        (tmp_path / "skip.zip").write_bytes(b"x")
        assert [p.name for p in ingest.documents_under(tmp_path)] == ["keep.md"]


class TestIngestSource:
    def test_document_missing_required_metadata_is_not_uploaded(
        self, tmp_path, captured_uploads
    ):
        document(tmp_path, "datasheet.md")  # no sidecar at all

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["effective_date", "version"],
            tenant_slug="acme",
        )

        assert report.uploaded == 0
        assert captured_uploads == [], "a document without a revision reached the index"
        assert report.refused[0][1] == ["effective_date", "version"]

    def test_document_with_complete_metadata_is_uploaded(self, tmp_path, captured_uploads):
        document(tmp_path, "datasheet.md", {"effective_date": "2026-01-01", "version": "3"})

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["effective_date", "version"],
            tenant_slug="acme",
        )

        assert (report.uploaded, report.refused, report.failed) == (1, [], [])
        key, attributes = captured_uploads[0]
        assert key == "documents/datasheet.md"
        assert attributes["version"] == "3"

    def test_partial_corpus_still_ingests_the_valid_documents(
        self, tmp_path, captured_uploads
    ):
        """One bad document must not block the rest — but it must be reported."""
        document(tmp_path, "good.md", {"version": "1"})
        document(tmp_path, "bad.md")

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["version"],
            tenant_slug="acme",
        )

        assert report.uploaded == 1
        assert [p.name for p, _ in report.refused] == ["bad.md"]

    def test_every_document_is_stamped_with_its_tenant(self, tmp_path, captured_uploads):
        document(tmp_path, "notes.md")
        ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="general",
            required_metadata=[],
            tenant_slug="globex",
        )
        assert captured_uploads[0][1]["tenant"] == "globex"

    def test_sidecar_cannot_override_the_tenant_stamp(self, tmp_path, captured_uploads):
        """Corpus authors supply facts about the document, not about who owns it."""
        document(tmp_path, "notes.md", {"tenant": "someone-else"})
        ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="general",
            required_metadata=[],
            tenant_slug="globex",
        )
        assert captured_uploads[0][1]["tenant"] == "globex"


class TestKeysStayUnique:
    """The directory a document sits in is part of what identifies it.

    Discovery recurses, so a real corpus has `2023/report.pdf` beside
    `2024/report.pdf`. Keying on the stem alone made the second overwrite the
    first — an upload that logged success and lost a document.
    """

    def test_same_name_in_different_folders_gets_different_keys(
        self, tmp_path, captured_uploads
    ):
        (tmp_path / "2023").mkdir()
        (tmp_path / "2024").mkdir()
        document(tmp_path / "2023", "report.md")
        document(tmp_path / "2024", "report.md")

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="general",
            required_metadata=[],
            tenant_slug="acme",
        )

        keys = sorted(k for k, _ in captured_uploads)
        assert report.uploaded == 2
        assert keys == ["documents/2023/report.md", "documents/2024/report.md"]
        assert len(set(keys)) == 2, "one document silently overwrote the other"

    def test_same_stem_different_format_gets_different_keys(self):
        """`report.pdf` and `report.docx` are two documents, not one."""
        root = Path("/corpus")
        assert ingest.s3_key("documents/", root, root / "report.pdf") != ingest.s3_key(
            "documents/", root, root / "report.docx"
        )

    def test_markdown_does_not_get_a_second_suffix(self):
        root = Path("/corpus")
        assert ingest.s3_key("documents/", root, root / "a" / "intro.md") == (
            "documents/a/intro.md"
        )

    def test_non_markdown_keeps_its_original_extension(self):
        root = Path("/corpus")
        assert ingest.s3_key("documents/", root, root / "spec.pdf") == "documents/spec.pdf.md"

    def test_a_single_file_source_has_no_directory_to_preserve(self, tmp_path):
        """`ingest.py ./one.pdf` — root is the file itself, so relative_to() cannot apply."""
        target = tmp_path / "one.pdf"
        target.write_text("x", encoding="utf-8")
        assert ingest.s3_key("documents/", target, target) == "documents/one.pdf.md"


class TestOneBadFileDoesNotEndTheRun:
    """Corpora arrive with a corrupt PDF or a password-protected spreadsheet.

    Aborting on the first one leaves a half-uploaded corpus that nothing can
    resume: the operator re-runs from the start and hits the same file again.
    """

    def test_a_failing_document_is_recorded_and_skipped(self, tmp_path, captured_uploads, monkeypatch):
        document(tmp_path, "a-good.md")
        document(tmp_path, "b-broken.md")
        document(tmp_path, "c-good.md")

        def parse(path):
            if "broken" in path.name:
                raise ValueError("stream error: not a PDF")
            return "content"

        monkeypatch.setattr(ingest, "parse_to_markdown", parse)

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="general",
            required_metadata=[],
            tenant_slug="acme",
        )

        assert report.uploaded == 2, "a later document was skipped after an earlier failure"
        assert [p.name for p, _ in report.failed] == ["b-broken.md"]
        assert "ValueError" in report.failed[0][1]

    def test_failures_are_kept_apart_from_refusals(self, tmp_path, captured_uploads, monkeypatch):
        """A refusal is the gate working; a failure is something to go and fix."""
        document(tmp_path, "no-metadata.md")
        document(tmp_path, "broken.md", {"version": "1"})
        monkeypatch.setattr(ingest, "parse_to_markdown", _raise_on("broken"))

        report = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="general",
            required_metadata=["version"],
            tenant_slug="acme",
        )

        assert [p.name for p, _ in report.refused] == ["no-metadata.md"]
        assert [p.name for p, _ in report.failed] == ["broken.md"]

    def test_a_missing_parser_still_stops_the_run(self, tmp_path, monkeypatch):
        """SystemExit means docling is absent — identical for every file, so
        carrying on would print it once per document and upload nothing."""
        document(tmp_path, "a.md")

        def parse(path):
            raise SystemExit("docling not installed")

        monkeypatch.setattr(ingest, "parse_to_markdown", parse)

        with pytest.raises(SystemExit):
            ingest.ingest_source(
                "bucket",
                tmp_path,
                prefix="documents/",
                doc_type="general",
                required_metadata=[],
                tenant_slug="acme",
            )


def _raise_on(marker: str):
    def parse(path):
        if marker in path.name:
            raise ValueError("boom")
        return "content"

    return parse
