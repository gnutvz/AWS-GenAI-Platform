"""Fail the build if a dependency arrives under a licence we cannot ship under.

A copyleft package can enter a dependency tree transitively, without anyone
choosing it — nobody adds AGPL on purpose, it shows up three levels down in
something that looked routine. By then it is in a customer deployment and
removing it is a rewrite. Checking on every push is the cheap moment.

    python scripts/check_licenses.py
    python scripts/check_licenses.py --list

Not legal advice, and metadata is sometimes wrong: this catches the obvious
cases early so a human only has to look at the interesting ones.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
from collections import defaultdict

# Strong copyleft: linking these into a distributed or hosted service imposes
# obligations this project cannot meet. AGPL is the one that matters most for a
# hosted platform — it reaches network use, not just distribution.
DENIED = re.compile(
    r"\b(AGPL|GNU Affero|SSPL|Server Side Public|BUSL|Business Source|Commons Clause)\b"
    r"|(?<!L)\bGPL(?!-compatible)\b"
    r"|\bGNU General Public\b",
    re.IGNORECASE,
)

# Weak copyleft: fine to use as an unmodified library, but worth knowing about
# before someone patches one in a vendored copy. Both the abbreviation and the
# spelled-out name — packages use either, and matching only "LGPL" misses half.
NOTABLE = re.compile(
    r"\b(LGPL|GNU Lesser|MPL|Mozilla Public|EPL|Eclipse Public|CDDL)\b", re.IGNORECASE
)

# Packages whose metadata is unhelpful but whose licence is known and verified.
KNOWN = {
    "aws-cdk-lib": "Apache-2.0",
    "constructs": "Apache-2.0",
    "aws-cdk.asset-awscli-v1": "Apache-2.0",
    "aws-cdk.asset-node-proxy-agent-v6": "Apache-2.0",
    "aws-cdk.cloud-assembly-schema": "Apache-2.0",
}


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    name = meta.get("Name") or "?"
    if name in KNOWN:
        return KNOWN[name]

    if expression := meta.get("License-Expression"):
        return expression

    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::") and classifier != "License :: OSI Approved":
            return classifier.split("::")[-1].strip()

    # Some projects paste the whole licence text into this field.
    return (meta.get("License") or "UNKNOWN").split("\n")[0][:60] or "UNKNOWN"


def scan() -> dict[str, list[tuple[str, str]]]:
    findings: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for dist in md.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        licence = licence_of(dist)

        if DENIED.search(licence):
            findings["denied"].append((name, licence))
        elif NOTABLE.search(licence):
            findings["notable"].append((name, licence))
        elif licence == "UNKNOWN":
            findings["unknown"].append((name, licence))
        else:
            findings["ok"].append((name, licence))

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print every package and licence")
    args = parser.parse_args(argv)

    findings = scan()

    if args.list:
        everything = [pair for group in findings.values() for pair in group]
        for name, licence in sorted(everything, key=lambda pair: pair[0].lower()):
            print(f"{name:40} {licence}")
        return 0

    total = sum(len(v) for v in findings.values())
    print(f"Checked {total} packages\n")

    if findings["notable"]:
        print("Weak copyleft — fine as unmodified libraries, do not vendor and patch:")
        for name, licence in sorted(findings["notable"]):
            print(f"  {name:36} {licence}")
        print()

    if findings["unknown"]:
        print("No licence metadata — verify by hand before shipping:")
        for name, _ in sorted(findings["unknown"]):
            print(f"  {name}")
        print()

    if findings["denied"]:
        print("BLOCKED — cannot ship under these:")
        for name, licence in sorted(findings["denied"]):
            print(f"  {name:36} {licence}")
        print(
            "\nRemove the dependency, or get sign-off and add it to an explicit "
            "exception in this script with the reasoning written down."
        )
        return 1

    print("No strong copyleft found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
