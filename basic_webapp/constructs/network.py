"""Networking layer: the VPC, its subnets, NAT Gateways and VPC endpoints.

PLAIN-ENGLISH OVERVIEW
----------------------
A VPC ("Virtual Private Cloud") is your own private network inside AWS. Think of
it as a building, and *subnets* as floors of that building:

  * PUBLIC subnets      - have a door to the street (an Internet Gateway).
                          Only the load balancer lives here.
  * PRIVATE subnets     - no door to the street. Traffic can go *out* through a
                          NAT Gateway (like a one-way mail slot) but nothing on
                          the internet can start a connection *in*. The ECS
                          container hosts live here.
  * ISOLATED subnets    - no route to the internet at all, in either direction.
                          The database lives here.

Spreading these subnets across at least two Availability Zones (independent
data centres a few kilometres apart) is what makes the application survive the
loss of an entire data centre.
"""

from aws_cdk import RemovalPolicy, aws_ec2 as ec2, aws_logs as logs
from constructs import Construct

from ..config import EnvironmentConfig


class Network(Construct):
    """Creates the VPC and everything network-related around it.

    Attributes:
        vpc: the VPC other constructs attach to.
        public_subnets: subnet selection for internet-facing resources (the ALB).
        private_subnets: subnet selection for the ECS container hosts.
        isolated_subnets: subnet selection for the database.
    """

    # Subnet group names. They are referenced by name in a couple of places, so
    # they are constants rather than repeated string literals.
    PUBLIC_SUBNET_NAME = "public-alb"
    PRIVATE_SUBNET_NAME = "private-app"
    ISOLATED_SUBNET_NAME = "isolated-data"

    def __init__(
        self, scope: Construct, construct_id: str, *, config: EnvironmentConfig
    ) -> None:
        super().__init__(scope, construct_id)
        self._config = config

        # ------------------------------------------------------------------
        # 1. VPC flow logs destination
        # ------------------------------------------------------------------
        # Flow logs record *metadata* about every network connection (source,
        # destination, port, allowed/denied). They do not record packet
        # contents, so they are safe to keep, and they are the single most
        # useful artefact when investigating "why can't A reach B?" or a
        # security incident. We create the log group ourselves so that we
        # control its retention period and deletion behaviour.
        flow_log_group = logs.LogGroup(
            self,
            "FlowLogGroup",
            log_group_name=f"/aws/vpc/flowlogs/{config.resource_name('vpc')}",
            retention=self._retention(),
            removal_policy=self._log_removal_policy(),
        )

        # ------------------------------------------------------------------
        # 2. The VPC itself
        # ------------------------------------------------------------------
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=config.resource_name("vpc"),
            # ip_addresses is the modern replacement for the deprecated `cidr`
            # property. /16 gives us 65,536 addresses — plenty of head-room to
            # add subnets later without renumbering the network.
            ip_addresses=ec2.IpAddresses.cidr(config.vpc_cidr),
            max_azs=config.max_azs,
            # NAT Gateways cost money per hour *and* per GB, so dev uses a
            # single shared one while production gets one per AZ (see the
            # validation rules in config.py).
            nat_gateways=config.nat_gateways,
            nat_gateway_provider=ec2.NatProvider.gateway(),
            subnet_configuration=[
                # /24 = 256 addresses per subnet. Public subnets only ever hold
                # load balancer network interfaces, so they stay small.
                ec2.SubnetConfiguration(
                    name=self.PUBLIC_SUBNET_NAME,
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                    # Do NOT hand every launched instance a public IP by
                    # default; anything that truly needs one must ask for it.
                    map_public_ip_on_launch=False,
                ),
                ec2.SubnetConfiguration(
                    name=self.PRIVATE_SUBNET_NAME,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    # /20 = 4,096 addresses. The app tier is what grows, so give
                    # it the most room.
                    cidr_mask=20,
                ),
                ec2.SubnetConfiguration(
                    name=self.ISOLATED_SUBNET_NAME,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
            # Resolve private DNS names for RDS, ElastiCache, VPC endpoints...
            enable_dns_hostnames=True,
            enable_dns_support=True,
            # Remove every rule from the VPC's "default" security group. AWS
            # creates that group automatically and it allows all traffic between
            # anything attached to it — a classic accidental-open-door finding
            # in security audits. We never use it, so we strip it bare.
            restrict_default_security_group=True,
            flow_logs={
                "all-traffic": ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                        flow_log_group
                    ),
                    traffic_type=ec2.FlowLogTrafficType.ALL,
                    max_aggregation_interval=ec2.FlowLogMaxAggregationInterval.TEN_MINUTES,
                )
            },
        )

        # Convenient, self-documenting subnet selections for other constructs.
        self.public_subnets = ec2.SubnetSelection(
            subnet_group_name=self.PUBLIC_SUBNET_NAME
        )
        self.private_subnets = ec2.SubnetSelection(
            subnet_group_name=self.PRIVATE_SUBNET_NAME
        )
        self.isolated_subnets = ec2.SubnetSelection(
            subnet_group_name=self.ISOLATED_SUBNET_NAME
        )

        # ------------------------------------------------------------------
        # 3. Gateway VPC endpoints (free — always worth adding)
        # ------------------------------------------------------------------
        # Without this, every S3 request from a private subnet travels out
        # through the NAT Gateway and back into AWS, and you pay NAT data
        # processing charges for it. A gateway endpoint adds a route so S3
        # traffic never leaves the AWS network. It costs nothing.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[self.private_subnets, self.isolated_subnets],
        )

        # ------------------------------------------------------------------
        # 4. Interface VPC endpoints (optional — these cost ~$7/month each)
        # ------------------------------------------------------------------
        # These let the ECS agent, image pulls and log shipping happen entirely
        # over private AWS networking. In production that is both cheaper (no
        # NAT data charges for image pulls) and more secure (the hosts need far
        # less outbound internet access). Disabled in dev to keep costs near
        # zero — hence the config flag.
        if config.enable_private_link_endpoints:
            endpoint_services = {
                "EcrApiEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
                "EcrDockerEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
                "CloudWatchLogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
                "CloudWatchMonitoringEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
                "SecretsManagerEndpoint": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
                "SsmEndpoint": ec2.InterfaceVpcEndpointAwsService.SSM,
                "SsmMessagesEndpoint": ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
                "Ec2MessagesEndpoint": ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
                "EcsAgentEndpoint": ec2.InterfaceVpcEndpointAwsService.ECS_AGENT,
                "EcsTelemetryEndpoint": ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY,
            }
            for endpoint_id, service in endpoint_services.items():
                self.vpc.add_interface_endpoint(
                    endpoint_id,
                    service=service,
                    subnets=self.private_subnets,
                    # Lets resources reach the endpoint using the normal public
                    # AWS hostname (e.g. ecr.us-east-1.amazonaws.com) with no
                    # application changes.
                    private_dns_enabled=True,
                )

    # ---------------------------------------------------------------------- #
    # Small private helpers                                                  #
    # ---------------------------------------------------------------------- #
    def _retention(self) -> logs.RetentionDays:
        """Map the plain integer from config onto the CDK retention enum."""
        return _to_retention_days(self._config.log_retention_days)

    def _log_removal_policy(self) -> RemovalPolicy:
        """Keep production logs after a stack delete; throw dev logs away."""
        return (
            RemovalPolicy.RETAIN
            if self._config.retain_data_on_delete
            else RemovalPolicy.DESTROY
        )


