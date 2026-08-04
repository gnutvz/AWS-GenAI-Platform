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

        uploaded, refused = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["effective_date", "version"],
            tenant_slug="acme",
        )

        assert uploaded == 0
        assert captured_uploads == [], "a document without a revision reached the index"
        assert refused[0][1] == ["effective_date", "version"]

    def test_document_with_complete_metadata_is_uploaded(self, tmp_path, captured_uploads):
        document(tmp_path, "datasheet.md", {"effective_date": "2026-01-01", "version": "3"})

        uploaded, refused = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["effective_date", "version"],
            tenant_slug="acme",
        )

        assert (uploaded, refused) == (1, [])
        key, attributes = captured_uploads[0]
        assert key == "documents/datasheet.md"
        assert attributes["version"] == "3"

    def test_partial_corpus_still_ingests_the_valid_documents(
        self, tmp_path, captured_uploads
    ):
        """One bad document must not block the rest — but it must be reported."""
        document(tmp_path, "good.md", {"version": "1"})
        document(tmp_path, "bad.md")

        uploaded, refused = ingest.ingest_source(
            "bucket",
            tmp_path,
            prefix="documents/",
            doc_type="technical",
            required_metadata=["version"],
            tenant_slug="acme",
        )

        assert uploaded == 1
        assert [p.name for p, _ in refused] == ["bad.md"]

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
