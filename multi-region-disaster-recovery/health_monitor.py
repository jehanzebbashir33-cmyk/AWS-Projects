"""
Multi-Region Disaster Recovery — Health Check Monitor
Continuously monitors primary region health and triggers failover if needed.
"""
import json
import boto3
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class HealthStatus(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class FailoverState(Enum):
    ACTIVE_PRIMARY = "ACTIVE_PRIMARY"
    FAILOVER_INITIATED = "FAILOVER_INITIATED"
    ACTIVE_SECONDARY = "ACTIVE_SECONDARY"
    FAILBACK_INITIATED = "FAILBACK_INITIATED"


@dataclass
class RegionConfig:
    name: str
    region: str
    api_url: str
    health_check_path: str = "/health"
    health_check_interval: int = 30  # seconds
    failure_threshold: int = 3  # consecutive failures before failover
    recovery_threshold: int = 5  # consecutive successes before failback


@dataclass
class HealthCheckResult:
    region: str
    status: HealthStatus
    response_time_ms: float
    status_code: Optional[int] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


PRIMARY = RegionConfig(
    name="primary",
    region="eu-west-2",
    api_url="https://api-primary.snaptorent.com",
)

SECONDARY = RegionConfig(
    name="secondary",
    region="eu-west-1",
    api_url="https://api-secondary.snaptorent.com",
)


class HealthMonitor:
    """Monitors region health and manages failover decisions."""

    def __init__(self, primary: RegionConfig, secondary: RegionConfig):
        self.primary = primary
        self.secondary = secondary
        self.primary_failures = 0
        self.secondary_failures = 0
        self.primary_recoveries = 0
        self.state = FailoverState.ACTIVE_PRIMARY
        self.route53 = boto3.client("route53", region_name="us-east-1")
        self.sns = boto3.client("sns", region_name=primary.region)
        self.cloudwatch = boto3.client("cloudwatch", region_name=primary.region)

    def check_health(self, config: RegionConfig) -> HealthCheckResult:
        """Perform health check against a region's API endpoint."""
        import urllib.request
        import urllib.error

        start = time.time()
        try:
            url = f"{config.api_url}{config.health_check_path}"
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "DR-Monitor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                elapsed = (time.time() - start) * 1000
                return HealthCheckResult(
                    region=config.region,
                    status=HealthStatus.HEALTHY if resp.status == 200 else HealthStatus.DEGRADED,
                    response_time_ms=round(elapsed, 2),
                    status_code=resp.status,
                )
        except urllib.error.HTTPError as e:
            elapsed = (time.time() - start) * 1000
            return HealthCheckResult(
                region=config.region,
                status=HealthStatus.UNHEALTHY if e.code >= 500 else HealthStatus.DEGRADED,
                response_time_ms=round(elapsed, 2),
                status_code=e.code,
                error=str(e),
            )
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            return HealthCheckResult(
                region=config.region,
                status=HealthStatus.UNHEALTHY,
                response_time_ms=round(elapsed, 2),
                error=str(e),
            )

    def evaluate_failover(self, result: HealthCheckResult) -> Optional[str]:
        """Evaluate if failover should be triggered based on health check results."""
        if result.region == self.primary.region:
            if result.status == HealthStatus.UNHEALTHY:
                self.primary_failures += 1
                self.primary_recoveries = 0
                if self.primary_failures >= self.primary.failure_threshold:
                    return "FAILOVER_TO_SECONDARY"
            elif result.status == HealthStatus.HEALTHY:
                self.primary_failures = 0
                self.primary_recoveries += 1
                if (self.state == FailoverState.ACTIVE_SECONDARY and
                        self.primary_recoveries >= self.primary.recovery_threshold):
                    return "FAILBACK_TO_PRIMARY"
            else:  # DEGRADED
                self.primary_failures = max(0, self.primary_failures - 1)
                self.primary_recoveries = 0
        return None

    def execute_failover(self, target: RegionConfig) -> dict:
        """Execute DNS failover via Route 53."""
        hosted_zone_id = "Z1PA6795UKMFR9"  # Would be from SSM Parameter Store
        record_name = "api.snaptorent.com."

        try:
            response = self.route53.change_resource_record_sets(
                HostedZoneId=hosted_zone_id,
                ChangeBatch={
                    "Changes": [{
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": record_name,
                            "Type": "A",
                            "AliasTarget": {
                                "HostedZoneId": target.region,  # ELB hosted zone ID
                                "DNSName": target.api_url,
                                "EvaluateTargetHealth": True,
                            },
                            "TTL": 60,
                        },
                    }],
                    "Comment": f"DR failover from {self.primary.region} to {target.region}",
                },
            )
            self.state = FailoverState.FAILOVER_INITIATED
            return {"status": "FAILOVER_INITIATED", "change_id": response["ChangeInfo"]["Id"]}
        except Exception as e:
            return {"status": "FAILOVER_FAILED", "error": str(e)}

    def publish_metric(self, result: HealthCheckResult):
        """Publish health check metrics to CloudWatch."""
        self.cloudwatch.put_metric_data(
            Namespace="DRMonitor/HealthCheck",
            MetricData=[{
                "MetricName": "ResponseTime",
                "Value": result.response_time_ms,
                "Unit": "Milliseconds",
                "Dimensions": [
                    {"Name": "Region", "Value": result.region},
                    {"Name": "Status", "Value": result.status.value},
                ],
            }],
        )

    def send_alert(self, subject: str, message: str):
        """Send SNS alert for failover events."""
        topic_arn = f"arn:aws:sns:{self.primary.region}:123456789012:dr-alerts"
        self.sns.publish(TopicArn=topic_arn, Subject=subject, Message=message)


def lambda_handler(event, context):
    """Lambda handler for scheduled DR health monitoring."""
    monitor = HealthMonitor(PRIMARY, SECONDARY)

    primary_result = monitor.check_health(PRIMARY)
    monitor.publish_metric(primary_result)

    failover_decision = monitor.evaluate_failover(primary_result)

    if failover_decision == "FAILOVER_TO_SECONDARY":
        result = monitor.execute_failover(SECONDARY)
        monitor.send_alert(
            "DR: Failover Initiated",
            f"Primary region {PRIMARY.region} unhealthy. Failing over to {SECONDARY.region}.\n"
            f"Health: {primary_result.status.value}\nResult: {json.dumps(result)}",
        )
    elif failover_decision == "FAILBACK_TO_PRIMARY":
        result = monitor.execute_failover(PRIMARY)
        monitor.send_alert(
            "DR: Failback to Primary",
            f"Primary region {PRIMARY.region} recovered. Failing back from {SECONDARY.region}.\n"
            f"Result: {json.dumps(result)}",
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "primary_health": primary_result.status.value,
            "primary_response_ms": primary_result.response_time_ms,
            "failover_decision": failover_decision,
            "current_state": monitor.state.value,
        }),
    }