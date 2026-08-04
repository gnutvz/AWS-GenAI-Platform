"""Versioned prompts.

The prompt is the most frequently changed and least reviewed part of an agent.
As a string literal it had no version, which made a prompt change look like any
other commit: an eval score had nothing to attribute it to, a regression could
not be rolled back without rolling back the deploy that carried it, and a trace
recorded what the model said but not what it was told.

Two things are worth testing beyond the loader itself. That a deployment can pin
a version, since defaulting to "highest on disk" means adding a file and
shipping it are the same act. And that the prompt directory is found relative to
the module rather than the working directory — the failure that would appear
only in Lambda, where nothing runs from the repo root.
"""

from __future__ import annotations

import pytest

from aiplat import config, prompts
from services.agent.agent import SYSTEM_PROMPTS


@pytest.fixture
def registry(tmp_path):
    """A prompt directory with three versions, out of order on disk."""
    directory = tmp_path / "system"
    directory.mkdir()
    (directory / "v1.md").write_text("First.\n", encoding="utf-8")
    (directory / "v3.md").write_text("Third.\n", encoding="utf-8")
    (directory / "v2.md").write_text("Second.\n", encoding="utf-8")
    return directory


class TestDiscovery:
    def test_versions_are_sorted_numerically(self, registry):
        """Not lexically: v10 comes after v9, which string sorting gets wrong."""
        (registry / "v10.md").write_text("Tenth.", encoding="utf-8")
        assert prompts.versions(registry) == [1, 2, 3, 10]

    def test_unrelated_files_are_ignored(self, registry):
        (registry / "README.md").write_text("notes", encoding="utf-8")
        (registry / "draft.md").write_text("wip", encoding="utf-8")
        (registry / "v2.md.bak").write_text("old", encoding="utf-8")
        assert prompts.versions(registry) == [1, 2, 3]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert prompts.versions(tmp_path / "nope") == []


class TestLoading:
    def test_defaults_to_the_highest_version(self, registry):
        assert prompts.load(registry).version == 3

    def test_loads_a_specific_version(self, registry):
        assert prompts.load(registry, 1).text == "First."

    def test_name_comes_from_the_directory(self, registry):
        assert prompts.load(registry).name == "system"

    def test_label_identifies_prompt_and_version(self, registry):
        assert prompts.load(registry, 2).label == "system@v2"

    def test_text_is_stripped(self, registry):
        """Trailing newlines in a system prompt are noise in every trace."""
        assert prompts.load(registry, 1).text == "First."

    def test_unknown_version_names_what_is_available(self, registry):
        with pytest.raises(RuntimeError) as exc:
            prompts.load(registry, 9)
        assert "v1, v2, v3" in str(exc.value)

    def test_a_directory_with_no_versions_fails_loudly(self, tmp_path):
        empty = tmp_path / "system"
        empty.mkdir()
        with pytest.raises(RuntimeError, match="No prompt versions"):
            prompts.load(empty)


class TestPinning:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.delenv("PROMPT_VERSION", raising=False)
        config.settings.cache_clear()
        yield
        config.settings.cache_clear()

    def test_unset_means_unpinned(self):
        assert config.settings().prompt_version is None

    @pytest.mark.parametrize("raw", ["2", "v2", "V2"])
    def test_accepts_both_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("PROMPT_VERSION", raw)
        config.settings.cache_clear()
        assert config.settings().prompt_version == 2

    def test_nonsense_fails_rather_than_falling_back(self, monkeypatch):
        """Falling back would run unpinned in a deployment whose point was to pin."""
        monkeypatch.setenv("PROMPT_VERSION", "latest")
        config.settings.cache_clear()
        with pytest.raises(RuntimeError, match="PROMPT_VERSION"):
            config.settings()


class TestTheAgentsOwnPrompt:
    def test_it_ships_with_at_least_one_version(self):
        assert prompts.versions(SYSTEM_PROMPTS), (
            "the agent has no prompt on disk; the Lambda bundle copies services/, so "
            "a missing directory here is a missing prompt in production"
        )

    def test_it_is_found_regardless_of_working_directory(self, monkeypatch, tmp_path):
        """Lambda does not run from the repo root, and neither does a container."""
        monkeypatch.chdir(tmp_path)
        assert prompts.load(SYSTEM_PROMPTS).text

    def test_the_prompt_still_forbids_answering_from_memory(self):
        """Moving it to a file must not have quietly dropped the rules it carries."""
        text = prompts.load(SYSTEM_PROMPTS, 1).text.lower()
        assert "not from memory" in text
        assert "[n]" in text
