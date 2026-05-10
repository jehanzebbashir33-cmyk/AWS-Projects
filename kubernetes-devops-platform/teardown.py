"""
teardown.py — Tear down the Kubernetes DevOps Platform.

Workflow:
  1. Update kubeconfig (so kubectl can reach the cluster).
  2. Delete all Kubernetes resources in reverse manifest order.
  3. Delete the CloudFormation stack and wait for it to finish.
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

from config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_SECRET_ACCESS_KEY,
    EKS_CLUSTER_NAME,
    STACK_NAME,
)

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
# Paths
# ---------------------------------------------------------------------------
K8S_DIR = Path(__file__).parent / "k8s"

# Delete in reverse order so dependent objects are removed first.
MANIFEST_ORDER_REVERSED = [
    "hpa.yaml",
    "ingress.yaml",
    "service.yaml",
    "deployment.yaml",
    "rbac.yaml",
    "namespace.yaml",
]

# ---------------------------------------------------------------------------
# AWS session
# ---------------------------------------------------------------------------
def get_session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )


# ---------------------------------------------------------------------------
# kubeconfig
# ---------------------------------------------------------------------------
def update_kubeconfig(cluster_name: str, region: str) -> bool:
    """Return True on success, False if cluster is already gone."""
    log.info("Updating kubeconfig for cluster '%s'.", cluster_name)
    cmd = [
        "aws", "eks", "update-kubeconfig",
        "--name", cluster_name,
        "--region", region,
    ]
    env = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
        "AWS_DEFAULT_REGION": region,
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        log.warning(
            "Could not update kubeconfig (cluster may already be deleted):\n%s",
            result.stderr.strip(),
        )
        return False
    log.info(result.stdout.strip())
    return True


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------
def kubectl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["kubectl", *args]
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        log.info(result.stdout.strip())
    if result.stderr:
        log.warning(result.stderr.strip())
    return result


def delete_k8s_resources() -> None:
    log.info("Deleting Kubernetes resources (reverse manifest order).")
    for manifest in MANIFEST_ORDER_REVERSED:
        manifest_path = K8S_DIR / manifest
        if not manifest_path.exists():
            log.warning("Manifest not found, skipping: %s", manifest_path)
            continue
        result = kubectl("delete", "-f", str(manifest_path), "--ignore-not-found=true")
        if result.returncode != 0:
            log.warning("Non-zero exit deleting '%s' — continuing.", manifest_path)

    # Give the cluster a moment to finish cleanup before CloudFormation tears
    # down the underlying infrastructure (ENIs, load balancers, etc.).
    log.info("Pausing 60 s for Kubernetes finalizers to complete...")
    time.sleep(60)


# ---------------------------------------------------------------------------
# CloudFormation
# ---------------------------------------------------------------------------
def stack_exists(cf_client, stack_name: str) -> bool:
    try:
        stacks = cf_client.describe_stacks(StackName=stack_name)["Stacks"]
        # A stack in DELETE_COMPLETE state is effectively gone.
        return stacks[0]["StackStatus"] != "DELETE_COMPLETE"
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def delete_stack(cf_client) -> None:
    if not stack_exists(cf_client, STACK_NAME):
        log.info("Stack '%s' does not exist or is already deleted.", STACK_NAME)
        return

    log.info("Deleting CloudFormation stack '%s'.", STACK_NAME)
    cf_client.delete_stack(StackName=STACK_NAME)

    log.info("Waiting for stack deletion to complete (this may take 10-15 minutes)...")
    waiter = cf_client.get_waiter("stack_delete_complete")
    try:
        waiter.wait(
            StackName=STACK_NAME,
            WaiterConfig={"Delay": 30, "MaxAttempts": 60},
        )
    except WaiterError:
        events = cf_client.describe_stack_events(StackName=STACK_NAME)["StackEvents"]
        failed = [
            e for e in events
            if "FAILED" in e.get("ResourceStatus", "")
        ]
        for event in failed:
            log.error(
                "FAILED resource: %s — %s",
                event.get("LogicalResourceId"),
                event.get("ResourceStatusReason"),
            )
        log.error("Stack deletion failed. Manual cleanup may be required.")
        sys.exit(1)

    log.info("Stack '%s' deleted successfully.", STACK_NAME)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== Kubernetes DevOps Platform — Teardown ===")

    session = get_session()
    cf_client = session.client("cloudformation")

    # Step 1 — Try to configure kubectl so we can clean up k8s objects first.
    kubeconfig_ok = update_kubeconfig(EKS_CLUSTER_NAME, AWS_REGION)

    # Step 2 — Delete Kubernetes resources (best-effort; cluster may be gone).
    if kubeconfig_ok:
        delete_k8s_resources()
    else:
        log.warning("Skipping Kubernetes resource deletion — cluster unreachable.")

    # Step 3 — Delete the CloudFormation stack.
    delete_stack(cf_client)

    log.info("=== Teardown complete. ===")


if __name__ == "__main__":
    main()
