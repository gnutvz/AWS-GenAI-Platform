"""Document ingestion: parse locally, store in S3, let the Knowledge Base index.

Parsing is the one part of RAG worth owning. Bedrock KB will happily ingest a raw
PDF, but its built-in extraction flattens tables and loses heading structure — and
in enterprise documents the tables usually *are* the answer. So docling does the
PDF/DOCX -> Markdown conversion here, and the KB only handles chunking, embedding
and indexing.

Runs as a container or locally, never in Lambda: docling pulls model weights and
blows the deployment package limit.

    pip install 'aiplat[ingest]'
    python -m services.ingest.ingest ./docs --wait
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import boto3

from aiplat.config import settings

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


def upload(bucket: str, key: str, markdown: str, attributes: dict) -> None:
    """Upload the document plus a metadata sidecar.

    The sidecar is what makes filtered retrieval possible later (per department,
    per version). Adding it now costs nothing; retrofitting means a full re-index.
    """
    s3 = boto3.client("s3", region_name=settings().region)
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
    client = boto3.client("bedrock-agent", region_name=cfg.region)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse documents and sync to a Knowledge Base")
    parser.add_argument("source", type=Path, help="File or directory to ingest")
    parser.add_argument("--prefix", default="documents/", help="S3 key prefix")
    parser.add_argument("--department", default="general", help="Stamped as a filterable attribute")
    parser.add_argument("--wait", action="store_true", help="Block until indexing finishes")
    parser.add_argument("--no-sync", action="store_true", help="Upload only, skip the sync job")
    args = parser.parse_args(argv)

    bucket = settings().require("documents_bucket")

    files = [args.source] if args.source.is_file() else sorted(args.source.rglob("*"))
    targets = [f for f in files if f.is_file() and f.suffix.lower() in PARSEABLE]
    if not targets:
        logger.error("No parseable documents found under %s", args.source)
        return 1

    for path in targets:
        logger.info("Parsing %s", path.name)
        markdown = parse_to_markdown(path)
        key = f"{args.prefix}{path.stem}.md"
        upload(
            bucket,
            key,
            markdown,
            {
                "source_filename": path.name,
                "department": args.department,
            },
        )
        logger.info("Uploaded s3://%s/%s (%d chars)", bucket, key, len(markdown))

    if not args.no_sync:
        start_sync(wait=args.wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
