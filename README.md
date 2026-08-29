# Basic Web Application Infrastructure — AWS CDK (Python)

A production-shaped web application stack defined entirely as code: a VPC with
public/private/isolated subnets, an Application Load Balancer, containerised
application tasks on ECS running on an EC2 Auto Scaling Group, a private S3
bucket served through pre-signed URLs, and a PostgreSQL database on RDS.

Everything is deployed with one command, and every design decision is explained
in comments next to the code that implements it.

---

## Table of contents

- [Architecture](#architecture)
- [What gets created](#what-gets-created)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Deploy it](#deploy-it)
- [After deployment](#after-deployment)
- [Pre-signed URLs explained](#pre-signed-urls-explained)
- [Configuration reference](#configuration-reference)
- [Tests and security scanning](#tests-and-security-scanning)
- [CI/CD](#cicd)
- [Cost](#cost)
- [Tearing it down](#tearing-it-down)
- [Design decisions and trade-offs](#design-decisions-and-trade-offs)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
                                Internet
                                    │
                       HTTP :80 / HTTPS :443 only
                                    │
        ┌───────────────────────────▼────────────────────────────┐
        │                    VPC  10.20.0.0/16                   │
        │                                                        │
        │  ┌──────────── PUBLIC subnets (AZ-a, AZ-b) ──────────┐ │
        │  │   Application Load Balancer      NAT Gateway      │ │
        │  └──────────────────┬────────────────────┬───────────┘ │
        │                     │ ephemeral ports    │ outbound    │
        │                     │ 32768-65535        │ only        │
        │  ┌──────────────────▼────────────────────▼───────────┐ │
        │  │        PRIVATE subnets (AZ-a, AZ-b)               │ │
        │  │                                                   │ │
        │  │   EC2 Auto Scaling Group  (min 2 / max 4 hosts)    │ │
        │  │     ├── ECS task (container)                       │ │
        │  │     └── ECS task (container)                       │ │
        │  │   ECS service auto scaling: 2-4 tasks              │ │
        │  └──────────────────┬────────────────────────────────┘ │
        │                     │ PostgreSQL :5432                 │
        │  ┌──────────────────▼────────────────────────────────┐ │
        │  │        ISOLATED subnets (AZ-a, AZ-b)              │ │
        │  │   RDS PostgreSQL  (no internet route at all)       │ │
        │  └───────────────────────────────────────────────────┘ │
        │                                                        │
        │   VPC endpoint ──────► S3 (private assets bucket)       │
        └────────────────────────────────────────────────────────┘
```

Traffic can only enter through the load balancer, on ports 80 and 443. Each
tier's security group trusts *the security group of the tier above it*, not an
IP range — so nothing can be reached by accident.

### Two independent scaling dimensions

| Layer | What scales | Bounds | Driven by |
|---|---|---|---|
| EC2 Auto Scaling Group | container **hosts** | 2 → 4 | ECS capacity-provider managed scaling |
| ECS service | container **copies** (tasks) | 2 → 4 | CPU 60%, memory 70%, 1000 req/task |

---

## What gets created

<details>
<summary><strong>1. Networking</strong> (<code>basic_webapp/constructs/network.py</code>)</summary>

- VPC across 2 AZs in dev, 3 in prod
- **Public** subnets (`/24`) — load balancer and NAT Gateway only
- **Private with egress** subnets (`/20`) — ECS container hosts
- **Isolated** subnets (`/24`) — the database, with no internet route at all
- NAT Gateway: 1 in dev (cheap), one per AZ in prod (highly available)
- VPC **flow logs** to CloudWatch, with configurable retention
- **S3 gateway endpoint** — keeps S3 traffic off the NAT Gateway (free)
- Optional interface endpoints for ECR/Logs/SSM/Secrets Manager (prod)
- The VPC's default security group is stripped of all rules

</details>

<details>
<summary><strong>2. Compute</strong> (<code>basic_webapp/constructs/compute.py</code>)</summary>

- ECS cluster with **Container Insights** enabled
- EC2 Auto Scaling Group — **min 2 / max 4**, ECS-optimised Amazon Linux 2023,
  IMDSv2 required, encrypted GP3 root volume, rolling updates
- ECS capacity provider with managed scaling (target 100% utilisation)
- ECS task definition (bridge networking, dynamic host ports) + service with
  deployment circuit breaker and auto-rollback
- ECS service auto scaling on CPU, memory and ALB requests-per-target
- Application Load Balancer in the public subnets: invalid headers dropped,
  access logs on, HTTPS with a modern TLS policy when a certificate is supplied
  (and a permanent 80 → 443 redirect)
- SNS topic for scaling notifications
- Three separate IAM identities — host role, ECS execution role, task role

</details>

<details>
<summary><strong>3. Storage</strong> (<code>basic_webapp/constructs/storage.py</code>)</summary>

- **Private** assets bucket: all public access blocked, ACLs disabled,
  SSE-S3 encryption, TLS 1.2 minimum, plain HTTP denied, versioning on
- Lifecycle rules for old versions and abandoned multipart uploads
- CORS configured so browsers can fetch pre-signed URLs
- Separate access-log bucket for ALB access logs and S3 server access logs

</details>

<details>
<summary><strong>4. Database</strong> (<code>basic_webapp/constructs/database.py</code>) — bonus</summary>

- PostgreSQL 17 in the isolated subnets, not publicly accessible
- Security group admits only the ECS host security group, on 5432
- Password generated into **Secrets Manager**, rotated every 30 days
- Storage encrypted, GP3, autoscaling 20 GB → 100 GB
- `rds.force_ssl=1` — the server refuses unencrypted connections
- IAM database authentication enabled
- Automated backups; Multi-AZ, Performance Insights and enhanced monitoring
  in production
- `DeletionProtection` + `SNAPSHOT` removal policy in production

</details>

<details>
<summary><strong>5. CloudFormation outputs</strong></summary>

`AlbDnsName`, `ApplicationUrl`, `S3BucketName`, `S3BucketArn`, `VpcId`,
`EcsClusterName`, `EcsServiceName`, `AccessLogsBucketName`, `RdsEndpoint`,
`RdsPort`, `RdsSecretArn`.

All are exported (`export_name`) so other stacks can import them. **No secret
value is ever placed in an output** — only the ARN of the secret.

</details>

---

## Repository layout

```
basic_webapp_cdk/
├── app.py                          # CDK entry point
├── cdk.json                        # CDK config + all environment settings
├── pyproject.toml                  # ruff + pytest configuration
├── requirements.txt                # runtime dependencies
├── requirements-dev.txt            # test / lint / security-scan dependencies
├── package.json                    # pins the CDK CLI (a Node.js program)
│
├── basic_webapp/
│   ├── config.py                   # typed, validated configuration loader
│   ├── webapp_stack.py             # the stack: wiring + outputs only
│   ├── nag_suppressions.py         # justified security-scan exceptions
│   └── constructs/
│       ├── network.py              # VPC, subnets, NAT, flow logs, endpoints
│       ├── storage.py              # S3 assets bucket + access-log bucket
│       ├── compute.py              # ALB, ECS cluster, ASG, task, service
│       └── database.py             # RDS PostgreSQL
│
├── scripts/
│   └── generate_presigned_url.py   # runnable pre-signed URL demo
│
├── tests/
│   ├── conftest.py                 # synthesis fixtures
│   └── test_requirements.py        # 54 assertions, one per requirement
│
└── .github/workflows/
    ├── ci.yml                      # lint, test, synth, cdk-nag on every push
    └── deploy.yml                  # OIDC deploy: dev automatic, prod gated
```

The stack file is deliberately thin — it only wires layers together and
declares outputs. Each construct owns one slice of the architecture, which keeps
files short, independently testable, and reusable in another stack.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.9+** | 3.12 recommended |
| **Node.js 20+** | the CDK CLI is a Node program even for a Python app |
| **AWS CLI v2** | configured with `aws configure` or SSO |
| **An AWS account** | with permission to create VPC, ECS, EC2, ELB, S3, RDS, IAM |

Verify your credentials work before starting:

```bash
aws sts get-caller-identity
```

---

## Deploy it

### 1. Install dependencies

```bash
git clone <your-repo-url>
cd basic_webapp_cdk

# Python dependencies, in an isolated virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

# The CDK CLI, pinned by package.json
npm install
```

All `cdk` commands below use `npx cdk`, which runs the pinned local CLI. If you
prefer a global install (`npm install -g aws-cdk`) you can drop the `npx`.

### 2. Bootstrap the account (once per account + region)

CDK needs a small support stack — an S3 bucket and roles it uses to stage
assets and execute deployments.

```bash
npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
```

### 3. Preview what will be created

```bash
npx cdk synth              # writes the CloudFormation template to cdk.out/
npx cdk diff               # shows the change against what is deployed
```

### 4. Deploy

```bash
npx cdk deploy             # dev settings from cdk.json
```

Takes roughly **15–20 minutes**; the RDS instance is the slow part. Skip it for
a much faster first deploy:

```bash
npx cdk deploy -c env=dev -c enable_database=false
```

<details>
<summary><strong>Deploy with HTTPS (recommended)</strong></summary>

Request a free certificate in AWS Certificate Manager **in the same region as
the load balancer**, validate it, then pass its ARN:

```bash
aws acm request-certificate \
  --domain-name app.example.com \
  --validation-method DNS

npx cdk deploy -c certificate_arn=arn:aws:acm:us-east-1:123456789012:certificate/abc-123
```

The stack then serves HTTPS on 443 with the AWS-recommended TLS policy and
permanently redirects port 80 to it. Without a certificate it falls back to
plain HTTP, which is fine for a demo and not fine for real user data.

</details>

<details>
<summary><strong>Deploy the production configuration</strong></summary>

```bash
npx cdk deploy -c env=prod -c certificate_arn=<your-acm-arn>
```

Production differs in ways that matter and is validated at synth time — the
app refuses to build a prod stack with a single NAT Gateway or with
`retain_data_on_delete` turned off:

| | dev | prod |
|---|---|---|
| Availability Zones | 2 | 3 |
| NAT Gateways | 1 | 3 (one per AZ) |
| Instance type | t3.micro | m6i.large |
| Data retention on stack delete | destroy | **retain / snapshot** |
| RDS Multi-AZ | no | **yes** |
| VPC interface endpoints | no | yes |
| Log retention | 7 days | 90 days |
| Stack termination protection | off | **on** |

</details>

---

## After deployment

```bash
# All outputs at a glance
aws cloudformation describe-stacks --stack-name WebAppStack-dev \
  --query "Stacks[0].Outputs" --output table

# Open the application
open "$(aws cloudformation describe-stacks --stack-name WebAppStack-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" \
  --output text)"
```

Useful operational commands:

```bash
# Are the tasks healthy?
aws ecs describe-services --cluster basic-webapp-dev-cluster \
  --services basic-webapp-dev-service \
  --query "services[0].{running:runningCount,desired:desiredCount,events:events[:3]}"

# Application logs
aws logs tail /aws/ecs/basic-webapp-dev-service --follow

# Shell into a running container (no SSH, fully audited)
aws ecs execute-command --cluster basic-webapp-dev-cluster \
  --task <task-id> --container web --interactive --command "/bin/sh"

# Database credentials
aws secretsmanager get-secret-value \
  --secret-id basic-webapp-dev-db-credentials \
  --query SecretString --output text
```

---

## Pre-signed URLs explained

The brief asks for a **private** bucket that still **serves content**. Those
two goals are reconciled with pre-signed URLs.

**How it works.** A pre-signed URL is a normal S3 URL plus a cryptographic
signature in the query string. The signature covers the bucket, the object key,
the HTTP method and an expiry time, and is produced using the caller's AWS
credentials. When S3 receives the request it recreates the signature; if it
matches and has not expired, the request is served *as if the signer had made
it*.

```
Browser ──── GET /logo.png ────► ECS task (holds the task role)
                                      │  signs a URL locally, no API call
Browser ◄─── 302 or JSON ─────────────┘
   │
   └──── GET https://bucket.s3.amazonaws.com/logo.png?X-Amz-Signature=... ──► S3
                                                                              │
                                                                        200 OK ┘
```

**Why this design is safe.** The bucket blocks all public access, so the same
URL *without* its signature returns `403 AccessDenied`. Signing is offline
cryptography — no API call, so it is fast and free. And a signed URL can never
grant more than the signer already has: revoke `s3:GetObject` from the task role
and every outstanding URL stops working.

**Try it:**

```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name WebAppStack-dev \
  --query "Stacks[0].Outputs[?OutputKey=='S3BucketName'].OutputValue" --output text)

# Upload a file and get a 15-minute download link
python scripts/generate_presigned_url.py --bucket "$BUCKET" \
  --key assets/logo.png --upload ./logo.png

# Confirm the object is NOT public
curl -s -o /dev/null -w '%{http_code}\n' "https://$BUCKET.s3.amazonaws.com/assets/logo.png"
# → 403
```

The stack supports this by granting the ECS **task role** `s3:GetObject` on this
bucket (the permission needed to sign a download URL) and `s3:PutObject`
(deliberately *not* `s3:DeleteObject`), and by configuring CORS so a browser
`fetch()` of a signed URL succeeds.

**Operational note.** Treat the URL itself as a credential: keep the expiry
short, do not log it, and do not cache it in a CDN keyed only on path.

---

## Configuration reference

Every tunable value lives in `cdk.json` under `context.environments.<env>`, and
any of them can be overridden on the command line with `-c key=value`.

| Key | dev | prod | What it controls |
|---|---|---|---|
| `vpc_cidr` | `10.20.0.0/16` | `10.30.0.0/16` | VPC address range |
| `max_azs` | 2 | 3 | Availability Zones spanned |
| `nat_gateways` | 1 | 3 | NAT Gateways (cost vs. AZ resilience) |
| `instance_type` | `t3.micro` | `m6i.large` | EC2 container host size |
| `asg_min_capacity` / `asg_max_capacity` | 2 / 4 | 2 / 4 | EC2 host bounds |
| `task_desired_count` | 2 | 2 | Container copies at steady state |
| `task_min_count` / `task_max_count` | 2 / 4 | 2 / 4 | Task auto-scaling bounds |
| `container_image` | `amazon/amazon-ecs-sample` | same | Image to run |
| `container_port` | 80 | 80 | Port inside the container |
| `enable_database` | `true` | `true` | Create RDS at all |
| `db_instance_type` | `t3.micro` | `t3.small` | RDS size |
| `db_multi_az` | `false` | `true` | Standby in a second AZ |
| `db_performance_insights` | `false` | `true` | Query-level RDS metrics |
| `enable_private_link_endpoints` | `false` | `true` | VPC interface endpoints |
| `retain_data_on_delete` | `false` | `true` | Protect data on stack delete |
| `log_retention_days` | 7 | 90 | CloudWatch retention |
| `certificate_arn` | — | — | ACM certificate; enables HTTPS |

Invalid combinations are rejected at synth time, in under a second, rather than
failing 20 minutes into a CloudFormation rollback. For example: fewer than two
AZs, `min > max`, a desired task count outside its own bounds, zero NAT
Gateways, or a production environment with a single NAT Gateway or unprotected
data.

---

## Tests and security scanning

```bash
pytest -v                  # 54 tests, ~1 second, no AWS account needed
ruff check .               # lint
ruff format --check .      # formatting
npx cdk synth -c nag=true  # cdk-nag security scan
```

The tests synthesize the stack in memory and assert on the resulting
CloudFormation template. They are named after the requirements they protect, so
a failure tells you which promise the infrastructure just stopped keeping — for
example:

- `test_container_hosts_launch_only_in_private_subnets`
- `test_alb_security_group_allows_only_http_and_https`
- `test_no_ssh_access_anywhere`
- `test_assets_bucket_blocks_all_public_access`
- `test_no_output_leaks_a_secret_value`
- `test_database_lives_in_isolated_subnets`
- `test_production_settings_protect_data`

**cdk-nag** checks the synthesized template against the AWS Solutions
best-practice rule pack and fails the build on any finding. Every accepted
exception is listed with a written justification in
`basic_webapp/nag_suppressions.py` — so the exceptions are auditable and
anything new shows up loudly.

---

## CI/CD

Two GitHub Actions workflows, both fully commented.

**`ci.yml`** — on every push and pull request: lint, format check, unit tests,
`cdk synth` for both environments, and the cdk-nag scan. Needs no AWS
credentials, so it is safe on forks. The synthesized templates are uploaded as
an artifact so a reviewer can see exactly what infrastructure a PR changes.

**`deploy.yml`** — re-runs CI, then deploys. Highlights:

- **No stored AWS keys.** GitHub authenticates with OIDC and assumes a
  deployment role for short-lived credentials. Setup instructions, including the
  crucial trust-policy condition that restricts which repository and branch may
  assume the role, are in the file header.
- `cdk diff` is printed before every deploy, as a reviewable record.
- Production is gated behind a GitHub Environment with required reviewers.
- A concurrency group prevents two overlapping deployments to one stack.
- A smoke test polls the ALB afterwards, because "CloudFormation succeeded"
  does not mean "the application works".

---

## Cost

Rough `us-east-1` figures, on-demand, excluding data transfer and free tiers.

| Resource | dev | prod |
|---|---|---|
| NAT Gateway(s) | ~$33 | ~$99 |
| EC2 hosts | 2 × t3.micro ≈ $15 | 2 × m6i.large ≈ $140 |
| Application Load Balancer | ~$17 | ~$17 |
| RDS PostgreSQL | t3.micro ≈ $13 | t3.small Multi-AZ ≈ $55 |
| S3, CloudWatch, Secrets Manager | ~$3 | ~$15 |
| VPC interface endpoints | — | ~$70 |
| **Approximate total** | **~$80/month** | **~$400/month** |

Cheapest way to explore the stack:

```bash
npx cdk deploy -c enable_database=false   # saves ~$13/month and ~10 minutes
```

Every resource is tagged with `Project`, `Environment`, `Owner` and `ManagedBy`,
so Cost Explorer can break the bill down per environment.

---

## Tearing it down

```bash
npx cdk destroy
```

In **dev** this removes everything, including the S3 buckets (a CDK custom
resource empties them first) and the database.

In **prod** it will refuse, by design. Termination protection is on, the
database has deletion protection, and the buckets use a `RETAIN` policy. To
delete a production stack you must deliberately disable those first:

```bash
aws cloudformation update-termination-protection \
  --stack-name WebAppStack-prod --no-enable-termination-protection
# then set retain_data_on_delete=false in cdk.json, deploy, and destroy
```

Retained buckets survive the stack and must be emptied and deleted by hand —
which is exactly the point.

---

## Design decisions and trade-offs

<details>
<summary><strong>Why ECS on EC2 rather than Fargate?</strong></summary>

The brief asks for instances "part of an Auto Scaling group with a minimum of 2
and a maximum of 4", and an IAM role attached to the instances. Fargate has no
instances and no Auto Scaling Group, so EC2 capacity is the correct reading.

For a greenfield service Fargate is usually the better default: no hosts to
patch, no capacity to plan. The construct boundaries here make that swap a
change confined to `compute.py`.

</details>

<details>
<summary><strong>Why bridge networking instead of <code>awsvpc</code>?</strong></summary>

With bridge mode the container's port 80 maps to a random high port on the host,
so many task copies share one host and the ALB tracks those ports
automatically.

`awsvpc` gives every task its own network interface and its own security group —
nicer isolation — but small instance types support only one or two interfaces,
which would cap us at roughly one task per host and break the point of task
auto scaling. The host security group provides the network isolation instead.

</details>

<details>
<summary><strong>Why three subnet tiers when the brief asked for two?</strong></summary>

Public and private satisfy the brief. Adding an isolated tier for the database
costs nothing and removes an entire class of exposure: those subnets have no
route to a NAT Gateway, so there is no path to the internet in either
direction, even if a security group were later misconfigured.

</details>

<details>
<summary><strong>Why is there no SSH access or bastion host?</strong></summary>

Operators reach hosts through AWS Systems Manager Session Manager, and
containers through `aws ecs execute-command`. Both are audited in CloudTrail,
need no inbound port, and need no SSH keys to distribute or rotate. There is a
test (`test_no_ssh_access_anywhere`) that fails if port 22 is ever opened.

</details>

<details>
<summary><strong>Why SSE-S3 rather than a customer-managed KMS key?</strong></summary>

A customer-managed key gives you an audit trail of every decrypt and the
ability to revoke access instantly. The cost is that every consumer of a
pre-signed URL also needs `kms:Decrypt`, which complicates the browser flow, and
the ALB log-delivery service cannot write to a KMS-encrypted bucket at all.

SSE-S3 is free, always on, and the right default here. For regulated data,
switch the assets bucket to `BucketEncryption.KMS` with your own key — a
one-line change in `storage.py`.

</details>

<details>
<summary><strong>Why is managed termination protection off in dev?</strong></summary>

It stops the Auto Scaling Group from terminating a host that still has tasks on
it — correct for production, and enabled there. But it also blocks
`cdk destroy` until every task has drained, which makes throw-away environments
tedious to clean up. Hence: on in production, off elsewhere.

</details>

<details>
<summary><strong>Known gaps in a real production deployment</strong></summary>

Deliberately out of scope for this exercise, and what you would add next:

- **Route 53 + a real domain name** in front of the ALB, instead of the raw AWS
  DNS name.
- **WAF** on the load balancer for rate limiting and the managed rule sets.
- **CloudWatch alarms and dashboards** — the metrics and the SNS topic are
  already there; the alarms are not.
- **A real container image** in ECR with an image scan gate, replacing
  `amazon/amazon-ecs-sample`.
- **A dedicated `/healthz` endpoint** rather than health-checking `/`.
- **CDK Pipelines or blue/green deployments** via CodeDeploy for zero-downtime
  releases with automatic rollback on alarm.
- **AWS Backup** with cross-region copies for the database.

</details>

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `This stack uses assets, so the toolkit stack must be deployed` | The account/region is not bootstrapped. Run `npx cdk bootstrap`. |
| `Need to perform AWS calls for account ..., but no credentials configured` | Expired credentials. Re-run `aws sso login` or `aws configure`. |
| Targets stuck **unhealthy** in the ALB | The container is not answering on `/`. Check `aws logs tail /aws/ecs/basic-webapp-dev-service`, and confirm `container_port` matches what the image listens on. |
| ECS events say **"no container instance met all of its requirements"** | Not enough host capacity, or the task's memory reservation exceeds what a `t3.micro` can offer. Raise `asg_max_capacity` or use a larger `instance_type`. |
| Tasks never start; hosts show as not registered | The hosts cannot reach ECS. Confirm the NAT Gateway exists and the private subnets route `0.0.0.0/0` through it. |
| `Cannot delete bucket: bucket not empty` | Only happens with `retain_data_on_delete=true`. Empty the bucket manually — that protection is intentional. |
| RDS deploy fails on **Performance Insights** | Not supported on the smallest instance classes. Set `db_performance_insights: false` or use a larger `db_instance_type`. |
| `cdk deploy` fails immediately with a `ValueError` | A configuration guard-rail caught an invalid combination. The message says exactly which one; see [Configuration reference](#configuration-reference). |

---

## License

Provided as a technical exercise.
