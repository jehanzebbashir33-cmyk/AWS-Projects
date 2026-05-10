"""
deploy.py — Orchestrates the full multi-region DR deployment across
eu-north-1 (primary), eu-central-1 (secondary), and global resources.
"""

import logging
import os
import sys
import time
import boto3
from botocore.exceptions import ClientError

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))

DEPLOY_PARAMS = {
    "KeyName": os.environ.get("KEY_NAME", "my-key-pair"),
    "DBPassword": os.environ.get("DB_PASSWORD", "ChangeMe123!"),
    "DBUsername": os.environ.get("DB_USERNAME", "admin"),
    "HostedZoneId": os.environ.get("HOSTED_ZONE_ID", "ZXXXXXXXXXXXX"),
}


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


def load_template(filename: str) -> str:
    path = os.path.join(TEMPLATE_DIR, filename)
    with open(path, "r") as fh:
        return fh.read()


def stack_exists(cfn, stack_name: str) -> bool:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        return len(resp["Stacks"]) > 0
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def deploy_stack(cfn, stack_name: str, template_body: str, parameters: list) -> dict:
    kwargs = dict(
        StackName=stack_name,
        TemplateBody=template_body,
        Parameters=parameters,
        Capabilities=["CAPABILITY_NAMED_IAM"],
        OnFailure="ROLLBACK",
    )

    if stack_exists(cfn, stack_name):
        log.info("Stack %s already exists — updating.", stack_name)
        try:
            cfn.update_stack(**{k: v for k, v in kwargs.items() if k != "OnFailure"})
            waiter = cfn.get_waiter("stack_update_complete")
        except ClientError as exc:
            if "No updates are to be performed" in str(exc):
                log.info("Stack %s is already up-to-date.", stack_name)
                return get_stack_outputs(cfn, stack_name)
            raise
    else:
        log.info("Creating stack %s.", stack_name)
        cfn.create_stack(**kwargs)
        waiter = cfn.get_waiter("stack_create_complete")

    log.info("Waiting for stack %s to stabilise…", stack_name)
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 120},
    )
    log.info("Stack %s is ready.", stack_name)
    return get_stack_outputs(cfn, stack_name)


def get_stack_outputs(cfn, stack_name: str) -> dict:
    resp = cfn.describe_stacks(StackName=stack_name)
    raw = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in raw}


def params(*pairs) -> list:
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in pairs]


def setup_s3_replication(
    primary_bucket: str,
    secondary_bucket: str,
    replication_role_arn: str,
):
    log.info(
        "Configuring S3 cross-region replication: %s → %s",
        primary_bucket,
        secondary_bucket,
    )
    s3 = _s3_client(config.PRIMARY_REGION)

    secondary_s3 = _s3_client(config.SECONDARY_REGION)
    try:
        secondary_s3.get_bucket_versioning(Bucket=secondary_bucket)
    except ClientError:
        pass

    s3.put_bucket_replication(
        Bucket=primary_bucket,
        ReplicationConfiguration={
            "Role": replication_role_arn,
            "Rules": [
                {
                    "ID": "replicate-all-to-secondary",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Destination": {
                        "Bucket": f"arn:aws:s3:::{secondary_bucket}",
                        "StorageClass": "STANDARD",
                    },
                    "DeleteMarkerReplication": {"Status": "Enabled"},
                }
            ],
        },
    )
    log.info("S3 cross-region replication configured.")


def main():
    # ── Step 1: Primary stack ────────────────────────────────────────────────
    log.info("=== Step 1: Deploying primary stack in %s ===", config.PRIMARY_REGION)
    primary_cfn = _cfn_client(config.PRIMARY_REGION)
    primary_template = load_template("primary_template.yaml")

    primary_outputs = deploy_stack(
        primary_cfn,
        config.PRIMARY_STACK_NAME,
        primary_template,
        params(
            ("KeyName", DEPLOY_PARAMS["KeyName"]),
            ("DBPassword", DEPLOY_PARAMS["DBPassword"]),
            ("DBUsername", DEPLOY_PARAMS["DBUsername"]),
        ),
    )

    primary_alb_dns = primary_outputs["ALBDnsName"]
    primary_rds_arn = primary_outputs["RDSInstanceArn"]
    primary_s3_bucket = primary_outputs["S3BucketName"]
    replication_role_arn = primary_outputs["S3ReplicationRoleArn"]

    log.info("Primary ALB: %s", primary_alb_dns)
    log.info("Primary RDS ARN: %s", primary_rds_arn)
    log.info("Primary S3 bucket: %s", primary_s3_bucket)

    # ── Step 2: Secondary stack ──────────────────────────────────────────────
    log.info(
        "=== Step 2: Deploying secondary stack in %s ===", config.SECONDARY_REGION
    )
    secondary_cfn = _cfn_client(config.SECONDARY_REGION)
    secondary_template = load_template("secondary_template.yaml")

    secondary_outputs = deploy_stack(
        secondary_cfn,
        config.SECONDARY_STACK_NAME,
        secondary_template,
        params(
            ("KeyName", DEPLOY_PARAMS["KeyName"]),
            ("SourceDBInstanceArn", primary_rds_arn),
        ),
    )

    secondary_alb_dns = secondary_outputs["ALBDnsName"]
    secondary_s3_bucket = secondary_outputs["S3BucketName"]

    log.info("Secondary ALB: %s", secondary_alb_dns)
    log.info("Secondary S3 bucket: %s", secondary_s3_bucket)

    # ── Step 3: S3 cross-region replication ──────────────────────────────────
    log.info("=== Step 3: Setting up S3 cross-region replication ===")
    setup_s3_replication(primary_s3_bucket, secondary_s3_bucket, replication_role_arn)

    # ── Step 4: Global stack ─────────────────────────────────────────────────
    log.info("=== Step 4: Deploying global stack (CloudFront + Route 53) ===")
    global_cfn = _cfn_client("us-east-1")
    global_template = load_template("global_template.yaml")

    global_outputs = deploy_stack(
        global_cfn,
        config.GLOBAL_STACK_NAME,
        global_template,
        params(
            ("PrimaryALBDns", primary_alb_dns),
            ("SecondaryALBDns", secondary_alb_dns),
            ("DomainName", config.DOMAIN_NAME),
            ("HostedZoneId", DEPLOY_PARAMS["HostedZoneId"]),
        ),
    )

    cloudfront_domain = global_outputs["CloudFrontDomain"]
    app_url = global_outputs["ApplicationURL"]

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=== Deployment complete ===")
    log.info("Primary ALB DNS   : %s", primary_alb_dns)
    log.info("Secondary ALB DNS : %s", secondary_alb_dns)
    log.info("CloudFront Domain : %s", cloudfront_domain)
    log.info("Application URL   : %s", app_url)
    log.info("Primary S3 Bucket : %s", primary_s3_bucket)
    log.info("Secondary S3 Bucket: %s", secondary_s3_bucket)

    return {
        "primary": primary_outputs,
        "secondary": secondary_outputs,
        "global": global_outputs,
    }


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Deployment failed: %s", exc)
        sys.exit(1)
