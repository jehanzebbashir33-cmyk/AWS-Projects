"""
Test suite for Health Monitor — Multi-Region Disaster Recovery
"""
import pytest
from unittest.mock import patch, MagicMock
from health_monitor import (
    HealthMonitor, HealthStatus, FailoverState,
    PRIMARY, SECONDARY, HealthCheckResult
)
from datetime import datetime, timezone


class TestHealthCheckResult:
    def test_healthy_result(self):
        result = HealthCheckResult(
            region="eu-west-2",
            status=HealthStatus.HEALTHY,
            response_time_ms=45.2,
            status_code=200,
        )
        assert result.status == HealthStatus.HEALTHY
        assert result.response_time_ms == 45.2

    def test_unhealthy_result(self):
        result = HealthCheckResult(
            region="eu-west-2",
            status=HealthStatus.UNHEALTHY,
            response_time_ms=10000.0,
            error="Connection timeout",
        )
        assert result.status == HealthStatus.UNHEALTHY
        assert result.error == "Connection timeout"


class TestHealthMonitor:
    def setup_method(self):
        self.monitor = HealthMonitor(PRIMARY, SECONDARY)

    def test_initial_state(self):
        assert self.monitor.state == FailoverState.ACTIVE_PRIMARY
        assert self.monitor.primary_failures == 0

    def test_evaluate_failover_no_failover_needed(self):
        result = HealthCheckResult(
            region=PRIMARY.region,
            status=HealthStatus.HEALTHY,
            response_time_ms=50.0,
            status_code=200,
        )
        decision = self.monitor.evaluate_failover(result)
        assert decision is None
        assert self.monitor.primary_failures == 0

    def test_evaluate_failover_triggers_on_threshold(self):
        self.monitor.primary.failure_threshold = 3
        for i in range(3):
            result = HealthCheckResult(
                region=PRIMARY.region,
                status=HealthStatus.UNHEALTHY,
                response_time_ms=10000.0,
                error="Connection refused",
            )
            decision = self.monitor.evaluate_failover(result)

        assert decision == "FAILOVER_TO_SECONDARY"

    def test_evaluate_failback_after_recovery(self):
        self.monitor.state = FailoverState.ACTIVE_SECONDARY
        self.monitor.primary.recovery_threshold = 5
        decision = None
        for i in range(5):
            result = HealthCheckResult(
                region=PRIMARY.region,
                status=HealthStatus.HEALTHY,
                response_time_ms=50.0,
                status_code=200,
            )
            decision = self.monitor.evaluate_failover(result)

        assert decision == "FAILBACK_TO_PRIMARY"

    def test_degraded_status_reduces_failures(self):
        self.monitor.primary_failures = 2
        result = HealthCheckResult(
            region=PRIMARY.region,
            status=HealthStatus.DEGRADED,
            response_time_ms=2000.0,
            status_code=503,
        )
        self.monitor.evaluate_failover(result)
        assert self.monitor.primary_failures == 1  # Reduced by 1


class TestHealthStatus:
    def test_status_values(self):
        assert HealthStatus.HEALTHY.value == "HEALTHY"
        assert HealthStatus.DEGRADED.value == "DEGRADED"
        assert HealthStatus.UNHEALTHY.value == "UNHEALTHY"
        assert HealthStatus.UNKNOWN.value == "UNKNOWN"


class TestFailoverState:
    def test_state_values(self):
        assert FailoverState.ACTIVE_PRIMARY.value == "ACTIVE_PRIMARY"
        assert FailoverState.FAILOVER_INITIATED.value == "FAILOVER_INITIATED"
        assert FailoverState.ACTIVE_SECONDARY.value == "ACTIVE_SECONDARY"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])