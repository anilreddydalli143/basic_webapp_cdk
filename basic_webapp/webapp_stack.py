"""The CloudFormation stack that assembles the whole web application.

This file is deliberately thin. It does three things and nothing else:

  1. Load the configuration for the requested environment.
  2. Create the four layers, in dependency order, and hand each one the
     references it needs from the previous ones.
  3. Declare the CloudFormation **outputs** — the handful of values a human or
     a CI pipeline needs after the deployment finishes.

All the interesting detail lives in ``basic_webapp/constructs/``. Keeping the
stack at this "wiring diagram" altitude means a reviewer can understand the
architecture from one screen of code.
"""

from typing import Optional

from aws_cdk import CfnOutput, Stack, Tags
from constructs import Construct

from .config import EnvironmentConfig, load_config
from .constructs.compute import WebAppCompute
from .constructs.database import Database
from .constructs.network import Network
from .constructs.storage import StaticAssets


class WebAppStack(Stack):
    """A scalable, private-by-default web application stack.

    Layer order matters, because each layer consumes the one above it:

        Network  ->  StaticAssets  ->  WebAppCompute  ->  Database
        (VPC)        (S3 buckets)      (ALB + ECS)        (RDS, optional)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Optional[EnvironmentConfig] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # `config` is injectable so that unit tests can build a stack with
        # specific settings without needing a cdk.json on disk.
        self.config = config or load_config(self)

        # ------------------------------------------------------------------
        # Tags
        # ------------------------------------------------------------------
        # Tags.of(self) applies these to *every* taggable resource in the stack.
        # They are what makes AWS Cost Explorer able to answer "how much does
        # the dev environment cost?" and what lets automated policies find
        # resources they are allowed to touch.
        Tags.of(self).add("Project", self.config.project_name)
        Tags.of(self).add("Environment", self.config.env_name)
        Tags.of(self).add("Owner", self.config.owner)
        Tags.of(self).add("ManagedBy", "aws-cdk")

        # ------------------------------------------------------------------
        # 1. Network — VPC, public/private/isolated subnets, NAT, flow logs
        # ------------------------------------------------------------------
        self.network = Network(self, "Network", config=self.config)

        # ------------------------------------------------------------------
        # 2. Storage — private assets bucket (+ shared access-log bucket)
        # ------------------------------------------------------------------
        self.static_assets = StaticAssets(self, "Storage", config=self.config)

        # ------------------------------------------------------------------
        # 3. Compute — ALB in public subnets, ECS tasks in private subnets
        # ------------------------------------------------------------------
        self.compute = WebAppCompute(
            self,
            "Compute",
            config=self.config,
            network=self.network,
            static_assets=self.static_assets,
        )

        # ------------------------------------------------------------------
        # 4. Database (optional bonus) — PostgreSQL in the isolated subnets
        # ------------------------------------------------------------------
        self.database: Optional[Database] = None
        if self.config.enable_database:
            self.database = Database(
                self,
                "Data",
                config=self.config,
                network=self.network,
                # Passing the security group and role in — rather than letting
                # the database construct reach into the compute construct —
                # keeps the dependency one-directional and easy to follow.
                client_security_group=self.compute.instance_security_group,
                client_role=self.compute.task_role,
            )

        self._add_outputs()

    # ---------------------------------------------------------------------- #
    # CloudFormation outputs                                                 #
    # ---------------------------------------------------------------------- #
    def _add_outputs(self) -> None:
        """Publish the values people actually need after a deployment.

        ``export_name`` matters: an exported output can be imported by *another*
        stack (``Fn.import_value``), which is how you split a large system into
        several stacks later without rewriting anything. Export names must be
        unique per account+region, so they are prefixed with the stack name.
        """
        prefix = self.stack_name

        # --- Required by the brief -----------------------------------------
        CfnOutput(
            self,
            "AlbDnsName",
            value=self.compute.load_balancer.load_balancer_dns_name,
            description="Public DNS name of the Application Load Balancer",
            export_name=f"{prefix}-AlbDnsName",
        )
        CfnOutput(
            self,
            "ApplicationUrl",
            value=self.compute.service_url,
            description="Open this in a browser to reach the application",
            export_name=f"{prefix}-ApplicationUrl",
        )
        CfnOutput(
            self,
            "S3BucketName",
            value=self.static_assets.bucket.bucket_name,
            description="Private S3 bucket holding static assets (served via pre-signed URLs)",
            export_name=f"{prefix}-S3BucketName",
        )
        CfnOutput(
            self,
            "S3BucketArn",
            value=self.static_assets.bucket.bucket_arn,
            description="ARN of the static assets bucket (for cross-account IAM policies)",
            export_name=f"{prefix}-S3BucketArn",
        )

        # --- Useful operational context ------------------------------------
        CfnOutput(
            self,
            "VpcId",
            value=self.network.vpc.vpc_id,
            description="VPC that hosts the application",
            export_name=f"{prefix}-VpcId",
        )
        CfnOutput(
            self,
            "EcsClusterName",
            value=self.compute.cluster.cluster_name,
            description="ECS cluster name (use with `aws ecs list-tasks`)",
            export_name=f"{prefix}-EcsClusterName",
        )
        CfnOutput(
            self,
            "EcsServiceName",
            value=self.compute.service.service_name,
            description="ECS service name (use with `aws ecs update-service`)",
            export_name=f"{prefix}-EcsServiceName",
        )
        CfnOutput(
            self,
            "AccessLogsBucketName",
            value=self.static_assets.log_bucket.bucket_name,
            description="Bucket holding ALB access logs and S3 server access logs",
            export_name=f"{prefix}-AccessLogsBucketName",
        )

        # --- Database outputs, only when a database was created ------------
        if self.database is not None:
            CfnOutput(
                self,
                "RdsEndpoint",
                value=self.database.endpoint_address,
                description="Private hostname of the PostgreSQL instance",
                export_name=f"{prefix}-RdsEndpoint",
            )
            CfnOutput(
                self,
                "RdsPort",
                value=self.database.endpoint_port,
                description="PostgreSQL port",
                export_name=f"{prefix}-RdsPort",
            )
            if self.database.secret is not None:
                # Only the ARN is published — never the password itself. A
                # CloudFormation output is readable by anyone who can describe
                # the stack, so secrets must never be placed in one.
                CfnOutput(
                    self,
                    "RdsSecretArn",
                    value=self.database.secret.secret_arn,
                    description=(
                        "Secrets Manager ARN holding the DB username/password. "
                        "Read it with: aws secretsmanager get-secret-value --secret-id <arn>"
                    ),
                    export_name=f"{prefix}-RdsSecretArn",
                )
