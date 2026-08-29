"""Tests that map 1:1 onto the requirements in the exercise brief.

Each test names the requirement it protects, so a failure tells you which
promise the infrastructure just stopped keeping. This is "infrastructure as
code" taken seriously: the design intent is executable, not just documented.

Run with:  pytest -v
"""

import json

import pytest
from aws_cdk.assertions import Match, Template

from .conftest import build_config, synth

# ===========================================================================
# Requirement 1 — VPC: public + private subnets across >= 2 AZs, with NAT
# ===========================================================================


def test_vpc_exists_with_the_configured_cidr(template: Template) -> None:
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {
            "CidrBlock": "10.20.0.0/16",
            # Required for private DNS resolution of RDS and VPC endpoints.
            "EnableDnsHostnames": True,
            "EnableDnsSupport": True,
        },
    )


def test_subnets_span_at_least_two_availability_zones(template: Template) -> None:
    subnets = template.find_resources("AWS::EC2::Subnet")
    availability_zones = {
        subnet["Properties"]["AvailabilityZone"] for subnet in subnets.values()
    }
    assert len(availability_zones) >= 2, (
        "The brief requires at least two Availability Zones; found "
        f"{sorted(availability_zones)}"
    )


def test_three_subnet_tiers_exist(template: Template) -> None:
    """Public (ALB), private-with-egress (ECS), isolated (database)."""
    subnets = template.find_resources("AWS::EC2::Subnet")
    tiers = set()
    for subnet in subnets.values():
        for tag in subnet["Properties"].get("Tags", []):
            if tag["Key"] == "aws-cdk:subnet-name":
                tiers.add(tag["Value"])
    assert tiers == {"public-alb", "private-app", "isolated-data"}, tiers


def test_nat_gateway_lets_private_subnets_reach_the_internet(
    template: Template,
) -> None:
    template.resource_count_is("AWS::EC2::NatGateway", 1)
    # A NAT Gateway is useless without a default route pointing at it, so
    # assert the route exists too — this is the part people forget.
    template.has_resource_properties(
        "AWS::EC2::Route",
        {
            "DestinationCidrBlock": "0.0.0.0/0",
            "NatGatewayId": Match.any_value(),
        },
    )


def test_public_subnets_do_not_auto_assign_public_ips(template: Template) -> None:
    """Nothing should get a public IP just by being launched in a public subnet."""
    subnets = template.find_resources("AWS::EC2::Subnet")
    for logical_id, subnet in subnets.items():
        assert subnet["Properties"].get("MapPublicIpOnLaunch", False) is False, (
            f"{logical_id} auto-assigns public IPs"
        )


def test_vpc_flow_logs_are_enabled(template: Template) -> None:
    template.has_resource_properties(
        "AWS::EC2::FlowLog", {"TrafficType": "ALL", "ResourceType": "VPC"}
    )


# ===========================================================================
# Requirement 2 — ECS tasks in private subnets, ASG min 2 / max 4, IAM role
# ===========================================================================


def test_auto_scaling_group_is_min_2_max_4(template: Template) -> None:
    template.has_resource_properties(
        "AWS::AutoScaling::AutoScalingGroup",
        {"MinSize": "2", "MaxSize": "4"},
    )


def test_container_hosts_launch_only_in_private_subnets(template: Template) -> None:
    """The single most important placement assertion in the whole suite."""
    asgs = template.find_resources("AWS::AutoScaling::AutoScalingGroup")
    (asg,) = asgs.values()
    subnet_refs = {
        ref["Ref"] for ref in asg["Properties"]["VPCZoneIdentifier"] if "Ref" in ref
    }

    subnets = template.find_resources("AWS::EC2::Subnet")
    for logical_id in subnet_refs:
        tags = {
            tag["Key"]: tag["Value"]
            for tag in subnets[logical_id]["Properties"].get("Tags", [])
        }
        assert tags.get("aws-cdk:subnet-name") == "private-app", (
            f"ECS host subnet {logical_id} is not in the private tier"
        )


def test_instances_require_imdsv2(template: Template) -> None:
    """IMDSv2 closes the SSRF-to-credential-theft path."""
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateData": Match.object_like(
                {"MetadataOptions": Match.object_like({"HttpTokens": "required"})}
            )
        },
    )


def test_instance_root_volume_is_encrypted(template: Template) -> None:
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateData": Match.object_like(
                {
                    "BlockDeviceMappings": Match.array_with(
                        [
                            Match.object_like(
                                {"Ebs": Match.object_like({"Encrypted": True})}
                            )
                        ]
                    )
                }
            )
        },
    )


