#!/usr/bin/env python3
"""CDK application entry point.

`cdk synth` / `cdk deploy` runs this file. Its job is to create the App, add one
or more stacks to it, and call ``app.synth()`` — which turns the Python objects
into a CloudFormation template under ``cdk.out/``.

USAGE
-----
    cdk synth                       # dev settings, print the template
    cdk deploy                      # dev settings, deploy
    cdk deploy -c env=prod          # production settings
    cdk deploy -c env=prod -c certificate_arn=arn:aws:acm:...   # enable HTTPS
    cdk synth -c nag=true           # additionally run the cdk-nag security scan
"""

import os

import aws_cdk as cdk

from basic_webapp.config import load_config
from basic_webapp.webapp_stack import WebAppStack

app = cdk.App()

# Read cdk.json context (and any -c overrides) into a validated config object.
# Doing this here rather than inside the stack means a bad configuration fails
# immediately, before any resource is defined.
config = load_config(app)

# ---------------------------------------------------------------------------
# Target account and region
# ---------------------------------------------------------------------------
# An "environment-agnostic" stack (no account/region) cannot look up real
# values such as the list of Availability Zones or the latest ECS-optimised
# AMI, so CDK falls back to dummy values and the template becomes less safe.
# We therefore always pin an explicit account and region, preferring:
#
#   1. an explicit -c account=... / -c region=... override,
#   2. otherwise CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION, which the CDK CLI
#      fills in from your current AWS credentials and configured region.
env = cdk.Environment(
    account=app.node.try_get_context("account") or os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or os.getenv("CDK_DEFAULT_REGION"),
)

# The environment name is part of the stack name so that dev and prod can live
# side by side in the same AWS account without colliding.
stack = WebAppStack(
    app,
    f"WebAppStack-{config.env_name}",
    config=config,
    env=env,
    description=(
        f"Basic web application ({config.env_name}): VPC, ALB, ECS on EC2 Auto "
        f"Scaling, private S3 assets bucket"
        + (", RDS PostgreSQL" if config.enable_database else "")
    ),
    # Only mark the stack itself as protected in production. This blocks
    # `cdk destroy` / DeleteStack until someone deliberately turns it off.
    termination_protection=config.retain_data_on_delete,
)

# ---------------------------------------------------------------------------
# Optional: cdk-nag security review (bonus)
# ---------------------------------------------------------------------------
# cdk-nag inspects the *synthesized* template against published AWS best
# practice rule packs (the same checks as AWS Solutions reviews) and fails the
# synth on unresolved findings. It is opt-in so that day-to-day `cdk synth`
# stays fast, and so a missing dev dependency cannot break a deployment.
if app.node.try_get_context("nag"):
    from cdk_nag import AwsSolutionsChecks  # imported lazily: dev dependency only

    from basic_webapp.nag_suppressions import apply_nag_suppressions

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    # Every suppression carries a written justification, so the exceptions are
    # reviewable rather than invisible.
    apply_nag_suppressions(stack)

app.synth()
