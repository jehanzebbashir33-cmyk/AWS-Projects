import logging
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "template.yaml"
CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]


def get_cfn_client():
    return boto3.client(
        "cloudformation",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def stack_exists(client, stack_name: str) -> bool:
    try:
        resp = client.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
        if status == "ROLLBACK_COMPLETE":
            log.warning("Stack is in ROLLBACK_COMPLETE — deleting before re-deploy.")
            client.delete_stack(StackName=stack_name)
            waiter = client.get_waiter("stack_delete_complete")
            waiter.wait(StackName=stack_name)
            return False
        return True
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def deploy(notification_email: str = "ops-team@example.com", environment: str = "dev") -> None:
    client = get_cfn_client()
    template_body = TEMPLATE_PATH.read_text()
    stack_name = config.STACK_NAME
    parameters = [
        {"ParameterKey": "NotificationEmail", "ParameterValue": notification_email},
        {"ParameterKey": "Environment", "ParameterValue": environment},
    ]

    if stack_exists(client, stack_name):
        log.info("Stack '%s' exists — attempting update.", stack_name)
        try:
            client.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=CAPABILITIES,
            )
            waiter = client.get_waiter("stack_update_complete")
            log.info("Waiting for stack update to complete…")
            waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 60})
            log.info("Stack updated successfully.")
        except ClientError as exc:
            if "No updates are to be performed" in str(exc):
                log.info("No changes detected — stack is up to date.")
            else:
                raise
    else:
        log.info("Stack '%s' does not exist — creating.", stack_name)
        client.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=parameters,
            Capabilities=CAPABILITIES,
            EnableTerminationProtection=False,
        )
        waiter = client.get_waiter("stack_create_complete")
        log.info("Waiting for stack creation to complete…")
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
        log.info("Stack created successfully.")

    print_outputs(client, stack_name)


def print_outputs(client, stack_name: str) -> None:
    resp = client.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    if not outputs:
        log.info("No outputs to display.")
        return

    log.info("Stack outputs:")
    col = max(len(o["OutputKey"]) for o in outputs)
    for output in outputs:
        log.info("  %-*s  %s", col, output["OutputKey"], output["OutputValue"])


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "ops-team@example.com"
    env = sys.argv[2] if len(sys.argv) > 2 else "dev"
    deploy(notification_email=email, environment=env)
