"""
teardown.py — Tears down all DR stacks in reverse dependency order:
  1. Global stack (CloudFront + Route 53)
  2. S3 replication configuration
  3. Secondary stack (eu-central-1)
  4. Primary stack (eu-north-1)
"""

import logging
import sys
import boto3
from botocore.exceptions import ClientError

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def _cfn_client(region: str):
    return boto3.client(
        "cloudformation",
        region_name=region,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def _s3_client(region: str):
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def stack_exists(cfn, stack_name: str) -> bool:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        return len(resp["Stacks"]) > 0
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def get_stack_outputs(cfn, stack_name: str) -> dict:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        raw = resp["Stacks"][0].get("Outputs", [])
        return {o["OutputKey"]: o["OutputValue"] for o in raw}
    except ClientError:
        return {}


def delete_stack(cfn, stack_name: str):
    if not stack_exists(cfn, stack_name):
        log.info("Stack %s does not exist — skipping.", stack_name)
        return

    log.info("Deleting stack %s…", stack_name)
    cfn.delete_stack(StackName=stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 120},
    )
    log.info("Stack %s deleted.", stack_name)


def remove_s3_replication(primary_bucket: str):
    if not primary_bucket:
        log.warning("Primary bucket name unknown — skipping replication removal.")
        return
    log.info("Removing S3 replication configuration from %s.", primary_bucket)
    s3 = _s3_client(config.PRIMARY_REGION)
    try:
        s3.delete_bucket_replication(Bucket=primary_bucket)
        log.info("S3 replication configuration removed.")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchReplicationConfiguration", "NoSuchBucket"):
            log.info("No replication configuration found — skipping.")
        else:
            log.warning("Could not remove replication config: %s", exc)


def empty_bucket(bucket_name: str, region: str):
    if not bucket_name:
        return
    log.info("Emptying bucket %s before stack deletion.", bucket_name)
    s3 = _s3_client(region)
    paginator = s3.get_paginator("list_object_versions")
    try:
        for page in paginator.paginate(Bucket=bucket_name):
            objects_to_delete = []
            for version in page.get("Versions", []):
                objects_to_delete.append(
                    {"Key": version["Key"], "VersionId": version["VersionId"]}
                )
            for marker in page.get("DeleteMarkers", []):
                objects_to_delete.append(
                    {"Key": marker["Key"], "VersionId": marker["VersionId"]}
                )
            if objects_to_delete:
                s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": objects_to_delete, "Quiet": True},
                )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchBucket":
            log.info("Bucket %s does not exist — skipping.", bucket_name)
        else:
            log.warning("Error emptying bucket %s: %s", bucket_name, exc)


def main():
    # ── Step 1: Global stack ─────────────────────────────────────────────────
    log.info("=== Step 1: Deleting global stack ===")
    global_cfn = _cfn_client("us-east-1")
    delete_stack(global_cfn, config.GLOBAL_STACK_NAME)

    # ── Step 2: Remove S3 replication ────────────────────────────────────────
    log.info("=== Step 2: Removing S3 cross-region replication ===")
    primary_cfn = _cfn_client(config.PRIMARY_REGION)
    primary_outputs = get_stack_outputs(primary_cfn, config.PRIMARY_STACK_NAME)
    primary_bucket = primary_outputs.get("S3BucketName", "")
    remove_s3_replication(primary_bucket)

    # ── Step 3: Secondary stack ──────────────────────────────────────────────
    log.info("=== Step 3: Deleting secondary stack ===")
    secondary_cfn = _cfn_client(config.SECONDARY_REGION)
    secondary_outputs = get_stack_outputs(secondary_cfn, config.SECONDARY_STACK_NAME)
    secondary_bucket = secondary_outputs.get("S3BucketName", "")

    empty_bucket(secondary_bucket, config.SECONDARY_REGION)
    delete_stack(secondary_cfn, config.SECONDARY_STACK_NAME)

    # ── Step 4: Primary stack ─────────────────────────────────────────────────
    log.info("=== Step 4: Deleting primary stack ===")
    empty_bucket(primary_bucket, config.PRIMARY_REGION)
    delete_stack(primary_cfn, config.PRIMARY_STACK_NAME)

    log.info("=== Teardown complete ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Teardown failed: %s", exc)
        sys.exit(1)
