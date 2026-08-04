"""The AgentCore entrypoint, tested without the AgentCore SDK installed.

`bedrock-agentcore` is an optional extra, so nothing in CI imports this module —
which is exactly how it came to ship a call to `build_agent(tenant=...)` that
`build_agent` has never accepted. The file would have raised TypeError on its
first real invocation, and the file that calls itself "the production path" is a
bad place to find that out.

So the SDK is stubbed rather than skipped. A test that skips when the extra is
missing would skip in the one place it needs to run.

The invariant being pinned: the tenant comes from the deployment, never from the
caller's payload. `tests/test_tenancy.py` asserts this for the Lambda path by
reading synthesized CloudFormation; that check cannot see this file, because
here it is a property of the Python, not of the infrastructure.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import types

import pytest

from services.agent import agent as agent_module


@pytest.fixture
def agentcore_app(monkeypatch):
    """Import the entrypoint against a stand-in for the AgentCore SDK."""

    class FakeApp:
        def __init__(self) -> None:
            self.entrypoint_fn = None

        def entrypoint(self, fn):
            self.entrypoint_fn = fn
            return fn

        def run(self) -> None:  # pragma: no cover - only __main__ calls this
            raise AssertionError("run() must not be called at import time")

    runtime = types.ModuleType("bedrock_agentcore.runtime")
    runtime.BedrockAgentCoreApp = FakeApp
    package = types.ModuleType("bedrock_agentcore")
    package.runtime = runtime

    monkeypatch.setitem(sys.modules, "bedrock_agentcore", package)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", runtime)
    monkeypatch.delitem(sys.modules, "services.agent.agentcore_app", raising=False)

    module = importlib.import_module("services.agent.agentcore_app")
    yield module

    # Leave nothing importable behind that was built on the stub.
    sys.modules.pop("services.agent.agentcore_app", None)


class Recorder:
    """Stands in for build_agent, remembering how it was called."""

    def __init__(self, chunks=("hello", " world")):
        self.calls: list[dict] = []
        self.chunks = chunks

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self

    async def stream_async(self, prompt: str):
        for chunk in self.chunks:
            yield {"data": chunk}


def drain(async_gen) -> list[str]:
    """Run an async generator to completion. No pytest-asyncio in the dev extra."""

    async def collect() -> list[str]:
        return [item async for item in async_gen]

    return asyncio.run(collect())


class TestTenantIsFixedByTheDeployment:
    def test_payload_tenant_is_ignored(self, agentcore_app, monkeypatch):
        """A caller naming a tenant must not change which tenant is served."""
        recorder = Recorder()
        monkeypatch.setattr(agentcore_app, "build_agent", recorder)

        drain(agentcore_app.invoke({"prompt": "hi", "tenant": "globex"}))

        assert recorder.calls, "build_agent was never called"
        assert "tenant" not in recorder.calls[0], (
            "the AgentCore entrypoint passed a caller-supplied tenant into build_agent — "
            "that is the whole isolation boundary decided by the request body"
        )

    def test_build_agent_takes_no_tenant_argument(self):
        """The other half: the seam this entrypoint must not grow a way around."""
        params = inspect.signature(agent_module.build_agent).parameters
        assert "tenant" not in params, (
            "build_agent grew a tenant parameter. Tenancy is resolved at deploy time "
            "via TENANT; a per-call override would let one caller read another's corpus"
        )


class TestEntrypointContract:
    def test_calls_build_agent_with_the_session_id(self, agentcore_app, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr(agentcore_app, "build_agent", recorder)

        drain(agentcore_app.invoke({"prompt": "hi", "session_id": "abc123"}))

        assert recorder.calls[0]["session_id"] == "abc123"

    def test_streams_every_chunk_in_order(self, agentcore_app, monkeypatch):
        recorder = Recorder(chunks=("The ", "warranty ", "is 24 months."))
        monkeypatch.setattr(agentcore_app, "build_agent", recorder)

        assert drain(agentcore_app.invoke({"prompt": "warranty?"})) == [
            "The ",
            "warranty ",
            "is 24 months.",
        ]

    def test_missing_prompt_is_reported_without_building_an_agent(
        self, agentcore_app, monkeypatch
    ):
        recorder = Recorder()
        monkeypatch.setattr(agentcore_app, "build_agent", recorder)

        assert drain(agentcore_app.invoke({})) == ["Field 'prompt' is required."]
        assert not recorder.calls, "an empty prompt still constructed an agent"

    def test_entrypoint_is_registered_on_the_app(self, agentcore_app):
        """The decorator is what makes this file an entrypoint at all."""
        assert agentcore_app.app.entrypoint_fn is agentcore_app.invoke
