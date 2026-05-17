"""
Test suite for Stream Processor — Data Engineering Analytics Platform
"""
import pytest
import json
import base64
from processor.stream_processor import (
    SchemaValidator, EventEnricher, DeadLetterHandler, lambda_handler
)


def make_kinesis_record(event_dict: dict) -> dict:
    """Helper to create a Kinesis record from an event dict."""
    data = base64.b64encode(json.dumps(event_dict).encode()).decode()
    return {
        "kinesis": {
            "data": data,
            "partitionKey": "test-key",
            "sequenceNumber": "4960737986492337705031504768",
        }
    }


class TestSchemaValidator:
    def test_valid_page_view(self):
        event = {
            "event_type": "page_view",
            "user_id": "user-123",
            "page_url": "https://example.com/home",
            "timestamp": "2025-01-15T10:30:00Z",
        }
        valid, error = SchemaValidator.validate_event(event)
        assert valid is True
        assert error is None

    def test_valid_purchase(self):
        event = {
            "event_type": "purchase",
            "user_id": "user-456",
            "product_id": "prod-789",
            "amount": 29.99,
            "currency": "GBP",
            "timestamp": "2025-01-15T12:00:00Z",
        }
        valid, error = SchemaValidator.validate_event(event)
        assert valid is True

    def test_unknown_event_type(self):
        event = {"event_type": "unknown_type", "timestamp": "2025-01-15T10:00:00Z"}
        valid, error = SchemaValidator.validate_event(event)
        assert valid is False
        assert "Unknown event type" in error

    def test_missing_required_field(self):
        event = {
            "event_type": "page_view",
            "user_id": "user-123",
            # Missing page_url and timestamp
        }
        valid, error = SchemaValidator.validate_event(event)
        assert valid is False
        assert "Missing required field" in error

    def test_invalid_timestamp(self):
        event = {
            "event_type": "page_view",
            "user_id": "user-123",
            "page_url": "/home",
            "timestamp": "not-a-date",
        }
        valid, error = SchemaValidator.validate_event(event)
        assert valid is False
        assert "Invalid timestamp" in error

    def test_negative_purchase_amount(self):
        event = {
            "event_type": "purchase",
            "user_id": "user-123",
            "product_id": "prod-1",
            "amount": -10.00,
            "currency": "GBP",
            "timestamp": "2025-01-15T10:00:00Z",
        }
        valid, error = SchemaValidator.validate_event(event)
        assert valid is False
        assert "Invalid amount" in error


class TestEventEnricher:
    def test_enrich_adds_metadata(self):
        event = {
            "event_type": "page_view",
            "user_id": "user-123",
            "page_url": "/home",
            "timestamp": "2025-01-15T10:30:00Z",
        }
        enriched = EventEnricher.enrich(event)
        assert "processed_at" in enriched
        assert "schema_version" in enriched
        assert "event_hash" in enriched
        assert "s3_partition" in enriched

    def test_enrich_computes_partition_key(self):
        event = {
            "event_type": "page_view",
            "user_id": "user-123",
            "page_url": "/home",
            "timestamp": "2025-01-15T10:30:00Z",
        }
        enriched = EventEnricher.enrich(event)
        assert "year=2025" in enriched["s3_partition"]
        assert "month=01" in enriched["s3_partition"]

    def test_device_category_mobile(self):
        event = {"device_type": "mobile Safari", "timestamp": "2025-01-15T10:00:00Z"}
        enriched = EventEnricher.enrich(event)
        assert enriched["device_category"] == "mobile"

    def test_device_category_desktop(self):
        event = {"device_type": "Chrome on Windows", "timestamp": "2025-01-15T10:00:00Z"}
        enriched = EventEnricher.enrich(event)
        assert enriched["device_category"] == "desktop"


class TestSchemaValidatorSanitization:
    def test_email_redacted(self):
        event = {"text": "Contact user@example.com for details"}
        sanitized = SchemaValidator.sanitize_event(event)
        assert "[REDACTED_EMAIL]" in sanitized["text"]
        assert "user@example.com" not in sanitized["text"]

    def test_card_number_redacted(self):
        event = {"text": "Card: 1234 5678 9012 3456"}
        sanitized = SchemaValidator.sanitize_event(event)
        assert "[REDACTED_CARD]" in sanitized["text"]
        assert "1234 5678 9012 3456" not in sanitized["text"]


class TestLambdaHandler:
    def test_process_valid_record(self):
        event_data = {
            "event_type": "page_view",
            "user_id": "user-123",
            "page_url": "https://example.com",
            "timestamp": "2025-01-15T10:30:00Z",
        }
        kinesis_event = {"Records": [make_kinesis_record(event_data)]}
        result = lambda_handler(kinesis_event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed_count"] == 1
        assert body["failed_count"] == 0

    def test_process_invalid_record(self):
        event_data = {"event_type": "invalid_type"}
        kinesis_event = {"Records": [make_kinesis_record(event_data)]}
        result = lambda_handler(kinesis_event, None)
        body = json.loads(result["body"])
        assert body["failed_count"] == 1

    def test_process_malformed_json(self):
        kinesis_event = {
            "Records": [{
                "kinesis": {
                    "data": base64.b64encode(b"not json").decode(),
                    "partitionKey": "test",
                    "sequenceNumber": "1",
                }
            }]
        }
        result = lambda_handler(kinesis_event, None)
        body = json.loads(result["body"])
        assert body["failed_count"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])