"""Typed configuration for the web application stack.

WHY DOES THIS FILE EXIST?
-------------------------
CDK apps are just Python programs, so it is very tempting to hard-code values
("t3.micro", "10.20.0.0/16", min 2 / max 4 ...) directly inside the stack code.
That works, but it has two problems:

  1. To deploy a second, differently-sized copy of the stack (say a production
     one) you have to edit the code, which risks breaking the dev one.
  2. The values end up scattered across hundreds of lines, so nobody can answer
     "how big is prod?" without reading the whole stack.

So instead every tunable value lives in ``cdk.json`` under ``context`` and is
loaded here into a frozen (read-only) dataclass. The stack then simply reads
``config.asg_min_capacity`` and never worries about where the number came from.

HOW TO USE IT
-------------
    cdk deploy                     # uses the "dev" settings from cdk.json
    cdk deploy -c env=prod         # uses the "prod" settings
    cdk deploy -c env=dev -c certificate_arn=arn:aws:acm:us-east-1:111:certificate/abc
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from constructs import Construct


@dataclass(frozen=True)
class EnvironmentConfig:
    """All settings for one deployment environment (dev, prod, ...).

    ``frozen=True`` makes instances immutable: once the configuration is built,
    no piece of stack code can accidentally change it half-way through synthesis.
    That keeps the generated CloudFormation template deterministic.
    """

    # --- Identity -----------------------------------------------------------
    env_name: str  # "dev" / "prod" — used in resource names and tags
    project_name: str  # short slug used as a prefix for names
    owner: str  # team that owns the stack (goes into a cost-allocation tag)

    # --- Networking ---------------------------------------------------------
    vpc_cidr: str  # private IP range for the whole VPC, e.g. 10.20.0.0/16
    max_azs: int  # how many Availability Zones (physical data centres) to span
    nat_gateways: int  # 1 = cheap (single point of failure), = max_azs is HA
    enable_private_link_endpoints: bool  # VPC interface endpoints for ECR/logs

    # --- Compute (EC2 Auto Scaling Group that hosts the ECS tasks) ----------
    instance_type: str  # EC2 size for the container hosts
    asg_min_capacity: int  # never fewer than this many EC2 hosts
    asg_max_capacity: int  # never more than this many EC2 hosts

    # --- Application containers (ECS tasks) ---------------------------------
    container_image: str  # image reference, e.g. "amazon/amazon-ecs-sample"
    container_port: int  # port the app listens on *inside* the container
    task_desired_count: int  # how many copies of the app to run at steady state
    task_min_count: int  # lower bound for task auto scaling
    task_max_count: int  # upper bound for task auto scaling

    # --- Database (bonus requirement) ---------------------------------------
    enable_database: bool  # set false to skip RDS entirely (faster deploys)
    db_instance_type: str
    db_multi_az: bool  # true = standby replica in a second AZ (HA, 2x cost)
    db_performance_insights: bool  # not supported on every small instance size

    # --- Operational behaviour ---------------------------------------------
    # retain_data_on_delete=True protects the S3 bucket and the database from
    # being destroyed when the stack is deleted. Always True in production.
    retain_data_on_delete: bool
    log_retention_days: int

    # --- Optional TLS certificate ------------------------------------------
    # If an ACM certificate ARN is supplied the load balancer serves HTTPS on
    # 443 and permanently redirects port 80 -> 443. Without a certificate we can
    # only serve plain HTTP, because AWS will not create an HTTPS listener that
    # has no certificate to present.
    certificate_arn: str | None = None

    # --- Free-form extras ---------------------------------------------------
    tags: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Derived helpers                                                     #
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        """True for environments that must not lose data or be interrupted."""
        return self.env_name.lower() in ("prod", "production")

    @property
    def https_enabled(self) -> bool:
        """True when we have a certificate and can therefore terminate TLS."""
        return bool(self.certificate_arn)

    def resource_name(self, suffix: str) -> str:
        """Build a consistent, human-readable physical name.

        Example: ``resource_name("assets")`` -> ``basic-webapp-dev-assets``.
        Consistent naming makes resources easy to find in the AWS console and
        easy to target with IAM policies.
        """
        return f"{self.project_name}-{self.env_name}-{suffix}".lower()


# Values used when cdk.json does not specify them. Keeping the defaults here
# means a missing key produces a sensible stack instead of a KeyError crash.
_DEFAULTS: dict[str, Any] = {
    "vpc_cidr": "10.20.0.0/16",
    "max_azs": 2,
    "nat_gateways": 1,
    "enable_private_link_endpoints": False,
    "instance_type": "t3.micro",
    "asg_min_capacity": 2,
    "asg_max_capacity": 4,
    "container_image": "amazon/amazon-ecs-sample",
    "container_port": 80,
    "task_desired_count": 2,
    "task_min_count": 2,
    "task_max_count": 4,
    "enable_database": True,
    "db_instance_type": "t3.micro",
    "db_multi_az": False,
    "db_performance_insights": False,
    "retain_data_on_delete": False,
    "log_retention_days": 7,
}


def load_config(scope: Construct) -> EnvironmentConfig:
    """Read ``cdk.json`` context (plus any ``-c key=value`` overrides).

    ``scope`` is normally the CDK ``App``. Every construct exposes
    ``node.try_get_context(...)``, which walks up the construct tree and finally
    reaches the values in ``cdk.json`` / the command line.

    Raises:
        ValueError: if the requested environment is not defined, or if the
            numbers it contains cannot possibly work. Failing here — at synth
            time, in a fraction of a second — is far better than failing 20
            minutes into a CloudFormation deployment.
    """
    env_name = scope.node.try_get_context("env") or "dev"
    all_envs = scope.node.try_get_context("environments") or {}

    if env_name not in all_envs:
        available = ", ".join(sorted(all_envs)) or "<none>"
        raise ValueError(
            f"Unknown environment '{env_name}'. Defined environments: {available}. "
            f"Add a block under context.environments in cdk.json, or deploy with "
            f"-c env=<one of the above>."
        )

    # Precedence, lowest to highest:
    #   1. the built-in defaults above,
    #   2. the environment block in cdk.json,
    #   3. anything passed on the command line as -c key=value.
    values: dict[str, Any] = {**_DEFAULTS, **all_envs[env_name]}
    values.update(_command_line_overrides(scope, values))

    # A certificate is deployment-specific (it depends on your domain), so it is
    # taken from the command line first and only then from the env block.
    certificate_arn = scope.node.try_get_context("certificate_arn") or values.get(
        "certificate_arn"
    )

    config = EnvironmentConfig(
        env_name=env_name,
        project_name=scope.node.try_get_context("project_name") or "basic-webapp",
        owner=scope.node.try_get_context("owner") or "unknown",
        certificate_arn=certificate_arn,
        **{
            k: v
            for k, v in values.items()
            if k in EnvironmentConfig.__annotations__ and k != "certificate_arn"
        },
    )

    _validate(config)
    return config


def _command_line_overrides(
    scope: Construct, current: dict[str, Any]
) -> dict[str, Any]:
    """Collect ``-c key=value`` overrides for any known setting.

    This is what makes ad-hoc tweaks possible without editing cdk.json::

        cdk deploy -c enable_database=false     # skip RDS, deploy much faster
        cdk deploy -c instance_type=t3.small    # try a bigger host

    The CDK CLI passes every ``-c`` value through as a **string**, so "false"
    would otherwise be truthy and "4" would be text where an int is needed.
    Each value is therefore coerced to the type of the setting it replaces.
    """
    overrides: dict[str, Any] = {}
    for key, existing in current.items():
        raw = scope.node.try_get_context(key)
        if raw is None:
            continue
        overrides[key] = _coerce(key, raw, type(existing))
    return overrides


def _coerce(key: str, raw: Any, target_type: type) -> Any:
    """Convert a context value to ``target_type``, with a clear error if it cannot."""
    if isinstance(raw, target_type) and not (
        target_type is int and isinstance(raw, bool)
    ):
        return raw

    text = str(raw).strip()
    try:
        if target_type is bool:
            if text.lower() in ("true", "1", "yes", "on"):
                return True
            if text.lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError(text)
        if target_type is int:
            return int(text)
        return text
    except ValueError:
        raise ValueError(
            f"Could not read -c {key}={raw!r} as a {target_type.__name__}. "
            f"Booleans accept true/false, numbers accept digits."
        ) from None


def _validate(config: EnvironmentConfig) -> None:
    """Fail fast on configurations that AWS would reject (or that cost money
    for no benefit)."""
    if config.max_azs < 2:
        raise ValueError(
            "max_azs must be at least 2: a single Availability Zone cannot be "
            "highly available, and an Application Load Balancer requires "
            "subnets in two or more AZs."
        )

    if config.asg_min_capacity < 1 or config.asg_min_capacity > config.asg_max_capacity:
        raise ValueError(
            f"Invalid Auto Scaling bounds: min={config.asg_min_capacity}, "
            f"max={config.asg_max_capacity}. Expected 1 <= min <= max."
        )

    if config.task_min_count < 1 or config.task_min_count > config.task_max_count:
        raise ValueError(
            f"Invalid ECS task scaling bounds: min={config.task_min_count}, "
            f"max={config.task_max_count}. Expected 1 <= min <= max."
        )

    if not (
        config.task_min_count <= config.task_desired_count <= config.task_max_count
    ):
        raise ValueError(
            f"task_desired_count ({config.task_desired_count}) must sit between "
            f"task_min_count ({config.task_min_count}) and task_max_count "
            f"({config.task_max_count})."
        )

    if config.nat_gateways < 1:
        raise ValueError(
            "nat_gateways must be at least 1, otherwise ECS hosts in the private "
            "subnets cannot pull container images or reach AWS APIs."
        )

    if config.is_production:
        # These are policy checks rather than technical ones: CloudFormation
        # would happily deploy a fragile production stack, so we refuse instead.
        if config.nat_gateways < config.max_azs:
            raise ValueError(
                "Production requires one NAT Gateway per Availability Zone so "
                "that losing an AZ cannot cut off outbound traffic for the rest."
            )

        if not config.retain_data_on_delete:
            raise ValueError(
                "Production must set retain_data_on_delete=true so that deleting "
                "the stack cannot silently destroy the S3 bucket or database."
            )
