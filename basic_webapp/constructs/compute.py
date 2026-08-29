"""Compute layer: Application Load Balancer + ECS tasks on an EC2 Auto Scaling Group.

THE REQUEST PATH, TOP TO BOTTOM
-------------------------------
    Internet
        |  (HTTPS 443 / HTTP 80 only — enforced by the ALB security group)
        v
    Application Load Balancer      <- lives in the PUBLIC subnets
        |  (forwards to a random high "ephemeral" port on a healthy host)
        v
    EC2 container host (in the Auto Scaling Group)   <- PRIVATE subnets
        |
        v
    ECS task = your Docker container listening on port 80 inside itself

TWO INDEPENDENT LAYERS OF SCALING
---------------------------------
This trips people up, so it is worth spelling out:

  * The **Auto Scaling Group** scales the *EC2 hosts* (the hardware). The brief
    asks for min 2 / max 4.
  * **ECS service auto scaling** scales the *number of container copies* running
    on those hosts.

Both are configured below. ECS "capacity provider managed scaling" links them:
if ECS wants to place a task and no host has room, it tells the ASG to add one.

WHY BRIDGE NETWORKING AND NOT `awsvpc`?
---------------------------------------
With bridge mode the container's port 80 is mapped to a *random* high port on
the host, so many copies of the same container can share one host, and the ALB
tracks those random ports automatically. With `awsvpc` each task gets its own
network interface (nicer isolation) but small instance types only support one or
two interfaces, which would cap us at ~1 task per host. Bridge mode is the
right trade-off for an EC2-backed service on small instances; the host security
group below provides the network isolation.
"""

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_sns as sns,
)
from constructs import Construct

from ..config import EnvironmentConfig
from .network import Network, to_retention_days
from .storage import StaticAssets

# The port range the ALB uses when talking to bridge-mode containers. ECS picks
# a free port from this range for every task it starts.
EPHEMERAL_PORT_MIN = 32768
EPHEMERAL_PORT_MAX = 65535

CONTAINER_NAME = "web"