def test_instance_role_can_use_s3_and_cloudwatch(template: Template) -> None:
    """The brief's explicit requirement, checked as *scoped* permissions."""
    policies = template.find_resources("AWS::IAM::Policy")
    all_statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    rendered = json.dumps(all_statements)

    assert "s3:GetObject" in rendered, "no S3 read permission was granted"
    assert "logs:PutLogEvents" in rendered, "no CloudWatch Logs permission was granted"
    assert "cloudwatch:PutMetricData" in rendered, "no CloudWatch metrics permission"

    # And the important half: the S3 grants must not be account-wide.
    s3_statements = [
        statement
        for statement in all_statements
        if any(
            action.startswith("s3:") for action in _as_list(statement.get("Action", []))
        )
    ]
    assert s3_statements, "expected at least one S3 statement"
    for statement in s3_statements:
        assert statement["Resource"] != "*", (
            "S3 permissions must be scoped to the assets bucket, not '*'"
        )


def test_ecs_service_runs_the_configured_number_of_tasks(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "DesiredCount": 2,
            # Capacity-provider strategy (not LaunchType) is what links the
            # service to the Auto Scaling Group's managed scaling.
            "CapacityProviderStrategy": Match.any_value(),
        },
    )


def test_ecs_task_autoscaling_is_configured(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalableTarget",
        {"MinCapacity": 2, "MaxCapacity": 4},
    )
    # CPU, memory and requests-per-target policies.
    template.resource_count_is("AWS::ApplicationAutoScaling::ScalingPolicy", 3)


def test_capacity_provider_uses_managed_scaling(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ECS::CapacityProvider",
        {
            "AutoScalingGroupProvider": Match.object_like(
                {
                    "ManagedScaling": Match.object_like(
                        {"Status": "ENABLED", "TargetCapacity": 100}
                    )
                }
            )
        },
    )


# ===========================================================================
# Requirement 3 — ALB in public subnets, HTTP/HTTPS only
# ===========================================================================


def test_load_balancer_is_internet_facing(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {
            "Scheme": "internet-facing",
            "Type": "application",
        },
    )


def test_load_balancer_sits_in_public_subnets(template: Template) -> None:
    (alb,) = template.find_resources(
        "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ).values()
    subnet_refs = {ref["Ref"] for ref in alb["Properties"]["Subnets"] if "Ref" in ref}

    subnets = template.find_resources("AWS::EC2::Subnet")
    for logical_id in subnet_refs:
        tags = {
            tag["Key"]: tag["Value"]
            for tag in subnets[logical_id]["Properties"].get("Tags", [])
        }
        assert tags.get("aws-cdk:subnet-name") == "public-alb"


def test_alb_security_group_allows_only_http_and_https(template: Template) -> None:
    """The brief's security requirement, asserted exhaustively."""
    security_groups = template.find_resources("AWS::EC2::SecurityGroup")
    alb_groups = [
        group
        for group in security_groups.values()
        if "Public entry point" in group["Properties"].get("GroupDescription", "")
    ]
    assert len(alb_groups) == 1, "expected exactly one ALB security group"

    ingress_ports = {
        (rule["FromPort"], rule["ToPort"])
        for rule in alb_groups[0]["Properties"]["SecurityGroupIngress"]
    }
    assert ingress_ports == {(80, 80), (443, 443)}, (
        f"ALB must accept only ports 80 and 443; found {sorted(ingress_ports)}"
    )


def test_container_hosts_accept_traffic_only_from_the_load_balancer(
    template: Template,
) -> None:
    """No ingress rule on the host security group may use an IP range."""
    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
    assert ingress_rules, "expected security-group-to-security-group ingress rules"
    for logical_id, rule in ingress_rules.items():
        properties = rule["Properties"]
        assert "CidrIp" not in properties and "CidrIpv6" not in properties, (
            f"{logical_id} opens a raw IP range into a private-tier resource"
        )
        assert "SourceSecurityGroupId" in properties


def test_no_ssh_access_anywhere(template: Template) -> None:
    """Operators use SSM Session Manager, so port 22 must be closed everywhere."""
    for resource_type in ("AWS::EC2::SecurityGroup", "AWS::EC2::SecurityGroupIngress"):
        for logical_id, resource in template.find_resources(resource_type).items():
            properties = resource["Properties"]
            rules = properties.get("SecurityGroupIngress", [properties])
            for rule in rules:
                from_port = rule.get("FromPort")
                to_port = rule.get("ToPort")
                # Ports can render as CloudFormation intrinsics (a dict) rather
                # than literal numbers; there is nothing to compare in that case.
                if not isinstance(from_port, int) or not isinstance(to_port, int):
                    continue
                assert not (from_port <= 22 <= to_port), f"{logical_id} exposes SSH"


def test_target_group_has_a_real_health_check(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "HealthCheckEnabled": True,
            "HealthCheckPath": "/",
            "HealthyThresholdCount": 2,
            "UnhealthyThresholdCount": 3,
            "Matcher": {"HttpCode": "200-399"},
        },
    )


