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
logger = logging.getLogger(__name__)


def build_client():
    return boto3.client(
        "cloudformation",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def stack_exists(client, stack_name: str) -> bool:
    try:
        response = client.describe_stacks(StackName=stack_name)
        stacks = response.get("Stacks", [])
        return bool(stacks)
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def delete_stack(client, stack_name: str) -> None:
    logger.info("Initiating deletion of stack '%s'", stack_name)
    client.delete_stack(StackName=stack_name)
    logger.info("Waiting for stack deletion to complete …")
    waiter = client.get_waiter("stack_delete_complete")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 120},
    )
    logger.info("Stack '%s' deleted successfully.", stack_name)


def main() -> None:
    client = build_client()
    stack_name = config.STACK_NAME

    try:
        if not stack_exists(client, stack_name):
            logger.warning("Stack '%s' does not exist — nothing to delete.", stack_name)
            sys.exit(0)

        confirm = input(
            f"Are you sure you want to delete stack '{stack_name}' in region "
            f"'{config.AWS_REGION}'? This action cannot be undone. [yes/no]: "
        ).strip().lower()

        if confirm != "yes":
            logger.info("Teardown cancelled by user.")
            sys.exit(0)

        delete_stack(client, stack_name)
    except ClientError as exc:
        logger.error("AWS error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
