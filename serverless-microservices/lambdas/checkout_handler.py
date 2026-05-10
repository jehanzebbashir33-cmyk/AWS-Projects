import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
events_client = boto3.client("events")

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]

table = dynamodb.Table(ORDERS_TABLE)


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _is_duplicate(order_id: str) -> dict | None:
    resp = table.query(
        KeyConditionExpression="orderId = :oid",
        ExpressionAttributeValues={":oid": order_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _write_order(order_id: str, customer_id: str, items: list, total: Decimal, created_at: str) -> None:
    table.put_item(
        Item={
            "orderId": order_id,
            "customerId": customer_id,
            "items": items,
            "total": str(total),
            "status": "PENDING",
            "createdAt": created_at,
        },
        ConditionExpression="attribute_not_exists(orderId)",
    )


def _publish_order_placed(order_id: str, customer_id: str, total: Decimal, created_at: str) -> None:
    events_client.put_events(
        Entries=[
            {
                "Source": "microservices.checkout",
                "DetailType": "OrderPlaced",
                "EventBusName": EVENT_BUS_NAME,
                "Detail": json.dumps({
                    "orderId": order_id,
                    "customerId": customer_id,
                    "total": float(total),
                    "createdAt": created_at,
                }),
            }
        ]
    )


def handler(event: dict, context) -> dict:
    log.info("Checkout invoked: requestId=%s", context.aws_request_id if context else "local")

    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("Invalid JSON body: %s", exc)
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    customer_id: str = body.get("customerId", "").strip()
    items: list = body.get("items", [])
    order_id: str = body.get("orderId") or str(uuid.uuid4())

    if not customer_id:
        return {"statusCode": 400, "body": json.dumps({"error": "customerId is required"})}
    if not items or not isinstance(items, list):
        return {"statusCode": 400, "body": json.dumps({"error": "items must be a non-empty list"})}

    existing = _is_duplicate(order_id)
    if existing:
        log.info("Idempotency hit for orderId=%s", order_id)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "orderId": order_id,
                "status": existing.get("status"),
                "idempotent": True,
            }),
        }

    total = Decimal(str(
        sum(
            Decimal(str(i.get("price", 0))) * Decimal(str(i.get("quantity", 1)))
            for i in items
        )
    ))
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        _write_order(order_id, customer_id, items, total, created_at)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning("Race condition on orderId=%s — returning existing record", order_id)
            existing = _is_duplicate(order_id)
            return {
                "statusCode": 200,
                "body": json.dumps({"orderId": order_id, "idempotent": True, "status": existing.get("status")}),
            }
        log.error("DynamoDB error: %s", exc)
        return {"statusCode": 500, "body": json.dumps({"error": "Failed to persist order"})}

    try:
        _publish_order_placed(order_id, customer_id, total, created_at)
    except Exception as exc:
        log.error("EventBridge publish failed for orderId=%s: %s", order_id, exc)

    log.info("Order created: orderId=%s customerId=%s total=%s", order_id, customer_id, total)
    return {
        "statusCode": 201,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "orderId": order_id,
            "status": "PENDING",
            "total": float(total),
            "createdAt": created_at,
        }),
    }