def test_alb_drops_invalid_http_headers(template: Template) -> None:
    (alb,) = template.find_resources(
        "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ).values()
    attributes = {
        attribute["Key"]: attribute["Value"]
        for attribute in alb["Properties"]["LoadBalancerAttributes"]
    }
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"
    assert attributes["access_logs.s3.enabled"] == "true"


# ===========================================================================
# Requirement 4 — private S3 bucket served through pre-signed URLs
# ===========================================================================


def test_assets_bucket_blocks_all_public_access(template: Template) -> None:
    """Pre-signed URLs are only meaningful if the bucket itself is closed."""
    buckets = template.find_resources("AWS::S3::Bucket")
    assert buckets, "no S3 bucket was created"
    for logical_id, bucket in buckets.items():
        block = bucket["Properties"]["PublicAccessBlockConfiguration"]
        assert block == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, f"{logical_id} does not block all public access"


def test_buckets_are_encrypted_at_rest(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        encryption = bucket["Properties"]["BucketEncryption"]
        algorithms = {
            rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"]
            for rule in encryption["ServerSideEncryptionConfiguration"]
        }
        assert algorithms == {"AES256"}, f"{logical_id}: {algorithms}"


def test_buckets_reject_plain_http(template: Template) -> None:
    """enforce_ssl renders as an explicit Deny on aws:SecureTransport=false."""
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert policies, "no bucket policies were created"
    for logical_id, policy in policies.items():
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        deny_insecure = [
            statement
            for statement in statements
            if statement["Effect"] == "Deny"
            and statement.get("Condition", {})
            .get("Bool", {})
            .get("aws:SecureTransport")
            in ("false", False)
        ]
        assert deny_insecure, f"{logical_id} allows unencrypted requests"


def test_assets_bucket_is_versioned(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            # CORS is what makes a browser fetch() of a pre-signed URL work.
            "CorsConfiguration": Match.any_value(),
        },
    )


def test_assets_bucket_has_server_access_logging(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LoggingConfiguration": Match.object_like(
                {"LogFilePrefix": "s3-access-logs/"}
            )
        },
    )


# ===========================================================================
# Requirement 5 — CloudFormation outputs
# ===========================================================================


@pytest.mark.parametrize(
    "output_name",
    [
        "AlbDnsName",
        "ApplicationUrl",
        "S3BucketName",
        "S3BucketArn",
        "VpcId",
        "EcsClusterName",
        "EcsServiceName",
        "AccessLogsBucketName",
        "RdsEndpoint",
        "RdsPort",
        "RdsSecretArn",
    ],
)
def test_required_output_is_present(template: Template, output_name: str) -> None:
    outputs = template.find_outputs(output_name)
    assert outputs, f"missing CloudFormation output: {output_name}"
    assert outputs[output_name].get("Description"), (
        f"{output_name} has no description; outputs are read by humans"
    )


def test_no_output_leaks_a_secret_value(template: Template) -> None:
    """Outputs are readable by anyone who can describe the stack.

    Only the ``Value`` of each output is inspected — a Description is free to
    mention the word "password" while explaining where to fetch one from.
    """
    values = json.dumps(
        [output.get("Value") for output in template.find_outputs("*").values()]
    )
    for forbidden in ("Password", "password", "SecretString", "resolve:secretsmanager"):
        assert forbidden not in values, f"an output appears to expose {forbidden}"


# ===========================================================================
# Bonus — RDS security posture
# ===========================================================================


def test_database_is_private_and_encrypted(template: Template) -> None:
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "PubliclyAccessible": False,
            "StorageEncrypted": True,
            "Engine": "postgres",
            "EnableIAMDatabaseAuthentication": True,
        },
    )


def test_database_lives_in_isolated_subnets(template: Template) -> None:
    (subnet_group,) = template.find_resources("AWS::RDS::DBSubnetGroup").values()
    subnet_refs = {
        ref["Ref"] for ref in subnet_group["Properties"]["SubnetIds"] if "Ref" in ref
    }
    subnets = template.find_resources("AWS::EC2::Subnet")
    for logical_id in subnet_refs:
        tags = {
            tag["Key"]: tag["Value"]
            for tag in subnets[logical_id]["Properties"].get("Tags", [])
        }
        assert tags.get("aws-cdk:subnet-name") == "isolated-data"


