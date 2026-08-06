"""The slice of behaviour a tenant owns.

Shared behaviour is what separates a platform from several forks wearing a
config file, and that argument held while there was one use case. It stopped
holding when more than one department plugged in: a legal team and an
engineering team asking questions of their own corpora do not want the same
instructions, and telling them the platform's prompt is the platform's is
telling them to fork it.

So a tenant now chooses its prompt and its model. It still does not choose
chunking, the guardrail or retrieval — those are the parts an auditor asks about.

The mechanism is not new, which is the point: one Lambda per tenant already
existed, so this is a value flowing through a path rather than a config service
resolved per request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from aiplat.tenants import AgentConfig, Tenant, load

# Appended, not prepended: infra/app.py would otherwise shadow the app/ package.
sys.path.append(str(Path(__file__).resolve().parents[1] / "infra"))
# scripts/ holds operator tooling, not an importable package.
sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
from stacks.api_stack import ApiStack
from stacks.knowledge_stack import KnowledgeStack


def write(tmp_path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestDefaults:
    def test_a_tenant_without_an_agent_block_is_unchanged(self, tmp_path):
        """Existing tenant files must keep behaving exactly as before."""
        tenant = load(write(tmp_path, "acme", "tenant: acme\ndisplay_name: Acme\n"))

        assert tenant.agent == AgentConfig()
        assert tenant.agent.prompt == "system"
        assert tenant.agent.prompt_version is None
        assert tenant.agent.model is None


class TestParsing:
    def test_prompt_and_model_are_read(self, tmp_path):
        tenant = load(
            write(
                tmp_path,
                "legal",
                "tenant: legal\ndisplay_name: Legal\n"
                "agent:\n  prompt: legal\n  model: global.anthropic.claude-opus-4-8\n",
            )
        )

        assert tenant.agent.prompt == "legal"
        assert tenant.agent.model == "global.anthropic.claude-opus-4-8"

    @pytest.mark.parametrize("written", ["2", "v2", 2])
    def test_prompt_version_accepts_the_spellings_a_human_writes(self, tmp_path, written):
        tenant = load(
            write(
                tmp_path,
                "acme",
                f"tenant: acme\ndisplay_name: Acme\nagent:\n  prompt_version: {written}\n",
            )
        )

        assert tenant.agent.prompt_version == 2

    def test_a_nonsense_version_fails_loudly(self, tmp_path):
        with pytest.raises(ValueError, match="prompt_version"):
            load(
                write(
                    tmp_path,
                    "acme",
                    "tenant: acme\ndisplay_name: Acme\nagent:\n  prompt_version: latest\n",
                )
            )

    def test_a_typo_in_a_field_name_is_refused(self, tmp_path):
        """Silently ignoring it means the setting never applies — the failure the
        rest of this loader already refuses to have."""
        with pytest.raises(ValueError, match="unknown agent field"):
            load(
                write(
                    tmp_path,
                    "acme",
                    "tenant: acme\ndisplay_name: Acme\nagent:\n  promt: system\n",
                )
            )


class TestItReachesTheDeployment:
    """Parsing it is worthless if it stops before the Lambda."""

    @staticmethod
    def synth(tenant: Tenant) -> Template:
        app = cdk.App(context={"aws:cdk:bundling-stacks": []})
        env = cdk.Environment(account="111111111111", region="us-west-2")
        knowledge = KnowledgeStack(app, f"K-{tenant.slug}", env=env, tenant=tenant)
        api = ApiStack(
            app,
            f"A-{tenant.slug}",
            env=env,
            tenant=tenant,
            knowledge_base_id=knowledge.knowledge_base_id,
            documents_bucket_name=knowledge.documents_bucket.bucket_name,
            guardrail_id="gr-test",
            guardrail_version="1",
            model_id="deployment-default-model",
        )
        return Template.from_stack(api)

    @staticmethod
    def agent_env(template: Template) -> dict:
        functions = template.find_resources("AWS::Lambda::Function")
        for function in functions.values():
            variables = function["Properties"].get("Environment", {}).get("Variables", {})
            if variables.get("TENANT"):
                return variables
        raise AssertionError("no agent function in the template")

    def test_the_tenants_model_wins_over_the_deployment_default(self):
        tenant = Tenant(
            slug="legal",
            display_name="Legal",
            agent=AgentConfig(model="global.anthropic.claude-opus-4-8"),
        )

        assert self.agent_env(self.synth(tenant))["MODEL_ID"] == (
            "global.anthropic.claude-opus-4-8"
        )

    def test_a_tenant_without_a_model_takes_the_deployment_default(self):
        tenant = Tenant(slug="acme", display_name="Acme")

        assert self.agent_env(self.synth(tenant))["MODEL_ID"] == "deployment-default-model"

    def test_prompt_selection_reaches_the_function(self):
        tenant = Tenant(
            slug="legal",
            display_name="Legal",
            agent=AgentConfig(prompt="legal", prompt_version=3),
        )

        variables = self.agent_env(self.synth(tenant))

        assert variables["PROMPT_NAME"] == "legal"
        assert variables["PROMPT_VERSION"] == "3"

    def test_an_unpinned_version_is_empty_not_the_string_none(self):
        """"None" would be read back as a version number and fail at startup."""
        tenant = Tenant(slug="acme", display_name="Acme")

        assert self.agent_env(self.synth(tenant))["PROMPT_VERSION"] == ""

    def test_two_tenants_get_different_agents(self):
        """The whole point: one account, two departments, two behaviours."""
        legal = self.agent_env(
            self.synth(
                Tenant(
                    slug="legal",
                    display_name="Legal",
                    agent=AgentConfig(prompt="legal", model="model-a"),
                )
            )
        )
        eng = self.agent_env(
            self.synth(
                Tenant(
                    slug="eng",
                    display_name="Eng",
                    agent=AgentConfig(prompt="eng", model="model-b"),
                )
            )
        )

        assert (legal["PROMPT_NAME"], legal["MODEL_ID"]) == ("legal", "model-a")
        assert (eng["PROMPT_NAME"], eng["MODEL_ID"]) == ("eng", "model-b")


class TestLocalMatchesDeployed:
    """`.env` has to carry the same agent config the Lambda got.

    Otherwise `make ask` and the eval suite answer with the default prompt and
    model while the deployed function uses the tenant's — and a local result says
    nothing about the deployment it is supposed to stand in for. That is a worse
    failure than no local run at all, because it looks like evidence.
    """

    @pytest.fixture
    def tenants_dir(self, tmp_path, monkeypatch):
        """A real `tenants/` directory, reached the way the CLI reaches it.

        `TENANTS_DIR` is a relative path resolved against the working directory,
        so chdir is the honest way to redirect it — patching the module attribute
        does nothing, because it is bound as a default argument.
        """
        directory = tmp_path / "tenants"
        directory.mkdir()
        (directory / "legal.yaml").write_text(
            "tenant: legal\ndisplay_name: Legal\n"
            "agent:\n  prompt: legal\n  prompt_version: 2\n  model: model-a\n",
            encoding="utf-8",
        )
        (directory / "acme.yaml").write_text(
            "tenant: acme\ndisplay_name: Acme\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        return directory

    def test_the_tenants_agent_config_is_written(self, tenants_dir):
        import write_env

        values = write_env.agent_settings("legal")

        assert values == {
            "PROMPT_NAME": "legal",
            "PROMPT_VERSION": "2",
            "MODEL_ID": "model-a",
        }

    def test_a_default_tenant_does_not_override_model_id(self, tenants_dir):
        """MODEL_ID absent means the deployment default applies, as in the Lambda."""
        import write_env

        values = write_env.agent_settings("acme")

        assert "MODEL_ID" not in values
        assert values["PROMPT_NAME"] == "system"
        assert values["PROMPT_VERSION"] == ""
