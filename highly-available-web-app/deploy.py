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

TEMPLATE_FILE = "template.yaml"

STACK_PARAMETERS = [
    {"ParameterKey": "EnvironmentName", "ParameterValue": "production"},
    {"ParameterKey": "InstanceType", "ParameterValue": "t3.medium"},
    {"ParameterKey": "DBInstanceClass", "ParameterValue": "db.t3.medium"},
    {"ParameterKey": "DBName", "ParameterValue": "appdb"},
    {"ParameterKey": "DBUsername", "ParameterValue": "admin"},
    {"ParameterKey": "DBPassword", "ParameterValue": "Ch@ngeMe2024!"},
    {"ParameterKey": "ASGMinSize", "ParameterValue": "2"},
    {"ParameterKey": "ASGMaxSize", "ParameterValue": "6"},
    {"ParameterKey": "ASGDesiredCapacity", "ParameterValue": "2"},
    {
        "ParameterKey": "CertificateArn",
        "ParameterValue": "arn:aws:acm:us-east-1:123456789012:certificate/EXAMPLE-CERT-ARN-PLACEHOLDER",
    },
    {"ParameterKey": "DomainName", "ParameterValue": "app.example.com"},
    {"ParameterKey": "HostedZoneId", "ParameterValue": "Z1EXAMPLE00000"},
    {"ParameterKey": "ElastiCacheNodeType", "ParameterValue": "cache.t3.micro"},
]

STACK_CAPABILITIES = ["CAPABILITY_NAMED_IAM"]


def build_client():
    return boto3.client(
        "cloudformation",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def read_template(path: str) -> str:
    logger.info("Reading CloudFormation template from %s", path)
    with open(path, "r") as fh:
        return fh.read()


def stack_exists(client, stack_name: str) -> bool:
    try:
        response = client.describe_stacks(StackName=stack_name)
        stacks = response.get("Stacks", [])
        if stacks:
            status = stacks[0]["StackStatus"]
            if status == "REVIEW_IN_PROGRESS":
                return False
            return True
        return False
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def create_stack(client, stack_name: str, template_body: str) -> None:
    logger.info("Creating stack '%s'", stack_name)
    client.create_stack(
        StackName=stack_name,
        TemplateBody=template_body,
        Parameters=STACK_PARAMETERS,
        Capabilities=STACK_CAPABILITIES,
        EnableTerminationProtection=False,
        OnFailure="ROLLBACK",
        Tags=[
            {"Key": "ManagedBy", "Value": "CloudFormation"},
            {"Key": "Project", "Value": "highly-available-web-app"},
        ],
    )
    logger.info("Waiting for stack creation to complete …")
    waiter = client.get_waiter("stack_create_complete")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 120},
    )
    logger.info("Stack '%s' created successfully.", stack_name)


def update_stack(client, stack_name: str, template_body: str) -> None:
    logger.info("Updating stack '%s'", stack_name)
    try:
        client.update_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=STACK_PARAMETERS,
            Capabilities=STACK_CAPABILITIES,
        )
    except ClientError as exc:
        if "No updates are to be performed" in str(exc):
            logger.info("No changes detected — stack is already up to date.")
            return
        raise
    logger.info("Waiting for stack update to complete …")
    waiter = client.get_waiter("stack_update_complete")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 120},
    )
    logger.info("Stack '%s' updated successfully.", stack_name)


def print_outputs(client, stack_name: str) -> None:
    response = client.describe_stacks(StackName=stack_name)
    outputs = response["Stacks"][0].get("Outputs", [])
    if not outputs:
        logger.info("Stack has no outputs.")
        return
    logger.info("Stack outputs:")
    for output in outputs:
        logger.info(
            "  %-40s = %s  (%s)",
            output["OutputKey"],
            output["OutputValue"],
            output.get("Description", ""),
        )


def main() -> None:
    client = build_client()
    template_body = read_template(TEMPLATE_FILE)
    stack_name = config.STACK_NAME

    try:
        if stack_exists(client, stack_name):
            update_stack(client, stack_name, template_body)
        else:
            create_stack(client, stack_name, template_body)
        print_outputs(client, stack_name)
    except ClientError as exc:
        logger.error("AWS error: %s", exc)
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Template file '%s' not found.", TEMPLATE_FILE)
        sys.exit(1)


if __name__ == "__main__":
    main()
