# Licences

This project is **MIT**. Everything it depends on is permissive — no GPL, AGPL,
SSPL or BUSL anywhere in the tree. Enforced on every push by
`scripts/check_licenses.py`.

```bash
python scripts/check_licenses.py          # fails the build on strong copyleft
python scripts/check_licenses.py --list   # every package and its licence
```

---

## Direct dependencies

| Component | Licence | Commercial use |
|---|---|---|
| **aiplat** *(this repo)* | MIT | ✅ |
| strands-agents | Apache-2.0 | ✅ |
| boto3 / botocore | Apache-2.0 | ✅ |
| PyYAML | MIT | ✅ |
| aws-cdk-lib, constructs | Apache-2.0 | ✅ |
| **Optional extras** | | |
| chainlit *(`ui`)* | Apache-2.0 | ✅ |
| docling *(`ingest`)* | MIT | ✅ |
| litellm *(`gateway`)* | MIT | ✅ |
| bedrock-agentcore *(`agentcore`)* | Apache-2.0 | ✅ |
| pytest, ruff *(`dev`)* | MIT | ✅ |

## Data

| Asset | Licence | Note |
|---|---|---|
| EnterpriseRAG-Bench | MIT | Fully synthetic — fictional company, no real customer data |

## Container images

| Image | Licence | Note |
|---|---|---|
| `langfuse/langfuse:2` | MIT, **except `/ee`** | ⚠️ See below |
| `postgres:16-alpine` | PostgreSQL Licence | Permissive |
| `ghcr.io/berriai/litellm` | MIT | |
| `python:3.11-slim` | PSF + Debian | |

---

## Two things worth knowing

**Langfuse has an enterprise edition.** The repository is MIT *except the `ee`
folders*. Self-hosting the open-source build commercially is free, which is what
the observability stack deploys — but if someone later enables an EE feature, it
needs a commercial licence. Worth a check before offering tracing as part of a
paid engagement.

**Two MPL-2.0 packages** (`bidict`, `certifi`) arrive transitively. MPL is
file-level copyleft: using them unmodified as libraries carries no obligation.
It only matters if someone vendors a copy and patches it — so don't.

---

## When the check fails

`scripts/check_licenses.py` blocks strong copyleft and source-available licences
(AGPL, GPL, SSPL, BUSL, Commons Clause) and flags weak copyleft (LGPL, MPL, EPL,
CDDL) for review. Nobody adds AGPL deliberately — it arrives three levels down in
something routine, and by the time it is in a customer deployment, removing it is
a rewrite.

If it fires: drop the dependency, or get sign-off and add an explicit exception
in the script **with the reasoning written next to it**.

`tests/test_licenses.py` pins the classifier itself, including the trap that
`GPL` matches inside `LGPL` — a naive pattern blocks harmless libraries until
someone loses patience and disables the whole check.

> Not legal advice. This catches the obvious cases early so a human only has to
> look at the interesting ones.