def test_database_forces_tls(template: Template) -> None:
    template.has_resource_properties(
        "AWS::RDS::DBParameterGroup",
        {"Parameters": Match.object_like({"rds.force_ssl": "1"})},
    )


def test_database_password_is_generated_into_secrets_manager(
    template: Template,
) -> None:
    template.resource_count_is("AWS::SecretsManager::Secret", 1)
    # No plaintext password may appear anywhere in the template.
    rendered = json.dumps(template.to_json())
    assert (
        "MasterUserPassword" not in rendered or "{{resolve:secretsmanager" in rendered
    )


def test_database_password_rotates_automatically(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SecretsManager::RotationSchedule",
        {"RotationRules": Match.object_like({"ScheduleExpression": "rate(30 days)"})},
    )


# ===========================================================================
# Configuration behaviour
# ===========================================================================


def test_https_listener_replaces_http_when_a_certificate_is_supplied() -> None:
    config = build_config(
        certificate_arn="arn:aws:acm:us-east-1:111122223333:certificate/abc-123"
    )
    https_template = synth(config)

    # 443 with the certificate...
    https_template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 443,
            "Protocol": "HTTPS",
            "SslPolicy": Match.any_value(),
            "Certificates": Match.any_value(),
        },
    )
    # ...and port 80 becomes a permanent redirect rather than a way in.
    https_template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 80,
            "DefaultActions": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Type": "redirect",
                            "RedirectConfig": Match.object_like(
                                {
                                    "Protocol": "HTTPS",
                                    "Port": "443",
                                    "StatusCode": "HTTP_301",
                                }
                            ),
                        }
                    )
                ]
            ),
        },
    )


def test_database_can_be_switched_off() -> None:
    template = synth(build_config(enable_database=False))
    template.resource_count_is("AWS::RDS::DBInstance", 0)
    assert not template.find_outputs("RdsEndpoint")


def test_production_settings_protect_data() -> None:
    config = build_config(
        env_name="prod",
        retain_data_on_delete=True,
        nat_gateways=2,
        db_multi_az=True,
    )
    template = synth(config)

    # RETAIN / SNAPSHOT means a stack deletion cannot take the data with it.
    for bucket in template.find_resources("AWS::S3::Bucket").values():
        assert bucket["DeletionPolicy"] == "Retain"
    (database,) = template.find_resources("AWS::RDS::DBInstance").values()
    assert database["DeletionPolicy"] == "Snapshot"
    assert database["Properties"]["MultiAZ"] is True
    assert database["Properties"]["DeletionProtection"] is True


# ===========================================================================
# Configuration validation (fail fast, before anything is deployed)
# ===========================================================================


@pytest.mark.parametrize(
    "overrides,expected_message",
    [
        ({"max_azs": 1}, "at least 2"),
        ({"asg_min_capacity": 5, "asg_max_capacity": 4}, "Auto Scaling bounds"),
        ({"task_desired_count": 99}, "task_desired_count"),
        ({"nat_gateways": 0}, "nat_gateways"),
        (
            {"env_name": "prod", "retain_data_on_delete": False, "nat_gateways": 2},
            "retain_data_on_delete",
        ),
        (
            {"env_name": "prod", "retain_data_on_delete": True, "nat_gateways": 1},
            "NAT Gateway per Availability Zone",
        ),
    ],
)
def test_invalid_configuration_is_rejected(overrides, expected_message) -> None:
    with pytest.raises(ValueError, match=expected_message):
        build_config(**overrides)


def _as_list(value):
    """IAM renders a single action as a string and several as a list."""
    return value if isinstance(value, list) else [value]


# ===========================================================================
# Command-line overrides
# ===========================================================================


def test_command_line_override_coerces_types() -> None:
    """The CDK CLI passes every -c value as a string, so "false" must not be
    treated as truthy and "4" must not be left as text."""
    from basic_webapp.config import _coerce

    assert _coerce("enable_database", "false", bool) is False
    assert _coerce("enable_database", "TRUE", bool) is True
    assert _coerce("asg_max_capacity", "4", int) == 4
    assert _coerce("instance_type", "t3.small", str) == "t3.small"
    # Already the right type: passed straight through.
    assert _coerce("db_multi_az", True, bool) is True


def test_command_line_override_reports_a_clear_error() -> None:
    from basic_webapp.config import _coerce

    with pytest.raises(ValueError, match="as a bool"):
        _coerce("enable_database", "maybe", bool)
    with pytest.raises(ValueError, match="as a int"):
        _coerce("max_azs", "two", int)
