"""
Data Engineering Analytics Platform — Stream Processor
Real-time event processing pipeline: Kinesis → Lambda → S3/Athena
"""
import json
import base64
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional


# Event schema definitions
SCHEMA_VERSION = "1.0"

EVENT_TYPES = {
    "page_view": {
        "required_fields": ["user_id", "page_url", "timestamp"],
        "optional_fields": ["referrer", "session_id", "device_type", "browser"],
    },
    "purchase": {
        "required_fields": ["user_id", "product_id", "amount", "currency", "timestamp"],
        "optional_fields": ["payment_method", "discount_code", "quantity"],
    },
    "search": {
        "required_fields": ["user_id", "query", "timestamp"],
        "optional_fields": ["filters", "results_count", "page_number"],
    },
    "error": {
        "required_fields": ["error_code", "message", "timestamp"],
        "optional_fields": ["user_id", "stack_trace", "endpoint"],
    },
}


class SchemaValidator:
    """Validates incoming events against defined schemas."""

    @staticmethod
    def validate_event(event: dict) -> tuple[bool, Optional[str]]:
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            return False, f"Unknown event type: {event_type}"

        schema = EVENT_TYPES[event_type]
        for field in schema["required_fields"]:
            if field not in event:
                return False, f"Missing required field: {field}"

        # Validate timestamp format
        ts = event.get("timestamp")
        if ts:
            try:
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return False, f"Invalid timestamp format: {ts}"

        # Validate amount for purchase events
        if event_type == "purchase":
            amount = event.get("amount")
            if amount is not None and (not isinstance(amount, (int, float)) or amount < 0):
                return False, f"Invalid amount: {amount}"

        return True, None

    @staticmethod
    def sanitize_event(event: dict) -> dict:
        """Remove PII and sanitize input fields."""
        sanitized = {}
        for key, value in event.items():
            if isinstance(value, str):
                value = re.sub(r'\b[\w.-]+@[\w.-]+\.\w+\b', '[REDACTED_EMAIL]', value)
                value = re.sub(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', '[REDACTED_CARD]', value)
            sanitized[key] = value
        return sanitized


class EventEnricher:
    """Enriches events with computed fields and geolocation."""

    @staticmethod
    def enrich(event: dict, lambda_context=None) -> dict:
        enriched = event.copy()

        # Add processing metadata
        enriched["processed_at"] = datetime.now(timezone.utc).isoformat()
        enriched["schema_version"] = SCHEMA_VERSION

        # Compute event fingerprint for deduplication
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        enriched["event_hash"] = hashlib.sha256(canonical.encode()).hexdigest()[:16]

        # Add partition key for S3 storage
        ts = event.get("timestamp", enriched["processed_at"])
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        enriched["s3_partition"] = f"year={dt.year}/month={dt.month:02d}/day={dt.day:02d}"
        enriched["s3_key"] = f"{dt.strftime('%Y/%m/%d/%H')}/{enriched['event_hash']}.json"

        # Device categorization
        device = event.get("device_type", "unknown")
        if "mobile" in device.lower():
            enriched["device_category"] = "mobile"
        elif "tablet" in device.lower():
            enriched["device_category"] = "tablet"
        else:
            enriched["device_category"] = "desktop"

        return enriched


class DeadLetterHandler:
    """Handles events that fail validation or processing."""

    DLQ_BUCKET = "snaptorent-analytics-dlq"
    DLQ_PREFIX = "failed-events/"

    @staticmethod
    def send_to_dlq(event: dict, reason: str, original_record: str) -> dict:
        return {
            "original_event": event,
            "failure_reason": reason,
            "original_record": original_record,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retry_count": 0,
            "s3_key": f"{DeadLetterHandler.DLQ_PREFIX}"
                       f"{datetime.now(timezone.utc).strftime('%Y/%m/%d/%H')}/"
                       f"{hashlib.md5(original_record.encode()).hexdigest()}.json",
        }


def lambda_handler(event, context):
    """Kinesis stream processor Lambda."""
    processed = []
    failed = []

    for record in event.get("Records", []):
        try:
            # Decode Kinesis record
            payload = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
            raw_event = json.loads(payload)

            # Validate schema
            valid, error = SchemaValidator.validate_event(raw_event)
            if not valid:
                failed.append(DeadLetterHandler.send_to_dlq(raw_event, error, payload))
                continue

            # Sanitize PII
            sanitized = SchemaValidator.sanitize_event(raw_event)

            # Enrich with computed fields
            enriched = EventEnricher.enrich(sanitized, context)

            processed.append(enriched)

        except json.JSONDecodeError as e:
            failed.append(DeadLetterHandler.send_to_dlq(
                {"raw": payload[:200]}, f"JSON parse error: {str(e)}", payload
            ))
        except Exception as e:
            failed.append(DeadLetterHandler.send_to_dlq(
                {}, f"Processing error: {str(e)}", str(record)
            ))

    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed_count": len(processed),
            "failed_count": len(failed),
            "processed": processed[:10],  # Sample for logging
            "failed_sample": failed[:5],
        }),
    }