class WebAppCompute(Construct):
    """ALB, ECS cluster, Auto Scaling Group, task definition and service.

    Attributes:
        load_balancer: the public-facing ALB.
        target_group: the ALB target group holding the ECS tasks.
        cluster: the ECS cluster.
        service: the ECS service that keeps N copies of the container running.
        instance_security_group: security group of the EC2 container hosts. The
            database construct uses it as the source of its own ingress rule.
        task_role: the IAM identity the *application code* runs as.
        instance_role: the IAM identity the *EC2 host* runs as.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        network: Network,
        static_assets: StaticAssets,
    ) -> None:
        super().__init__(scope, construct_id)
        self._config = config
        self._network = network

        vpc = network.vpc

        # ==================================================================
        # 1. SECURITY GROUPS — the virtual firewalls
        # ==================================================================
        # A security group is a stateful allow-list. "Stateful" means you only
        # describe the direction a connection is *started* in; the reply is
        # automatically permitted.
        #
        # The pattern below is the important one: the host security group does
        # not trust an IP range, it trusts *the load balancer's security group*.
        # So even if someone attaches something else to the private subnet, it
        # still cannot reach the application ports.

        # --- 1a. Load balancer security group ---
        self.alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            security_group_name=config.resource_name("alb-sg"),
            description="Public entry point: allows only HTTP/80 and HTTPS/443 from the internet",
            # Start with no outbound rules at all. CDK then adds exactly one
            # egress rule — "to the container hosts on the ephemeral port
            # range" — when we register the targets. That is far tighter than
            # the default "allow all outbound".
            allow_all_outbound=False,
        )
        # These are the ONLY two ways in, as required by the brief.
        self.alb_security_group.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(80),
            description="HTTP from the internet (redirected to HTTPS when a certificate is configured)",
        )
        self.alb_security_group.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS from the internet",
        )
        # IPv6 equivalents. The ALB is dual-stack capable and clients on
        # IPv6-only mobile networks would otherwise be silently unreachable.
        self.alb_security_group.add_ingress_rule(
            peer=ec2.Peer.any_ipv6(),
            connection=ec2.Port.tcp(80),
            description="HTTP from the internet over IPv6",
        )
        self.alb_security_group.add_ingress_rule(
            peer=ec2.Peer.any_ipv6(),
            connection=ec2.Port.tcp(443),
            description="HTTPS from the internet over IPv6",
        )

        # --- 1b. Container host security group ---
        self.instance_security_group = ec2.SecurityGroup(
            self,
            "InstanceSecurityGroup",
            vpc=vpc,
            security_group_name=config.resource_name("ecs-host-sg"),
            description="ECS container hosts: reachable only from the load balancer",
            # Hosts need outbound access to pull container images from Docker
            # Hub / ECR and to call AWS APIs (via the NAT Gateway).
            allow_all_outbound=True,
        )
        self.instance_security_group.add_ingress_rule(
            peer=self.alb_security_group,
            connection=ec2.Port.tcp_range(EPHEMERAL_PORT_MIN, EPHEMERAL_PORT_MAX),
            description="Load balancer to bridge-mode container ports",
        )
        # Note what is deliberately ABSENT: there is no SSH (port 22) rule and
        # no bastion host. Operators get a shell through AWS Systems Manager
        # Session Manager (see the instance role below), which is audited in
        # CloudTrail and needs no inbound port at all.

        # ==================================================================
        # 2. IAM ROLES — who is allowed to do what
        # ==================================================================
        # There are three distinct identities in an ECS-on-EC2 setup, and mixing
        # them up is the most common source of "access denied" confusion:
        #
        #   1. instance role   — the EC2 host. Used by the ECS agent and by the
        #                        Docker log driver.
        #   2. execution role  — used by ECS itself to *start* the task (pull
        #                        the image, create the log stream). Created
        #                        automatically by CDK with minimal permissions.
        #   3. task role       — the identity your application code assumes at
        #                        runtime. This is where app permissions belong.

        # --- 2a. Log group for the container's stdout/stderr ---
        # Created explicitly (instead of letting CDK auto-generate one) so that
        # retention and deletion behaviour are under our control. Logs that
        # never expire are a slow, invisible cost leak.
        self.log_group = logs.LogGroup(
            self,
            "ServiceLogGroup",
            log_group_name=f"/aws/ecs/{config.resource_name('service')}",
            retention=to_retention_days(config.log_retention_days),
            removal_policy=(
                RemovalPolicy.RETAIN
                if config.retain_data_on_delete
                else RemovalPolicy.DESTROY
            ),
        )

        # --- 2b. EC2 instance role ---
        self.instance_role = iam.Role(
            self,
            "InstanceRole",
            role_name=config.resource_name("ecs-host-role"),
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Role for the EC2 hosts that run the ECS tasks",
            managed_policies=[
                # Lets the ECS agent register the host with the cluster, poll
                # for work and report task status. This is the AWS-authored
                # minimum for an ECS container instance.
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonEC2ContainerServiceforEC2Role"
                ),
                # Enables Session Manager: shell access with no SSH keys, no
                # open port 22 and full CloudTrail auditing.
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                # Lets the CloudWatch agent publish host metrics (memory, disk)
                # that EC2 does not report out of the box.
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )
        # The brief asks for host-level S3 and CloudWatch access, so both are
        # granted here — but scoped to *this* bucket and *this* log group
        # rather than "*", which is what "least privilege" means in practice.
        static_assets.grant_asset_read(self.instance_role)
        self.log_group.grant_write(self.instance_role)

        # --- 2c. ECS task role (the application's own identity) ---
        self.task_role = iam.Role(
            self,
            "TaskRole",
            role_name=config.resource_name("task-role"),
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime identity of the web application container",
        )
        # Read = also the permission required to *sign* pre-signed GET URLs.
        static_assets.grant_asset_read(self.task_role)
        # Put (upload) but intentionally NOT delete — see storage.py.
        static_assets.grant_asset_write(self.task_role)
        self.log_group.grant_write(self.task_role)
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="PublishCustomApplicationMetrics",
                actions=["cloudwatch:PutMetricData"],
                # cloudwatch:PutMetricData has no ARN to scope to, so AWS
                # requires "*". The condition below is how you still restrict
                # it: the app may only write metrics into its own namespace.
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": f"{config.project_name}/{config.env_name}"
                    }
                },
            )
        )

        # ==================================================================
        # 3. ECS CLUSTER + AUTO SCALING GROUP (the capacity)
        # ==================================================================
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=config.resource_name("cluster"),
            vpc=vpc,
            # Container Insights collects per-task CPU/memory/network metrics.
            # Without it you can see that the host is busy but not which task
            # is responsible.
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # Where scaling notifications go. Subscribe a real email address, a
        # PagerDuty endpoint or a Slack webhook to this topic after deployment:
        #   aws sns subscribe --topic-arn <OpsNotificationTopicArn> \
        #       --protocol email --notification-endpoint you@example.com
        self.ops_topic = sns.Topic(
            self,
            "OpsNotifications",
            topic_name=config.resource_name("ops-notifications"),
            display_name=f"{config.project_name} {config.env_name} scaling events",
            # Refuse any publish that arrives over plain HTTP.
            enforce_ssl=True,
        )

        self.auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            "EcsHostAsg",
            auto_scaling_group_name=config.resource_name("ecs-host-asg"),
            vpc=vpc,
            # >>> The brief's core requirement: tasks run in PRIVATE subnets. <<<
            # These hosts have no public IP address and cannot be reached from
            # the internet; all inbound traffic must come via the ALB.
            vpc_subnets=network.private_subnets,
            instance_type=ec2.InstanceType(config.instance_type),
            # The ECS-optimised Amazon Linux 2023 AMI ships with Docker and the
            # ECS agent pre-installed and is patched by AWS. Because the AMI ID
            # is looked up at synth time, a later `cdk deploy` naturally rolls
            # out the newest patched image.
            machine_image=ecs.EcsOptimizedImage.amazon_linux2023(),
            security_group=self.instance_security_group,
            role=self.instance_role,
            # >>> min 2 / max 4, as specified. <<<
            min_capacity=config.asg_min_capacity,
            max_capacity=config.asg_max_capacity,
            desired_capacity=None,  # let scaling decide; avoids deploy-time drift
            # Force IMDSv2. The instance metadata service is how code on a host
            # obtains the role's credentials; IMDSv1's simple GET request made
            # it reachable through server-side-request-forgery bugs. IMDSv2
            # requires a signed token first and closes that class of attack.
            require_imdsv2=True,
            block_devices=[
                autoscaling.BlockDevice(
                    device_name="/dev/xvda",  # root volume on Amazon Linux 2023
                    volume=autoscaling.BlockDeviceVolume.ebs(
                        volume_size=30,
                        volume_type=autoscaling.EbsDeviceVolumeType.GP3,
                        encrypted=True,  # encryption at rest for the host disk
                        delete_on_termination=True,
                    ),
                )
            ],
            # Replace hosts a few at a time instead of all at once, so a new
            # AMI or instance type never takes the whole service down.
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                min_instances_in_service=config.asg_min_capacity,
                max_batch_size=1,
                pause_time=Duration.minutes(5),
            ),
            # Publish the group-level metrics (desired/in-service/pending
            # capacity) that make scaling behaviour reviewable after the fact.
            group_metrics=[autoscaling.GroupMetrics.all()],
            health_checks=autoscaling.HealthChecks.ec2(
                grace_period=Duration.minutes(5)
            ),
            # Tell operators when hosts are launched, replaced or fail to launch.
            # A silent scaling failure ("we tried to add a host and couldn't")
            # is exactly the event you want to hear about *before* users do.
            notifications=[
                autoscaling.NotificationConfiguration(
                    topic=self.ops_topic,
                    scaling_events=autoscaling.ScalingEvents.ALL,
                )
            ],
        )

        # A capacity provider is the bridge between ECS and the ASG.
        self.capacity_provider = ecs.AsgCapacityProvider(
            self,
            "AsgCapacityProvider",
            auto_scaling_group=self.auto_scaling_group,
            # Managed scaling: ECS watches how much room is left for new tasks
            # and adjusts the ASG's desired count itself, staying within the
            # min/max above. Without this, adding tasks could fail with
            # "no container instance met all of its requirements".
            enable_managed_scaling=True,
            target_capacity_percent=100,  # keep hosts efficiently packed
            # Managed termination protection stops the ASG from killing a host
            # that still has tasks on it. It is the right choice in production,
            # but it also blocks `cdk destroy` until every task is drained,
            # which makes throw-away environments annoying to clean up. Hence:
            # on in production, off elsewhere.
            enable_managed_termination_protection=config.retain_data_on_delete,
        )
        self.cluster.add_asg_capacity_provider(self.capacity_provider)

        # ==================================================================
        # 4. TASK DEFINITION (the blueprint for one running copy of the app)
        # ==================================================================
        self.task_definition = ecs.Ec2TaskDefinition(
            self,
            "TaskDefinition",
            family=config.resource_name("task"),
            network_mode=ecs.NetworkMode.BRIDGE,  # see module docstring
            task_role=self.task_role,
            # No execution_role is passed on purpose: CDK creates one and grants
            # it exactly the image-pull and log-write permissions this task
            # needs — narrower than the AWS-managed policy would be.
        )

        container = self.task_definition.add_container(
            CONTAINER_NAME,
            image=ecs.ContainerImage.from_registry(config.container_image),
            # memory_limit_mib is a hard cap: exceed it and the container is
            # killed. memory_reservation_mib is the soft guarantee ECS uses when
            # deciding how many tasks fit on a host. Setting both lets tasks
            # burst without letting one task starve its neighbours.
            memory_limit_mib=512,
            memory_reservation_mib=256,
            cpu=256,  # 256 CPU units = 0.25 vCPU
            # If the container dies, the whole task is marked failed and ECS
            # replaces it. Correct for a single-container task.
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=CONTAINER_NAME,
                log_group=self.log_group,
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,  # never block the app on log backpressure
            ),
            environment={
                # Passing the bucket name in as an environment variable means
                # the application never has to hard-code it, and the same image
                # works unchanged in dev and prod.
                "ASSETS_BUCKET_NAME": static_assets.bucket.bucket_name,
                "AWS_REGION_NAME": self._region(),
                "APP_ENVIRONMENT": config.env_name,
                "PRESIGNED_URL_TTL_SECONDS": "900",  # 15 minutes
            },
            # A container that is running but wedged still "looks" healthy to
            # the host. Docker-level health checks catch that. This one is
            # commented out because the sample image has no curl/wget; the ALB
            # health check below covers us. Enable it for a real image:
            #
            # health_check=ecs.HealthCheck(
            #     command=["CMD-SHELL", "curl -fsS http://localhost/ || exit 1"],
            #     interval=Duration.seconds(30),
            #     timeout=Duration.seconds(5),
            #     retries=3,
            #     start_period=Duration.seconds(60),
            # ),
        )
        container.add_port_mappings(
            ecs.PortMapping(
                container_port=config.container_port,
                # host_port=0 means "pick any free ephemeral port". This is what
                # allows several copies of the container on one host, and it is
                # why the host security group opens 32768-65535 to the ALB.
                host_port=0,
                protocol=ecs.Protocol.TCP,
            )
        )

        # ==================================================================
        # 5. ECS SERVICE (keeps the desired number of tasks alive)
        # ==================================================================
        self.service = ecs.Ec2Service(
            self,
            "Service",
            service_name=config.resource_name("service"),
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=config.task_desired_count,
            # Run tasks through the capacity provider so that managed scaling
            # (step 3) is actually used.
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(
                    capacity_provider=self.capacity_provider.capacity_provider_name,
                    weight=1,
                )
            ],
            # During a deploy, keep at least half the tasks serving traffic and
            # allow up to double to exist briefly. That is what makes the
            # rolling update a zero-downtime one.
            min_healthy_percent=50,
            max_healthy_percent=200,
            # If a new version fails its health checks, roll back automatically
            # instead of retrying for ever with a broken deployment.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Give a fresh task time to boot before the ALB starts failing it.
            health_check_grace_period=Duration.seconds(60),
            # `aws ecs execute-command` for debugging — an audited replacement
            # for SSH into a container. CDK adds the required SSM permissions
            # to the task role automatically.
            enable_execute_command=True,
            placement_strategies=[
                # Spread tasks across Availability Zones first, so losing one AZ
                # cannot take out every copy of the application...
                ecs.PlacementStrategy.spread_across(
                    ecs.BuiltInAttributes.AVAILABILITY_ZONE
                ),
                # ...then across distinct hosts within an AZ.
                ecs.PlacementStrategy.spread_across_instances(),
            ],
        )

        # ==================================================================
        # 6. APPLICATION LOAD BALANCER
        # ==================================================================
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            load_balancer_name=config.resource_name("alb")[:32],  # AWS limit: 32 chars
            vpc=vpc,
            internet_facing=True,  # gets a public DNS name
            vpc_subnets=network.public_subnets,  # >>> ALB in the PUBLIC subnets <<<
            security_group=self.alb_security_group,
            # Reject requests containing malformed HTTP headers rather than
            # passing them to the app. Blocks a family of request-smuggling and
            # header-injection tricks.
            drop_invalid_header_fields=True,
            # Guard against someone deleting the production entry point with a
            # single click or an accidental `cdk destroy`.
            deletion_protection=config.retain_data_on_delete,
            idle_timeout=Duration.seconds(60),
            ip_address_type=elbv2.IpAddressType.IPV4,
        )
        # Access logs record every HTTP request (client IP, path, status, target
        # response time). Essential for debugging, abuse investigation and most
        # compliance regimes.
        self.load_balancer.log_access_logs(
            static_assets.log_bucket, prefix="alb-access-logs"
        )

        # --- 6a. Target group: the pool of healthy tasks ---
        self.target_group = elbv2.ApplicationTargetGroup(
            self,
            "TargetGroup",
            target_group_name=config.resource_name("tg")[:32],
            vpc=vpc,
            port=config.container_port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            # INSTANCE (not IP) because bridge networking registers
            # host + dynamic port pairs.
            target_type=elbv2.TargetType.INSTANCE,
            targets=[
                self.service.load_balancer_target(
                    container_name=CONTAINER_NAME,
                    container_port=config.container_port,
                )
            ],
            # How long the ALB waits for in-flight requests to finish before it
            # forgets a task that is being replaced. 30s is a good default for a
            # web app; raise it if you have long-running requests.
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                enabled=True,
                path="/",  # TODO: point at a dedicated /healthz endpoint
                protocol=elbv2.Protocol.HTTP,
                # 200-399 tolerates a redirect from "/" — a very common cause of
                # "why are all my targets unhealthy?".
                healthy_http_codes="200-399",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,  # 2 passes -> back in rotation
                unhealthy_threshold_count=3,  # 3 failures -> pulled out
            ),
        )

        # --- 6b. Listeners: what the ALB does with an incoming connection ---
        if config.https_enabled:
            # Preferred setup: terminate TLS at the ALB and force everyone onto
            # it. Certificates are free via AWS Certificate Manager and renew
            # themselves.
            self.load_balancer.add_listener(
                "HttpsListener",
                port=443,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                certificates=[
                    elbv2.ListenerCertificate.from_arn(config.certificate_arn)
                ],
                # RECOMMENDED_TLS tracks the current AWS-recommended cipher
                # suites, so old, weak ciphers are refused.
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
                default_target_groups=[self.target_group],
                # open=False: we already wrote the ingress rules by hand above,
                # so stop CDK from adding a second, duplicate set.
                open=False,
            )
            # Port 80 exists only to bounce visitors to HTTPS. A permanent (301)
            # redirect also lets browsers remember it.
            self.load_balancer.add_listener(
                "HttpRedirectListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.redirect(
                    protocol="HTTPS", port="443", permanent=True
                ),
                open=False,
            )
        else:
            # Fallback for a quick demo with no domain/certificate to hand.
            # Plain HTTP is acceptable for a throw-away environment only —
            # never for anything handling real user data.
            self.load_balancer.add_listener(
                "HttpListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_target_groups=[self.target_group],
                open=False,
            )

        # ==================================================================
        # 7. ECS SERVICE AUTO SCALING (scale the app, not just the hosts)
        # ==================================================================
        scalable_tasks = self.service.auto_scale_task_count(
            min_capacity=config.task_min_count,
            max_capacity=config.task_max_count,
        )
        # Target tracking works like a thermostat: name the number you want to
        # hold and AWS adds or removes tasks to keep it there. Far simpler and
        # less twitchy than hand-written step-scaling rules.
        scalable_tasks.scale_on_cpu_utilization(
            "CpuTargetTracking",
            target_utilization_percent=60,
            # Scale out quickly (a slow site loses users) but scale in slowly,
            # so a brief lull does not remove capacity you need back in a minute.
            scale_out_cooldown=Duration.seconds(60),
            scale_in_cooldown=Duration.minutes(5),
        )
        scalable_tasks.scale_on_memory_utilization(
            "MemoryTargetTracking",
            target_utilization_percent=70,
            scale_out_cooldown=Duration.seconds(60),
            scale_in_cooldown=Duration.minutes(5),
        )
        # Requests-per-task is usually the metric that correlates best with
        # real user-visible latency, because a request can be slow without ever
        # showing up as high CPU (e.g. waiting on the database).
        scalable_tasks.scale_on_request_count(
            "RequestCountTargetTracking",
            requests_per_target=1000,
            target_group=self.target_group,
            scale_out_cooldown=Duration.seconds(60),
            scale_in_cooldown=Duration.minutes(5),
        )

    # ---------------------------------------------------------------------- #
    # Helpers                                                                #
    # ---------------------------------------------------------------------- #
    def _region(self) -> str:
        """The region this construct is being deployed into."""
        return Stack.of(self).region

    @property
    def service_url(self) -> str:
        """Best-guess public URL for the application.

        Note this is the raw ALB DNS name. In a real deployment you would put a
        friendly name in front of it with a Route 53 alias record.
        """
        scheme = "https" if self._config.https_enabled else "http"
        return f"{scheme}://{self.load_balancer.load_balancer_dns_name}"

    def allow_egress_to(
        self, peer: ec2.IPeer, port: ec2.Port, description: str
    ) -> None:
        """Open a specific outbound path from the container hosts.

        Exposed as a method so that other constructs (for example the database)
        can declare the connectivity they need without reaching into this
        construct's internals.
        """
        self.instance_security_group.add_egress_rule(peer, port, description)
