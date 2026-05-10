"""
teardown.py
-----------
Safely tears down the Data Engineering & Analytics Platform:

  1. Discovers all S3 buckets owned by the stack via its outputs / resource list.
  2. Empties each bucket (deletes all object versions + delete markers).
  3. Deletes the CloudFormation stack and waits for completion.

Usage:
    python teardown.py              # dry-run prompt before deletion
    python teardown.py --confirm    # skip interactive prompt
"""

import argparse
import logging
import sys

import boto3
from botocore.exceptions import ClientError, WaiterError

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS session
# ---------------------------------------------------------------------------
SESSION = boto3.Session(
    aws_access_key_id=config.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    region_name=config.AWS_REGION,
)


# ---------------------------------------------------------------------------
# S3 bucket-emptying helpers
# ---------------------------------------------------------------------------

def list_bucket_names_from_stack(cf_client, stack_name: str) -> list[str]:
    """
    Return the names of every S3 bucket resource in the given stack.
    Falls back to an empty list if the stack does not exist.
    """
    buckets: list[str] = []
    paginator = cf_client.get_paginator("list_stack_resources")
    try:
        for page in paginator.paginate(StackName=stack_name):
            for resource in page.get("StackResourceSummaries", []):
                if resource["ResourceType"] == "AWS::S3::Bucket":
                    physical_id = resource.get("PhysicalResourceId")
                    if physical_id:
                        buckets.append(physical_id)
    except ClientError as exc:
        if "does not exist" in str(exc):
            log.warning("Stack '%s' does not exist — nothing to tear down.", stack_name)
            return []
        raise
    return buckets


def delete_all_objects(s3_client, bucket_name: str):
    """
    Delete every object version and delete marker in *bucket_name*.
    Required before CloudFormation can remove the bucket itself.
    """
    log.info("Emptying bucket '%s' …", bucket_name)
    paginator = s3_client.get_paginator("list_object_versions")

    try:
        pages = paginator.paginate(Bucket=bucket_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchBucket", "AccessDenied"):
            log.warning("Bucket '%s' not accessible or already gone — skipping.", bucket_name)
            return
        raise

    total_deleted = 0
    for page in pages:
        objects_to_delete = []

        for version in page.get("Versions", []):
            objects_to_delete.append(
                {"Key": version["Key"], "VersionId": version["VersionId"]}
            )
        for marker in page.get("DeleteMarkers", []):
            objects_to_delete.append(
                {"Key": marker["Key"], "VersionId": marker["VersionId"]}
            )

        if not objects_to_delete:
            continue

        # delete_objects accepts a maximum of 1000 keys per call
        for chunk_start in range(0, len(objects_to_delete), 1000):
            chunk = objects_to_delete[chunk_start : chunk_start + 1000]
            resp = s3_client.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": chunk, "Quiet": True},
            )
            errors = resp.get("Errors", [])
            if errors:
                for err in errors:
                    log.error(
                        "Failed to delete s3://%s/%s (VersionId=%s): %s",
                        bucket_name,
                        err.get("Key"),
                        err.get("VersionId"),
                        err.get("Message"),
                    )
            total_deleted += len(chunk) - len(errors)

    log.info("Deleted %d object version(s) from '%s'.", total_deleted, bucket_name)


def empty_all_stack_buckets(s3_client, cf_client, stack_name: str):
    """Discover and empty every S3 bucket in the stack."""
    bucket_names = list_bucket_names_from_stack(cf_client, stack_name)
    if not bucket_names:
        log.info("No S3 buckets found in stack '%s'.", stack_name)
        return

    log.info("Found %d bucket(s) to empty: %s", len(bucket_names), bucket_names)
    for bucket_name in bucket_names:
        delete_all_objects(s3_client, bucket_name)


# ---------------------------------------------------------------------------
# CloudFormation deletion helper
# ---------------------------------------------------------------------------

def delete_stack(cf_client, stack_name: str):
    """Delete the CloudFormation stack and wait until it is gone."""
    log.info("Deleting CloudFormation stack '%s' …", stack_name)
    try:
        cf_client.delete_stack(StackName=stack_name)
    except ClientError as exc:
        if "does not exist" in str(exc):
            log.info("Stack '%s' does not exist — nothing to delete.", stack_name)
            return
        raise

    log.info("Waiting for stack deletion to complete …")
    waiter = cf_client.get_waiter("stack_delete_complete")
    try:
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
    except WaiterError:
        log.error(
            "Stack deletion failed or timed out. Check the CloudFormation console for details."
        )
        sys.exit(1)

    log.info("Stack '%s' deleted successfully.", stack_name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Tear down the Data Engineering Platform stack."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Skip the interactive confirmation prompt and proceed with teardown.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.confirm:
        print(
            f"\nWARNING: This will permanently delete the stack '{config.STACK_NAME}' "
            f"and ALL data in its S3 buckets in region '{config.AWS_REGION}'.\n"
        )
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            log.info("Teardown cancelled.")
            sys.exit(0)

    cf_client = SESSION.client("cloudformation")
    s3_client = SESSION.client("s3")

    # Step 1: Empty all S3 buckets owned by the stack
    empty_all_stack_buckets(s3_client, cf_client, config.STACK_NAME)

    # Step 2: Delete the stack
    delete_stack(cf_client, config.STACK_NAME)

    log.info("Teardown complete.")


if __name__ == "__main__":
    main()
