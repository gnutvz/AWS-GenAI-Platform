"""Tenant config loading.

A tenant slug ends up in stack names, bucket names and IAM resource ARNs, so a
bad one fails late and confusingly — at deploy time, from CloudFormation, about
a name nobody typed. These tests move that failure to the moment the file is
read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aiplat import tenants

REPO_TENANTS = Path("tenants")


def write(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def minimal(**overrides) -> dict:
    return {"tenant": "acme", "display_name": "Acme Corp", **overrides}


class TestLoading:
    def test_reads_a_minimal_tenant(self, tmp_path):
        tenant = tenants.load(write(tmp_path, "acme", minimal()))
        assert tenant.slug == "acme"
        assert tenant.language == "en"
        assert tenant.sources == []

    def test_reads_sources_and_metadata_requirements(self, tmp_path):
        path = write(
            tmp_path,
            "acme",
            minimal(
                sources=[
                    {
                        "path": "./docs",
                        "doc_type": "technical",
                        "require_metadata": ["effective_date", "version"],
                    }
                ]
            ),
        )
        source = tenants.load(path).sources[0]
        assert source.doc_type == "technical"
        assert source.require_metadata == ["effective_date", "version"]

    def test_ignores_underscore_prefixed_files(self, tmp_path):
        write(tmp_path, "real", minimal(tenant="real"))
        write(tmp_path, "_example", minimal(tenant="example-co"))
        assert [t.slug for t in tenants.load_all(tmp_path)] == ["real"]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert tenants.load_all(tmp_path / "nope") == []


class TestValidation:
    @pytest.mark.parametrize(
        "slug",
        ["Acme", "acme_corp", "1acme", "a", "x" * 25, "acme corp"],
    )
    def test_rejects_slugs_that_break_resource_names(self, tmp_path, slug):
        with pytest.raises(ValueError, match="slug"):
            tenants.load(write(tmp_path, "t", minimal(tenant=slug)))

    def test_rejects_missing_required_fields(self, tmp_path):
        path = tmp_path / "t.yaml"
        path.write_text("tenant: acme\n", encoding="utf-8")
        with pytest.raises(ValueError, match="display_name"):
            tenants.load(path)

    def test_rejects_unknown_fields(self, tmp_path):
        """A typo in a key would otherwise be silent — the setting just never applies."""
        with pytest.raises(ValueError, match="languge"):
            tenants.load(write(tmp_path, "t", minimal(languge="ja")))

    def test_rejects_source_without_path(self, tmp_path):
        with pytest.raises(ValueError, match="path"):
            tenants.load(write(tmp_path, "t", minimal(sources=[{"doc_type": "general"}])))

    def test_rejects_duplicate_slugs_across_files(self, tmp_path):
        """Two files claiming one slug would collide on stack names."""
        write(tmp_path, "a", minimal(tenant="acme"))
        write(tmp_path, "b", minimal(tenant="acme", display_name="Other"))
        with pytest.raises(ValueError, match="Duplicate"):
            tenants.load_all(tmp_path)


class TestEmbedding:
    def test_language_selects_the_embedding_model(self, tmp_path):
        tenant = tenants.load(write(tmp_path, "t", minimal(language="ja")))
        model, dims = tenant.embedding
        assert model.startswith("amazon.titan-embed")
        assert dims == 1024

    def test_unknown_language_falls_back_rather_than_failing(self, tmp_path):
        """A new language should degrade to the multilingual default, not break deploy."""
        assert tenants.load(write(tmp_path, "t", minimal(language="xx"))).embedding == (
            tenants.DEFAULT_EMBEDDING
        )


class TestShippedTenants:
    """The demo tenants in this repo must stay loadable and stay non-customer."""

    def test_repo_tenants_all_parse(self):
        loaded = tenants.load_all(REPO_TENANTS)
        assert {t.slug for t in loaded} == {"acme", "globex"}

    def test_demo_tenants_point_at_the_public_benchmark_corpus(self):
        """This repo is public. Tenant configs must not reference customer data."""
        for tenant in tenants.load_all(REPO_TENANTS):
            for source in tenant.sources:
                assert source.path.startswith("./evals/corpus/"), (
                    f"{tenant.slug} points at {source.path} — demo tenants must use the "
                    f"reproducible public corpus, never customer documents"
                )
