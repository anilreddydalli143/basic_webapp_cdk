"""Shared pytest fixtures.

HOW CDK TESTING WORKS (in one paragraph)
----------------------------------------
We never talk to AWS. Instead we build the stack in memory, ask CDK to
*synthesize* it into a CloudFormation template (a big JSON document), and then
make assertions about that document with ``aws_cdk.assertions.Template``.
These tests therefore run in a couple of seconds, cost nothing, and need no
credentials — which makes them safe to run on every push in CI.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from basic_webapp.config import EnvironmentConfig, _validate, load_config
from basic_webapp.webapp_stack import WebAppStack

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _cdk_json_context() -> dict[str, Any]:
    """The context block from cdk.json.

    Loading it matters because it holds the CDK **feature flags**. Those flags
    change how constructs render (for example, whether S3 access logging uses a
    bucket policy or a legacy ACL), so a test App without them would synthesize
    a different template from the one `cdk deploy` produces — and the tests
    would be checking the wrong thing.
    """
    with (PROJECT_ROOT / "cdk.json").open() as handle:
        return json.load(handle).get("context", {})


# The application settings the tests pin explicitly. They are overlaid on top
# of cdk.json's context so that editing cdk.json's dev sizing cannot silently
# change what these tests assert.
DEV_OVERRIDES: dict[str, Any] = {
    "env": "dev",
    "project_name": "basic-webapp",
    "owner": "platform-engineering",
    "environments": {
        "dev": {
            "vpc_cidr": "10.20.0.0/16",
            "max_azs": 2,
            "nat_gateways": 1,
            "instance_type": "t3.micro",
            "asg_min_capacity": 2,
            "asg_max_capacity": 4,
            "task_desired_count": 2,
            "task_min_count": 2,
            "task_max_count": 4,
            "container_image": "amazon/amazon-ecs-sample",
            "container_port": 80,
            "enable_database": True,
            "db_instance_type": "t3.micro",
            "db_multi_az": False,
            "db_performance_insights": False,
            "enable_private_link_endpoints": False,
            "retain_data_on_delete": False,
            "log_retention_days": 7,
        }
    },
}

TEST_CONTEXT: dict[str, Any] = {**_cdk_json_context(), **DEV_OVERRIDES}


def build_config(**overrides: Any) -> EnvironmentConfig:
    """Build an EnvironmentConfig, optionally overriding individual settings.

    Overrides are re-validated, so the tests exercise exactly the same
    guard-rails that ``load_config`` applies during a real deployment.
    """
    app = cdk.App(context=TEST_CONTEXT)
    config = load_config(app)
    if not overrides:
        return config

    # dataclasses.replace produces a new frozen instance with the given fields
    # changed — the clean way to "edit" an immutable object.
    config = replace(config, **overrides)
    _validate(config)
    return config


def synth(config: Optional[EnvironmentConfig] = None) -> Template:
    """Synthesize a stack and return an assertable Template."""
    app = cdk.App(context=TEST_CONTEXT)
    stack = WebAppStack(
        app,
        "TestStack",
        config=config or load_config(app),
        # A fixed fake account/region keeps the template deterministic without
        # requiring AWS credentials.
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    """The default (dev) template. Module-scoped because synthesis is the slow
    part and the template is read-only."""
    return synth()
