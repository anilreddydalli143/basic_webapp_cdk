"""Storage layer: the private S3 bucket for static assets, plus a log bucket.

HOW "PRIVATE BUCKET + PRE-SIGNED URL" WORKS (in plain English)
-------------------------------------------------------------
A public website bucket lets anybody fetch any object. We do not want that, so
this bucket blocks *all* public access. Instead, the application hands out
**pre-signed URLs**:

  1. A browser asks the web app for, say, ``logo.png``.
  2. The web app (which holds the ECS *task role*) asks the AWS SDK to sign a
     URL for that object with a short expiry — for example 15 minutes.
     No network call to AWS is needed; signing is pure local cryptography using
     the role's temporary credentials.
  3. The app returns that long URL. The browser downloads straight from S3.
  4. After 15 minutes the signature expires and the link is dead.

Two important consequences:

  * The bucket never has to be public, and no object ACLs are involved.
  * A signed URL can only ever grant permissions the *signer* already has. That
    is why the task role is granted read access below — the signature is
    checked against the role's own policy when the URL is used.

See ``scripts/generate_presigned_url.py`` for a runnable example.
"""

from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct

from ..config import EnvironmentConfig


class StaticAssets(Construct):
    """Private, encrypted S3 bucket for the web application's static assets.

    Attributes:
        bucket: the assets bucket.
        log_bucket: destination for S3 server access logs and ALB access logs.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, config: EnvironmentConfig
    ) -> None:
        super().__init__(scope, construct_id)
        self._config = config

        # Deleting a stack should never be able to delete customer data in
        # production, so the removal policy is driven by configuration.
        #   RETAIN  -> CloudFormation leaves the bucket behind (prod).
        #   DESTROY -> CloudFormation deletes it (dev/test only).
        removal_policy = (
            RemovalPolicy.RETAIN
            if config.retain_data_on_delete
            else RemovalPolicy.DESTROY
        )
        # S3 refuses to delete a bucket that still contains objects, so for dev
        # we let CDK attach a small custom resource that empties it first.
        auto_delete = not config.retain_data_on_delete

        # ------------------------------------------------------------------
        # 1. Access-log bucket
        # ------------------------------------------------------------------
        # Both S3 (object-level access) and the ALB (HTTP request logs) write
        # here. Keeping logs in a *separate* bucket is deliberate: if logs went
        # into the assets bucket, every log write would itself be logged, which
        # loops forever and grows without bound.
        self.log_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            bucket_name=None,  # let CloudFormation generate a unique name
            # SSE-S3 (AES-256, keys managed by AWS) rather than KMS: the ALB
            # log delivery service cannot write to a bucket encrypted with a
            # customer-managed KMS key.
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,  # deny any request that arrives over plain HTTP
            # ACLs disabled entirely; access is controlled by bucket policy
            # only. This is the AWS-recommended modern default.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-logs",
                    enabled=True,
                    # Logs are only useful for a while, and storing them for
                    # ever is a slow, silent cost leak.
                    expiration=Duration.days(max(config.log_retention_days, 30)),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )

        # ------------------------------------------------------------------
        # 2. Static assets bucket
        # ------------------------------------------------------------------
        self.bucket = s3.Bucket(
            self,
            "AssetsBucket",
            # --- Privacy ---------------------------------------------------
            # BLOCK_ALL switches on all four "block public access" settings.
            # Even if somebody later attaches a public bucket policy or a
            # public ACL by mistake, S3 will ignore it. This is the single most
            # important guard-rail against accidental data exposure, and it is
            # what makes pre-signed URLs the only way to read objects.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # --- Encryption ------------------------------------------------
            # SSE-S3 encrypts every object at rest at no extra cost.
            # (A customer-managed KMS key would give you an audit trail and
            # revocable access, but then every pre-signed URL consumer also
            # needs kms:Decrypt, which complicates the browser flow.)
            encryption=s3.BucketEncryption.S3_MANAGED,
            bucket_key_enabled=True,  # cuts per-request encryption overhead
            # --- Encryption in transit -------------------------------------
            # enforce_ssl adds a bucket policy that denies any request where
            # aws:SecureTransport is false, i.e. plain HTTP is rejected.
            enforce_ssl=True,
            minimum_tls_version=1.2,  # reject legacy TLS 1.0/1.1 clients
            # --- Data protection -------------------------------------------
            # Versioning keeps the previous copy of an object when it is
            # overwritten or deleted, which makes an accidental `aws s3 rm` or
            # a bad deploy recoverable.
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
            # --- Audit -----------------------------------------------------
            server_access_logs_bucket=self.log_bucket,
            server_access_logs_prefix="s3-access-logs/",
            # --- Housekeeping ----------------------------------------------
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="tidy-up-old-versions-and-failed-uploads",
                    enabled=True,
                    # Old versions are a safety net, not an archive: keep them
                    # cheaply for a month, then drop them.
                    noncurrent_version_transitions=[
                        s3.NoncurrentVersionTransition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(30),
                        )
                    ],
                    noncurrent_version_expiration=Duration.days(90),
                    # A multipart upload that was interrupted leaves invisible
                    # parts behind that you still pay for. Clean them up.
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            # --- Browser support -------------------------------------------
            # A pre-signed URL is fetched by the browser directly from S3, so
            # S3 (not the ALB) answers the CORS pre-flight. Without this, a
            # JavaScript fetch() of a signed URL fails in the console with an
            # opaque CORS error.
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    # TODO(deployment): narrow this to your real site origin,
                    # e.g. ["https://www.example.com"]. "*" is acceptable here
                    # only because objects are already protected by signature.
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    exposed_headers=["ETag", "Content-Length", "Content-Type"],
                    max_age=3000,
                )
            ],
        )

    # ---------------------------------------------------------------------- #
    # Public helpers used by the stack to wire up least-privilege access     #
    # ---------------------------------------------------------------------- #
    def grant_asset_read(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to read assets *and* therefore to sign read URLs.

        Signing is offline, so there is no separate "presign" IAM action: the
        permission you need to sign a GET URL is simply ``s3:GetObject``.
        """
        return self.bucket.grant_read(grantee)

    def grant_asset_write(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to upload assets (e.g. user avatar uploads).

        ``grant_put`` is deliberately narrower than ``grant_read_write``: it
        adds ``s3:PutObject`` but *not* ``s3:DeleteObject``, so a compromised
        or buggy application cannot wipe the asset library. Least privilege in
        practice means granting the verbs you actually use, and nothing else.
        """
        return self.bucket.grant_put(grantee)
