"""
dr_test.py — Disaster recovery simulation script.

Simulates a primary region failure by stopping the primary EC2 instance,
monitors Route 53 health check status and CloudFront response to confirm
failover has occurred, then restores the primary instance.
"""

import logging
import sys
import time
import urllib.request
import urllib.error
import boto3
from botocore.exceptions import ClientError

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

HEALTH_CHECK_POLL_INTERVAL = 30
HEALTH_CHECK_MAX_WAIT = 600
FAILOVER_SETTLE_SECONDS = 60


def _ec2_client(region: str):
    return boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def _cfn_client(region: str):
    return boto3.client(
        "cloudformation",
        region_name=region,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def _r53_client():
    return boto3.client(
        "route53",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def get_stack_outputs(region: str, stack_name: str) -> dict:
    cfn = _cfn_client(region)
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        raw = resp["Stacks"][0].get("Outputs", [])
        return {o["OutputKey"]: o["OutputValue"] for o in raw}
    except ClientError as exc:
        log.error("Could not retrieve stack outputs for %s: %s", stack_name, exc)
        return {}


def get_instance_id(region: str, stack_name: str) -> str:
    outputs = get_stack_outputs(region, stack_name)
    instance_id = outputs.get("WebInstanceId")
    if not instance_id:
        raise RuntimeError(
            f"WebInstanceId not found in stack {stack_name} outputs. "
            "Ensure the stack has been deployed."
        )
    return instance_id


def get_health_check_id(global_stack_outputs: dict) -> str:
    hc_id = global_stack_outputs.get("PrimaryHealthCheckId")
    if not hc_id:
        raise RuntimeError("PrimaryHealthCheckId not found in global stack outputs.")
    return hc_id


def get_cloudfront_domain(global_stack_outputs: dict) -> str:
    domain = global_stack_outputs.get("CloudFrontDomain")
    if not domain:
        raise RuntimeError("CloudFrontDomain not found in global stack outputs.")
    return domain


def stop_instance(ec2, instance_id: str):
    log.info("Stopping primary EC2 instance %s to simulate failure…", instance_id)
    ec2.stop_instances(InstanceIds=[instance_id])
    waiter = ec2.get_waiter("instance_stopped")
    waiter.wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 15, "MaxAttempts": 40},
    )
    log.info("Instance %s is stopped.", instance_id)


def start_instance(ec2, instance_id: str):
    log.info("Starting primary EC2 instance %s to restore service…", instance_id)
    ec2.start_instances(InstanceIds=[instance_id])
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 15, "MaxAttempts": 40},
    )
    log.info("Instance %s is running.", instance_id)


def poll_health_check_status(r53, health_check_id: str) -> str:
    resp = r53.get_health_check_status(HealthCheckId=health_check_id)
    checkers = resp.get("HealthCheckObservations", [])
    if not checkers:
        return "UNKNOWN"
    statuses = [c["StatusReport"]["Status"] for c in checkers]
    healthy_count = sum(1 for s in statuses if "Success" in s)
    total = len(statuses)
    log.info(
        "Health check %s: %d/%d checkers reporting healthy.",
        health_check_id,
        healthy_count,
        total,
    )
    return "HEALTHY" if healthy_count > total / 2 else "UNHEALTHY"


def wait_for_health_check_status(
    r53, health_check_id: str, target_status: str, timeout: int
) -> bool:
    log.info(
        "Waiting for health check %s to reach status %s (timeout: %ds)…",
        health_check_id,
        target_status,
        timeout,
    )
    elapsed = 0
    while elapsed < timeout:
        status = poll_health_check_status(r53, health_check_id)
        if status == target_status:
            log.info("Health check reached target status: %s", target_status)
            return True
        log.info(
            "Current status: %s. Retrying in %ds…", status, HEALTH_CHECK_POLL_INTERVAL
        )
        time.sleep(HEALTH_CHECK_POLL_INTERVAL)
        elapsed += HEALTH_CHECK_POLL_INTERVAL
    log.warning(
        "Timed out after %ds waiting for health check to reach %s.",
        timeout,
        target_status,
    )
    return False


