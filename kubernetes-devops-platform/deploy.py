"""
deploy.py — Deploy the Kubernetes DevOps Platform to AWS.

Workflow:
  1. Upload and deploy the CloudFormation stack (VPC, EKS cluster, node group).
  2. Wait for the stack to reach CREATE_COMPLETE / UPDATE_COMPLETE.
  3. Update the local kubeconfig to point at the new cluster.
  4. Apply all Kubernetes manifests from the k8s/ directory.
  5. Wait for the application Deployment to finish rolling out.
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
TEMPLATE_PATH = Path(__file__).parent / "template.yaml"
K8S_DIR = Path(__file__).parent / "k8s"

# Kubernetes manifests are applied in this order so dependencies are satisfied.
MANIFEST_ORDER = [
    "namespace.yaml",
    "rbac.yaml",
    "deployment.yaml",
    "service.yaml",
    "ingress.yaml",
    "hpa.yaml",
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
# CloudFormation helpers
# ---------------------------------------------------------------------------
def stack_exists(cf_client, stack_name: str) -> bool:
    try:
        cf_client.describe_stacks(StackName=stack_name)
        return True
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def deploy_stack(cf_client) -> None:
    template_body = TEMPLATE_PATH.read_text()

    common_kwargs = dict(
        StackName=STACK_NAME,
        TemplateBody=template_body,
        Parameters=[
            {"ParameterKey": "NodeInstanceType",    "ParameterValue": "t3.medium"},
            {"ParameterKey": "NodeGroupMinSize",     "ParameterValue": "2"},
            {"ParameterKey": "NodeGroupMaxSize",     "ParameterValue": "6"},
            {"ParameterKey": "NodeGroupDesiredSize", "ParameterValue": "2"},
        ],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[
            {"Key": "Project", "Value": "kubernetes-devops-platform"},
            {"Key": "ManagedBy", "Value": "deploy.py"},
        ],
    )

    if stack_exists(cf_client, STACK_NAME):
        log.info("Stack '%s' already exists — updating.", STACK_NAME)
        try:
            cf_client.update_stack(**common_kwargs)
            waiter_name = "stack_update_complete"
        except ClientError as exc:
            if "No updates are to be performed" in str(exc):
                log.info("Stack is already up to date — no changes needed.")
                return
            raise
    else:
        log.info("Creating stack '%s'.", STACK_NAME)
        cf_client.create_stack(**common_kwargs)
        waiter_name = "stack_create_complete"

    log.info("Waiting for CloudFormation stack operation to complete (this may take 15-20 minutes)...")
    waiter = cf_client.get_waiter(waiter_name)
    try:
        waiter.wait(
            StackName=STACK_NAME,
            WaiterConfig={"Delay": 30, "MaxAttempts": 60},
        )
    except WaiterError:
        # Surface the stack events so the caller can diagnose failures.
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
        log.error("Stack operation failed. See events above.")
        sys.exit(1)

    log.info("Stack operation completed successfully.")


def get_stack_output(cf_client, output_key: str) -> str:
    response = cf_client.describe_stacks(StackName=STACK_NAME)
    outputs = response["Stacks"][0].get("Outputs", [])
    for output in outputs:
        if output["OutputKey"] == output_key:
            return output["OutputValue"]
    raise KeyError(f"Output '{output_key}' not found in stack '{STACK_NAME}'.")


# ---------------------------------------------------------------------------
# kubeconfig
# ---------------------------------------------------------------------------
def update_kubeconfig(cluster_name: str, region: str) -> None:
    log.info("Updating kubeconfig for cluster '%s' in '%s'.", cluster_name, region)
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
        log.error("update-kubeconfig failed:\n%s", result.stderr)
        sys.exit(1)
    log.info(result.stdout.strip())


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------
def kubectl(*args: str) -> subprocess.CompletedProcess:
    """Run a kubectl command, streaming output to the logger."""
    cmd = ["kubectl", *args]
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        log.info(result.stdout.strip())
    if result.stderr:
        # kubectl writes informational messages to stderr too.
        log.warning(result.stderr.strip())
    return result


def apply_manifests() -> None:
    log.info("Applying Kubernetes manifests from '%s'.", K8S_DIR)
    for manifest in MANIFEST_ORDER:
        manifest_path = K8S_DIR / manifest
        if not manifest_path.exists():
            log.warning("Manifest not found, skipping: %s", manifest_path)
            continue
        result = kubectl("apply", "-f", str(manifest_path))
        if result.returncode != 0:
            log.error("Failed to apply '%s'.", manifest_path)
            sys.exit(1)
    log.info("All manifests applied.")


def wait_for_rollout(deployment: str = "nginx-deployment", namespace: str = "devops") -> None:
    log.info("Waiting for rollout of deployment '%s' in namespace '%s'.", deployment, namespace)
    # kubectl rollout status blocks until ready or times out (--timeout flag).
    result = kubectl(
        "rollout", "status",
        f"deployment/{deployment}",
        "-n", namespace,
        "--timeout=10m",
    )
    if result.returncode != 0:
        log.error("Deployment rollout did not complete successfully.")
        sys.exit(1)
    log.info("Deployment '%s' is fully available.", deployment)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== Kubernetes DevOps Platform — Deploy ===")

    session = get_session()
    cf_client = session.client("cloudformation")

    # Step 1 — Deploy CloudFormation stack.
    deploy_stack(cf_client)

    # Step 2 — Retrieve cluster name from stack outputs (or fall back to config).
    try:
        cluster_name = get_stack_output(cf_client, "ClusterName")
    except KeyError:
        cluster_name = EKS_CLUSTER_NAME
        log.warning("ClusterName output not found; using config value '%s'.", cluster_name)

    # Step 3 — Configure kubectl.
    update_kubeconfig(cluster_name, AWS_REGION)

    # Brief pause to let the EKS API server become fully reachable after
    # the kubeconfig is written.
    log.info("Pausing 30 s for the EKS API server to stabilise...")
    time.sleep(30)

    # Step 4 — Apply Kubernetes manifests.
    apply_manifests()

    # Step 5 — Wait for the application to roll out.
    wait_for_rollout()

    log.info("=== Deployment complete. ===")
    log.info("Cluster endpoint: %s", get_stack_output(cf_client, "ClusterEndpoint"))


if __name__ == "__main__":
    main()
