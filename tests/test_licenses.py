"""Licence classification.

The regex is the whole check, and it has one classic way to be wrong: `GPL`
matches inside `LGPL`, so a naive pattern blocks weak-copyleft libraries that are
perfectly fine and erodes trust in the check until someone disables it. These
tests pin both directions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_licenses


class TestBlocked:
    @pytest.mark.parametrize(
        "licence",
        [
            "AGPL-3.0",
            "AGPL-3.0-or-later",
            "GNU Affero General Public License v3",
            "GPL-3.0",
            "GPL-2.0-only",
            "GNU General Public License v2 (GPLv2)",
            "SSPL-1.0",
            "Server Side Public License",
            "BUSL-1.1",
            "Business Source License 1.1",
            "MIT with Commons Clause",
        ],
    )
    def test_strong_copyleft_and_source_available_are_blocked(self, licence):
        assert check_licenses.DENIED.search(licence), f"{licence} should be blocked"


class TestNotBlocked:
    @pytest.mark.parametrize(
        "licence",
        [
            "MIT",
            "Apache-2.0",
            "Apache Software License",
            "BSD-3-Clause",
            "MIT-0",
            "PSF-2.0",
            "Python Software Foundation License",
            "ISC",
        ],
    )
    def test_permissive_licences_pass(self, licence):
        assert not check_licenses.DENIED.search(licence)
        assert not check_licenses.NOTABLE.search(licence)

    @pytest.mark.parametrize("licence", ["LGPL-2.1", "LGPL-3.0-or-later", "GNU Lesser General Public License"])
    def test_lgpl_is_not_treated_as_gpl(self, licence):
        """The bug this check exists to avoid: blocking LGPL because it contains 'GPL'."""
        assert not check_licenses.DENIED.search(licence), f"{licence} wrongly blocked"
        assert check_licenses.NOTABLE.search(licence)

    def test_gpl_compatible_is_not_gpl(self):
        """Several PSF-licensed packages describe themselves this way."""
        assert not check_licenses.DENIED.search("Python License (GPL-compatible)")


class TestWeakCopyleft:
    @pytest.mark.parametrize(
        "licence",
        ["MPL-2.0", "Mozilla Public License 2.0 (MPL 2.0)", "EPL-2.0", "CDDL-1.0"],
    )
    def test_flagged_but_not_blocked(self, licence):
        assert check_licenses.NOTABLE.search(licence)
        assert not check_licenses.DENIED.search(licence)


class TestAgainstTheRealEnvironment:
    def test_this_project_declares_its_licence(self):
        """A wheel with no licence metadata is the failure this check first caught."""
        import importlib.metadata as md

        assert md.distribution("aiplat").metadata.get("License-Expression") == "MIT"

    def test_installed_dependencies_are_shippable(self):
        """Runs against whatever is actually installed, not a fixture."""
        findings = check_licenses.scan()
        assert findings["denied"] == [], (
            f"blocked licence(s) present: {findings['denied']}"
        )

    def test_no_dependency_is_missing_licence_metadata(self):
        findings = check_licenses.scan()
        assert findings["unknown"] == [], (
            f"packages with no licence metadata: {[n for n, _ in findings['unknown']]}"
        )