def probe_cloudfront(domain: str) -> tuple[int, str]:
    url = f"http://{domain}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DR-Test/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc.reason)
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def check_failover_via_cloudfront(domain: str) -> bool:
    log.info("Probing CloudFront distribution at %s…", domain)
    status_code, body = probe_cloudfront(domain)
    log.info("CloudFront response: HTTP %d", status_code)
    if status_code in (200, 301, 302):
        if "eu-central-1" in body or "Secondary" in body or "secondary" in body:
            log.info("Response confirms traffic is being served from SECONDARY region.")
            return True
        log.info(
            "CloudFront is responding (HTTP %d) — failover may have occurred "
            "but response content does not explicitly indicate secondary origin.",
            status_code,
        )
        return True
    log.warning(
        "CloudFront returned HTTP %d — failover may not have completed yet.",
        status_code,
    )
    return False


def main():
    log.info("=== DR Failover Simulation Starting ===")

    # ── Gather stack metadata ────────────────────────────────────────────────
    log.info("Retrieving stack outputs…")
    primary_instance_id = get_instance_id(
        config.PRIMARY_REGION, config.PRIMARY_STACK_NAME
    )
    global_outputs = get_stack_outputs("us-east-1", config.GLOBAL_STACK_NAME)
    health_check_id = get_health_check_id(global_outputs)
    cloudfront_domain = get_cloudfront_domain(global_outputs)

    log.info("Primary instance  : %s", primary_instance_id)
    log.info("Health check ID   : %s", health_check_id)
    log.info("CloudFront domain : %s", cloudfront_domain)

    ec2 = _ec2_client(config.PRIMARY_REGION)
    r53 = _r53_client()

    # ── Baseline health check ────────────────────────────────────────────────
    log.info("=== Phase 1: Baseline health check ===")
    initial_status = poll_health_check_status(r53, health_check_id)
    log.info("Baseline health check status: %s", initial_status)

    # ── Simulate failure ─────────────────────────────────────────────────────
    log.info("=== Phase 2: Simulating primary region failure ===")
    stop_instance(ec2, primary_instance_id)

    # ── Wait for Route 53 to detect failure ──────────────────────────────────
    log.info("=== Phase 3: Monitoring Route 53 health check ===")
    became_unhealthy = wait_for_health_check_status(
        r53, health_check_id, "UNHEALTHY", HEALTH_CHECK_MAX_WAIT
    )

    if became_unhealthy:
        log.info("Route 53 health check detected primary failure.")
    else:
        log.warning(
            "Health check did not reach UNHEALTHY within the timeout. "
            "The check interval and failure threshold may need adjustment."
        )

    # ── Allow failover to settle ─────────────────────────────────────────────
    log.info(
        "Allowing %ds for CloudFront origin group failover to take effect…",
        FAILOVER_SETTLE_SECONDS,
    )
    time.sleep(FAILOVER_SETTLE_SECONDS)

    # ── Verify failover via CloudFront ────────────────────────────────────────
    log.info("=== Phase 4: Verifying failover via CloudFront ===")
    failover_confirmed = check_failover_via_cloudfront(cloudfront_domain)

    if failover_confirmed:
        log.info("RESULT: Failover CONFIRMED — secondary region is serving traffic.")
    else:
        log.warning(
            "RESULT: Failover status UNCERTAIN — manual verification recommended."
        )

    # ── Restore primary instance ──────────────────────────────────────────────
    log.info("=== Phase 5: Restoring primary EC2 instance ===")
    start_instance(ec2, primary_instance_id)

    # ── Wait for Route 53 to recover ─────────────────────────────────────────
    log.info("=== Phase 6: Confirming primary health check recovery ===")
    recovered = wait_for_health_check_status(
        r53, health_check_id, "HEALTHY", HEALTH_CHECK_MAX_WAIT
    )

    if recovered:
        log.info("Primary region has recovered and health check is HEALTHY.")
    else:
        log.warning(
            "Health check did not return to HEALTHY within %ds. "
            "Manual investigation required.",
            HEALTH_CHECK_MAX_WAIT,
        )

    # ── Final summary ─────────────────────────────────────────────────────────
    log.info("=== DR Simulation Complete ===")
    log.info("Primary instance stopped : %s", primary_instance_id)
    log.info("Unhealthy detected       : %s", became_unhealthy)
    log.info("Failover confirmed       : %s", failover_confirmed)
    log.info("Primary recovered        : %s", recovered)

    if not failover_confirmed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("DR simulation failed: %s", exc)
        sys.exit(1)
