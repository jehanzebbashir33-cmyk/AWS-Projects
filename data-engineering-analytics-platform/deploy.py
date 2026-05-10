"""
deploy.py
---------
Deploys the Data Engineering & Analytics Platform CloudFormation stack,
optionally triggers the initial Glue crawler run, and can send a sample
event to Kinesis to exercise the full pipeline end-to-end.

Usage:
    python deploy.py                          # deploy only
    python deploy.py --test-event            # deploy + send sample Kinesis event
    python deploy.py --bucket-suffix my-001  # override the default BucketSuffix
"""

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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

TEMPLATE_PATH = Path(__file__).parent / "template.yaml"


# ---------------------------------------------------------------------------
# CloudFormation helpers
# ---------------------------------------------------------------------------

def load_template() -> str:
    """Read the CloudFormation template from disk."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def stack_exists(cf_client, stack_name: str) -> bool:
    """Return True if the named stack exists (in any non-deleted state)."""
    try:
        resp = cf_client.describe_stacks(StackName=stack_name)
        stacks = resp.get("Stacks", [])
        return bool(stacks)
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def get_stack_outputs(cf_client, stack_name: str) -> dict:
    """Return a dict mapping OutputKey → OutputValue for the given stack."""
    resp = cf_client.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def deploy_stack(cf_client, stack_name: str, template_body: str, bucket_suffix: str):
    """Create or update the CloudFormation stack and wait for completion."""
    parameters = [
        {
            "ParameterKey": "BucketSuffix",
            "ParameterValue": bucket_suffix,
        }
    ]
    capabilities = ["CAPABILITY_NAMED_IAM"]

    if stack_exists(cf_client, stack_name):
        log.info("Stack '%s' already exists — attempting update …", stack_name)
        try:
            cf_client.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=capabilities,
            )
            waiter = cf_client.get_waiter("stack_update_complete")
            action = "update"
        except ClientError as exc:
            if "No updates are to be performed" in str(exc):
                log.info("Stack is already up to date — nothing to do.")
                return
            raise
    else:
        log.info("Creating stack '%s' …", stack_name)
        cf_client.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=parameters,
            Capabilities=capabilities,
            EnableTerminationProtection=False,
            Tags=[
                {"Key": "Project", "Value": "DataEngineeringPlatform"},
                {"Key": "ManagedBy", "Value": "CloudFormation"},
            ],
        )
        waiter = cf_client.get_waiter("stack_create_complete")
        action = "create"

    log.info("Waiting for stack %s to complete (this may take several minutes) …", action)
    try:
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
    except WaiterError:
        log.error("Stack %s failed. Check the CloudFormation console for details.", action)
        sys.exit(1)

    log.info("Stack %s completed successfully.", action)


# ---------------------------------------------------------------------------
# Glue crawler helpers
# ---------------------------------------------------------------------------

def start_crawler(glue_client, crawler_name: str):
    """Start the Glue crawler and wait until it finishes."""
    log.info("Starting Glue crawler '%s' …", crawler_name)
    try:
        glue_client.start_crawler(Name=crawler_name)
    except ClientError as exc:
        if "CrawlerRunningException" in str(exc):
            log.warning("Crawler '%s' is already running — skipping trigger.", crawler_name)
            return
        raise

    # Poll until the crawler is no longer RUNNING
    while True:
        resp = glue_client.get_crawler(Name=crawler_name)
        state = resp["Crawler"]["State"]
        log.info("Crawler state: %s", state)
        if state not in ("RUNNING", "STOPPING"):
            break
        time.sleep(20)

    last_crawl = resp["Crawler"].get("LastCrawl", {})
    status = last_crawl.get("Status", "UNKNOWN")
    if status == "SUCCEEDED":
        log.info("Crawler finished successfully.")
    else:
        log.warning("Crawler finished with status: %s  — %s", status, last_crawl.get("ErrorMessage", ""))


# ---------------------------------------------------------------------------
# Kinesis test-event helper
# ---------------------------------------------------------------------------

def send_sample_event(kinesis_client, stream_name: str):
    """Put a single synthetic purchase event on the Kinesis stream."""
    event = {
        "event_id": str(uuid.uuid4()),
        "user_id": f"user_{uuid.uuid4().hex[:8]}",
        "event_type": "purchase",
        "product_id": "prod_demo_001",
        "product_category": "electronics",
        "amount": 149.99,
        "currency": "GBP",
        "session_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "source": "deploy-test",
            "platform": "web",
            "country_code": "GB",
        },
    }
    partition_key = event["user_id"]
    payload = json.dumps(event)

    log.info("Sending sample event to Kinesis stream '%s' …", stream_name)
    resp = kinesis_client.put_record(
        StreamName=stream_name,
        Data=payload.encode("utf-8"),
        PartitionKey=partition_key,
    )
    log.info(
        "Event delivered — ShardId: %s  SequenceNumber: %s",
        resp["ShardId"],
        resp["SequenceNumber"],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Deploy the Data Engineering Platform stack.")
    parser.add_argument(
        "--bucket-suffix",
        default="de-platform-001",
        help="Unique suffix for S3 bucket names (default: de-platform-001).",
    )
    parser.add_argument(
        "--test-event",
        action="store_true",
        help="After deployment, send a sample event to Kinesis to test the pipeline.",
    )
    parser.add_argument(
        "--skip-crawler",
        action="store_true",
        help="Skip the initial Glue crawler run after deployment.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    cf_client = SESSION.client("cloudformation")
    glue_client = SESSION.client("glue")
    kinesis_client = SESSION.client("kinesis")

    # 1. Load and validate the template
    log.info("Loading CloudFormation template from %s", TEMPLATE_PATH)
    template_body = load_template()
    cf_client.validate_template(TemplateBody=template_body)
    log.info("Template validation passed.")

    # 2. Deploy
    deploy_stack(cf_client, config.STACK_NAME, template_body, args.bucket_suffix)

    # 3. Show stack outputs
    outputs = get_stack_outputs(cf_client, config.STACK_NAME)
    log.info("Stack outputs:")
    for key, value in outputs.items():
        log.info("  %-40s %s", key, value)

    # 4. Initial Glue crawler run
    if not args.skip_crawler:
        start_crawler(glue_client, "de-crawler-raw-data")
    else:
        log.info("Skipping initial Glue crawler run (--skip-crawler flag set).")

    # 5. Optional Kinesis test event
    if args.test_event:
        send_sample_event(kinesis_client, config.KINESIS_STREAM_NAME)
    else:
        log.info(
            "Tip: re-run with --test-event to send a sample purchase event through the pipeline."
        )

    log.info("Deployment complete.")


if __name__ == "__main__":
    main()
