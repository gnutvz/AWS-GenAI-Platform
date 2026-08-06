"""The local tracing stack, asserted without Docker.

Every check here corresponds to a way the Langfuse v4 stack was observed to fail
while being written. None of them are hypothetical, and none need a container:
they are all statements about docker-compose.yml that a reader would have to
boot the stack to discover otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

# Both containers refuse to start unless this is 64 hex characters.
HEX_64 = re.compile(r"^[0-9a-f]{64}$")

LANGFUSE_SERVICES = ("langfuse-web", "langfuse-worker")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def env_of(compose: dict, service: str) -> dict:
    return compose["services"][service]["environment"]


def test_encryption_key_survives_yaml_parsing(compose: dict) -> None:
    """A 64-digit key must be quoted, or YAML hands the container a number.

    Upstream's placeholder is sixty-four zeros. Unquoted, YAML reads that as the
    integer 0, the container receives "0", and both web and worker crash on a
    length check — with an error about the key being too short, which reads like
    the key is wrong rather than the quoting.
    """
    for service in LANGFUSE_SERVICES:
        key = env_of(compose, service)["ENCRYPTION_KEY"]
        assert isinstance(key, str), f"{service}: ENCRYPTION_KEY parsed as {type(key).__name__}"
        assert HEX_64.match(key), f"{service}: ENCRYPTION_KEY must be 64 hex characters"


def test_both_langfuse_services_bind_all_interfaces(compose: dict) -> None:
    """Without HOSTNAME both images bind only the container's own IP.

    The service then works through the published port while its healthcheck is
    refused over loopback, so the container sits in `starting` for ever and
    `docker compose up --wait` blocks until it times out.
    """
    for service in LANGFUSE_SERVICES:
        assert env_of(compose, service).get("HOSTNAME") == "0.0.0.0", service


def test_healthchecks_use_an_address_the_image_listens_on(compose: dict) -> None:
    """127.0.0.1, not localhost: there is no IPv6 listener, and localhost is ::1."""
    for service in LANGFUSE_SERVICES:
        probe = " ".join(compose["services"][service]["healthcheck"]["test"])
        assert "127.0.0.1" in probe, f"{service}: {probe}"
        assert "localhost" not in probe, f"{service}: localhost resolves to ::1 first"


def test_worker_has_a_healthcheck(compose: dict) -> None:
    """`up --wait` reports success on a crash-looping worker without one.

    That is not a cosmetic failure: the web container still accepts spans and
    still returns 200, so the stack looks healthy and traces are silently never
    written to ClickHouse.
    """
    assert "healthcheck" in compose["services"]["langfuse-worker"]


def test_every_backing_store_is_declared_and_waited_on(compose: dict) -> None:
    """v4 needs all four. Losing one gets discovered as missing traces, not an error."""
    required = {"postgres", "clickhouse", "redis", "minio"}
    assert required <= set(compose["services"])

    for service in LANGFUSE_SERVICES:
        depends = compose["services"][service]["depends_on"]
        assert required <= set(depends), service
        for name, condition in depends.items():
            assert condition["condition"] == "service_healthy", f"{service} -> {name}"


def test_stores_run_in_utc(compose: dict) -> None:
    """Langfuse requires UTC. A non-UTC store returns empty time ranges, not an error."""
    for service in ("postgres", "clickhouse"):
        assert compose["services"][service]["environment"].get("TZ") == "UTC", service


def test_langfuse_images_are_pinned_to_the_supported_major(compose: dict) -> None:
    """v2 went two majors stale in this repo unnoticed. Pin, so the next drift is visible."""
    for service in LANGFUSE_SERVICES:
        assert compose["services"][service]["image"].endswith(":4"), service


def test_gateway_stays_behind_its_profile(compose: dict) -> None:
    """`make trace-local` must not start the LiteLLM proxy as a side effect."""
    assert compose["services"]["gateway"]["profiles"] == ["gateway"]
    for service in LANGFUSE_SERVICES:
        assert "profiles" not in compose["services"][service], service


def test_only_the_ui_is_reachable_from_the_network(compose: dict) -> None:
    """Published credentials are safe only while the stores stay on loopback."""
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            published = str(mapping).rsplit(":", 1)[0]
            if name in ("langfuse-web", "gateway"):
                continue
            assert published.startswith("127.0.0.1"), f"{name} publishes {mapping} to all hosts"
