#!/usr/bin/env python3
"""Demonstrate the pre-signed URL mechanism against the deployed assets bucket.

WHY THIS SCRIPT EXISTS
----------------------
The exercise asks for a private S3 bucket that "serves content using a
pre-signed URL mechanism". The CDK stack builds the *bucket* and grants the
right IAM permissions, but "pre-signed URL" is not a thing you can see in a
CloudFormation template — it is a runtime behaviour. This script makes that
behaviour concrete and testable, and doubles as a copy-paste reference for the
application code that will do the same thing in production.

WHAT A PRE-SIGNED URL ACTUALLY IS
---------------------------------
A normal S3 URL plus a cryptographic signature in the query string. The
signature covers the bucket, the key, the HTTP method and an expiry time, and
is produced with the caller's AWS credentials. When S3 receives the request it
recreates the signature; if it matches and has not expired, the request is
allowed *as if it had been made by the signer*.

Consequences worth understanding:

  * Signing happens entirely offline — no API call, so it is fast and free.
  * A signed URL can never grant more than the signer is allowed to do. If the
    task role loses s3:GetObject, previously issued URLs stop working.
  * Anyone holding the URL can use it until it expires. Keep expiry short and
    treat the URL itself as a credential (do not log it, do not cache it in a
    CDN keyed only by path).

USAGE
-----
    # Find the bucket name from the stack outputs:
    BUCKET=$(aws cloudformation describe-stacks \
        --stack-name WebAppStack-dev \
        --query "Stacks[0].Outputs[?OutputKey=='S3BucketName'].OutputValue" \
        --output text)

    # Upload a file and print a 15-minute download link:
    python scripts/generate_presigned_url.py --bucket "$BUCKET" \
        --key assets/logo.png --upload ./logo.png

    # Just sign an existing object:
    python scripts/generate_presigned_url.py --bucket "$BUCKET" --key assets/logo.png

    # Sign an *upload* URL instead, so a browser can PUT straight to S3:
    python scripts/generate_presigned_url.py --bucket "$BUCKET" \
        --key uploads/photo.jpg --method put
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - developer convenience
    sys.exit(
        "boto3 is required. Install it with:\n    pip install -r requirements-dev.txt"
    )

# 15 minutes. Short enough that a leaked link is not a lasting problem, long
# enough for a slow connection to finish a download.
DEFAULT_EXPIRY_SECONDS = 900

# S3 caps pre-signed URL validity at 7 days for SigV4.
MAX_EXPIRY_SECONDS = 7 * 24 * 60 * 60


def build_s3_client(region: str | None, profile: str | None):
    """Create an S3 client configured for SigV4.

    Signature Version 4 is required for pre-signed URLs to work in every
    region, and for SSE-KMS objects. Being explicit avoids a class of
    "signature mismatch" errors in newer regions.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(
        "s3",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def upload(client, bucket: str, key: str, path: Path) -> None:
    """Upload a local file to the bucket before signing a link for it."""
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    # ServerSideEncryption is redundant here (the bucket enforces AES256 by
    # default) but stating it makes the intent explicit and keeps working if
    # the bucket default ever changes.
    client.upload_file(
        Filename=str(path),
        Bucket=bucket,
        Key=key,
        ExtraArgs={"ServerSideEncryption": "AES256"},
    )
    print(f"Uploaded {path} -> s3://{bucket}/{key}")


def presign(client, bucket: str, key: str, method: str, expires_in: int) -> str:
    """Return a signed URL for a GET (download) or PUT (upload) of one object."""
    operation = "get_object" if method == "get" else "put_object"
    return client.generate_presigned_url(
        ClientMethod=operation,
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
        HttpMethod=method.upper(),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Bucket name; take it from the S3BucketName stack output.",
    )
    parser.add_argument(
        "--key",
        required=True,
        help="Object key inside the bucket, e.g. assets/logo.png",
    )
    parser.add_argument(
        "--method",
        choices=("get", "put"),
        default="get",
        help="get = signed download link (default); put = signed upload link.",
    )
    parser.add_argument(
        "--expires-in",
        type=int,
        default=DEFAULT_EXPIRY_SECONDS,
        help=f"Validity in seconds (default {DEFAULT_EXPIRY_SECONDS}, max {MAX_EXPIRY_SECONDS}).",
    )
    parser.add_argument(
        "--upload",
        type=Path,
        metavar="FILE",
        help="Optional local file to upload to --key before signing.",
    )
    parser.add_argument("--region", help="AWS region (defaults to your CLI config).")
    parser.add_argument("--profile", help="AWS named profile to use.")

    args = parser.parse_args(argv)

    if not 1 <= args.expires_in <= MAX_EXPIRY_SECONDS:
        parser.error(
            f"--expires-in must be between 1 and {MAX_EXPIRY_SECONDS} seconds "
            "(S3's SigV4 limit is 7 days)."
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = build_s3_client(args.region, args.profile)

    try:
        if args.upload is not None:
            upload(client, args.bucket, args.key, args.upload)

        url = presign(client, args.bucket, args.key, args.method, args.expires_in)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError) as error:
        # The overwhelmingly common causes are: no credentials, credentials
        # without s3:GetObject on this bucket, or a typo in the bucket name.
        print(f"AWS error: {error}", file=sys.stderr)
        return 2

    minutes = args.expires_in // 60
    print(f"\nPre-signed {args.method.upper()} URL (valid {minutes} minutes):\n")
    print(url)
    print(
        "\nTry it with:\n"
        + (
            f'  curl -sSfL "{url}" -o downloaded-object'
            if args.method == "get"
            else f'  curl -sSf -X PUT --upload-file ./local-file "{url}"'
        )
    )
    print(
        "\nNote: the bucket blocks all public access, so the *same* URL without "
        "its signature returns 403 AccessDenied."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
