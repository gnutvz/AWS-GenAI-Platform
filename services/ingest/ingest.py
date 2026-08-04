"""Document ingestion: parse locally, store in S3, let the Knowledge Base index.

Parsing is the one part of RAG worth owning. Bedrock KB will happily ingest a raw
PDF, but its built-in extraction flattens tables and loses heading structure — and
in enterprise documents the tables usually *are* the answer. So docling does the
PDF/DOCX -> Markdown conversion here, and the KB only handles chunking, embedding
and indexing.

Runs as a container or locally, never in Lambda: docling pulls model weights and
blows the deployment package limit.

    pip install 'aiplat[ingest]'
    python -m services.ingest.ingest --tenant acme --wait     # sources from YAML
    python -m services.ingest.ingest ./docs --wait            # ad-hoc, no gate
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from functools import lru_cache
from pathlib import Path

import boto3

from aiplat.aws import boto_config
from aiplat.config import settings
from aiplat.tenants import get as get_tenant

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PARSEABLE = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".txt"}
PASSTHROUGH = {".md", ".txt"}


def parse_to_markdown(path: Path) -> str:
    """Convert a document to Markdown, preserving tables and heading structure."""
    if path.suffix.lower() in PASSTHROUGH:
        return path.read_text(encoding="utf-8", errors="replace")

    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        raise SystemExit("docling not installed. Run: pip install 'aiplat[ingest]'")

    result = DocumentConverter().convert(str(path))
    return result.document.export_to_markdown()


def read_document_metadata(path: Path) -> dict:
    """Metadata the corpus author supplied, from `<document>.meta.json` beside it.

    Optional for ordinary documents. Required for anything a tenant declares
    `require_metadata` on — see `refuse_incomplete`.
    """
    sidecar = path.with_name(f"{path.name}.meta.json")
    if not sidecar.exists():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{sidecar} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{sidecar} must contain a JSON object")
    return data


def refuse_incomplete(attributes: dict, required: list[str]) -> list[str]:
    """Return the required keys this document is missing.

    A non-empty result means the document does not get indexed. That is the
    point: you cannot promise "the current revision" when the source carries no
    revision, and an agent answering confidently from a superseded datasheet is
    worse than an agent that has never seen it. Refusing at the door is the only
    place this can be enforced cheaply.
    """
    return [key for key in required if not str(attributes.get(key, "")).strip()]


@lru_cache(maxsize=1)
def _s3():
    # Cached because upload() runs once per document. Building a client per file
    # costs more than the PutObject on a corpus of any size.
    return boto3.client("s3", region_name=settings().region, config=boto_config())


@lru_cache(maxsize=1)
def _bedrock_agent():
    return boto3.client("bedrock-agent", region_name=settings().region, config=boto_config())


def upload(bucket: str, key: str, markdown: str, attributes: dict) -> None:
    """Upload the document plus a metadata sidecar.

    The sidecar is what makes filtered retrieval possible later (per department,
    per version). Adding it now costs nothing; retrofitting means a full re-index.
    """
    s3 = _s3()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=markdown.encode("utf-8"),
        ContentType="text/markdown",
    )
    s3.put_object(
        Bucket=bucket,
        Key=f"{key}.metadata.json",
        Body=json.dumps({"metadataAttributes": attributes}).encode("utf-8"),
        ContentType="application/json",
    )


def start_sync(wait: bool = False) -> str:
    """Trigger a Knowledge Base ingestion job over the S3 data source."""
    cfg = settings()
    client = _bedrock_agent()
    kb_id = cfg.require("knowledge_base_id")

    sources = client.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", [])
    if not sources:
        raise SystemExit(f"Knowledge base {kb_id} has no data source attached")
    ds_id = sources[0]["dataSourceId"]

    job = client.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    job_id = job["ingestionJob"]["ingestionJobId"]
    logger.info("Started ingestion job %s", job_id)

    if wait:
        _wait_for(client, kb_id, ds_id, job_id)
    return job_id


def _wait_for(client, kb_id: str, ds_id: str, job_id: str, poll_seconds: int = 15) -> None:
    while True:
        job = client.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = job["status"]

        if status in ("COMPLETE", "FAILED", "STOPPED"):
            stats = job.get("statistics", {})
            logger.info("Ingestion %s — %s", status, json.dumps(stats))
            if status != "COMPLETE":
                raise SystemExit(f"Ingestion job ended with status {status}")
            return

        logger.info("Ingestion %s ...", status)
        time.sleep(poll_seconds)


def documents_under(root: Path) -> list[Path]:
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    return [
        f
        for f in files
        if f.is_file() and f.suffix.lower() in PARSEABLE and not f.name.endswith(".meta.json")
    ]


def ingest_source(
    bucket: str,
    root: Path,
    *,
    prefix: str,
    doc_type: str,
    required_metadata: list[str],
    tenant_slug: str,
) -> tuple[int, list[tuple[Path, list[str]]]]:
    """Ingest one directory. Returns (uploaded count, refused documents)."""
    uploaded = 0
    refused: list[tuple[Path, list[str]]] = []

    for path in documents_under(root):
        # Author-supplied metadata first, platform facts second — so the platform
        # wins. A sidecar describes the document; it does not get to declare which
        # tenant owns it or what the file is called.
        attributes = {
            **read_document_metadata(path),
            "source_filename": path.name,
            "doc_type": doc_type,
            "tenant": tenant_slug,
        }

        missing = refuse_incomplete(attributes, required_metadata)
        if missing:
            refused.append((path, missing))
            logger.warning("REFUSED %s — missing %s", path.name, ", ".join(missing))
            continue

        logger.info("Parsing %s", path.name)
        markdown = parse_to_markdown(path)
        key = f"{prefix}{path.stem}.md"
        upload(bucket, key, markdown, attributes)
        uploaded += 1
        logger.info("Uploaded s3://%s/%s (%d chars)", bucket, key, len(markdown))

    return uploaded, refused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse documents and sync to a Knowledge Base")
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        help="File or directory to ingest. Omit when using --tenant.",
    )
    parser.add_argument(
        "--tenant",
        help="Ingest every source in tenants/<slug>.yaml, honouring require_metadata",
    )
    parser.add_argument("--prefix", default="documents/", help="S3 key prefix")
    parser.add_argument("--doc-type", default="general", help="Stamped as a filterable attribute")
    parser.add_argument("--wait", action="store_true", help="Block until indexing finishes")
    parser.add_argument("--no-sync", action="store_true", help="Upload only, skip the sync job")
    args = parser.parse_args(argv)

    if not args.source and not args.tenant:
        parser.error("give a source directory, or --tenant to use its configured sources")

    bucket = settings().require("documents_bucket")

    if args.tenant:
        tenant = get_tenant(args.tenant)
        if not tenant.sources:
            raise SystemExit(f"Tenant {tenant.slug!r} declares no sources in its YAML")
        plan = [
            (Path(s.path), s.doc_type, s.require_metadata, f"{args.prefix}{s.doc_type}/")
            for s in tenant.sources
        ]
        tenant_slug = tenant.slug
    else:
        # Ad-hoc mode: no tenant config, so nothing is required and nothing is refused.
        plan = [(args.source, args.doc_type, [], args.prefix)]
        tenant_slug = settings().tenant

    total_uploaded = 0
    all_refused: list[tuple[Path, list[str]]] = []

    for root, doc_type, required, prefix in plan:
        if not root.exists():
            raise SystemExit(f"Source path does not exist: {root}")
        uploaded, refused = ingest_source(
            bucket,
            root,
            prefix=prefix,
            doc_type=doc_type,
            required_metadata=required,
            tenant_slug=tenant_slug,
        )
        total_uploaded += uploaded
        all_refused.extend(refused)

    if all_refused:
        # Loud on purpose. A silently shrinking corpus is how a RAG system starts
        # answering "I don't know" to questions it used to handle.
        logger.warning(
            "%d document(s) refused for missing metadata — they are NOT searchable:",
            len(all_refused),
        )
        for path, missing in all_refused:
            logger.warning("  %s — add %s to %s.meta.json", path.name, ", ".join(missing), path.name)

    if not total_uploaded:
        logger.error("Nothing was uploaded.")
        return 1

    logger.info("Uploaded %d document(s), refused %d", total_uploaded, len(all_refused))

    if not args.no_sync:
        start_sync(wait=args.wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
