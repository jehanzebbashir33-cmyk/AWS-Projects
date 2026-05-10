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


def get_cfn_client():
    return boto3.client(
        "cloudformation",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def teardown(stack_name: str = config.STACK_NAME) -> None:
    client = get_cfn_client()

    try:
        resp = client.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
        log.info("Stack '%s' found with status: %s", stack_name, status)
    except ClientError as exc:
        if "does not exist" in str(exc):
            log.info("Stack '%s' does not exist — nothing to delete.", stack_name)
            return
        raise

    confirm = input(f"Delete stack '{stack_name}' in region '{config.AWS_REGION}'? [y/N] ").strip().lower()
    if confirm != "y":
        log.info("Teardown cancelled.")
        return

    log.info("Initiating stack deletion for '%s'…", stack_name)
    client.delete_stack(StackName=stack_name)

    waiter = client.get_waiter("stack_delete_complete")
    log.info("Waiting for deletion to complete…")
    try:
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 60})
        log.info("Stack '%s' deleted successfully.", stack_name)
    except Exception:
        log.error("Stack deletion may have failed — check the CloudFormation console for details.")
        events = client.describe_stack_events(StackName=stack_name)["StackEvents"]
        failed = [
            e for e in events
            if "FAILED" in e.get("ResourceStatus", "")
        ]
        for event in failed[:5]:
            log.error(
                "  %s | %s | %s",
                event.get("ResourceType"),
                event.get("ResourceStatus"),
                event.get("ResourceStatusReason", ""),
            )
        raise


if __name__ == "__main__":
    stack = sys.argv[1] if len(sys.argv) > 1 else config.STACK_NAME
    teardown(stack)