def _to_retention_days(days: int) -> logs.RetentionDays:
    """Convert a number of days into the nearest supported CloudWatch value.

    CloudWatch Logs only accepts a fixed set of retention periods (1, 3, 5, 7,
    14, 30 days, ...). Rather than making callers memorise that list, we accept
    any integer and round *up* to the next supported value — rounding up is the
    safe direction, because it never deletes logs sooner than requested.
    """
    supported = {
        1: logs.RetentionDays.ONE_DAY,
        3: logs.RetentionDays.THREE_DAYS,
        5: logs.RetentionDays.FIVE_DAYS,
        7: logs.RetentionDays.ONE_WEEK,
        14: logs.RetentionDays.TWO_WEEKS,
        30: logs.RetentionDays.ONE_MONTH,
        60: logs.RetentionDays.TWO_MONTHS,
        90: logs.RetentionDays.THREE_MONTHS,
        120: logs.RetentionDays.FOUR_MONTHS,
        150: logs.RetentionDays.FIVE_MONTHS,
        180: logs.RetentionDays.SIX_MONTHS,
        365: logs.RetentionDays.ONE_YEAR,
        731: logs.RetentionDays.TWO_YEARS,
        1827: logs.RetentionDays.FIVE_YEARS,
        3653: logs.RetentionDays.TEN_YEARS,
    }
    for threshold in sorted(supported):
        if days <= threshold:
            return supported[threshold]
    return logs.RetentionDays.TEN_YEARS


# Re-exported so sibling modules can share the same conversion helper.
to_retention_days = _to_retention_days
