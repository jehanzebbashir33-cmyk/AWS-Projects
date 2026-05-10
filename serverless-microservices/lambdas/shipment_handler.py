import json
import logging
import os
import uuid

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
events_client = boto3.client("events")

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]

table = dynamodb.Table(ORDERS_TABLE)


def _find_order(order_id: str, created_at: str = "") -> dict | None:
    if created_at:
        resp = table.get_item(Key={"orderId": order_id, "createdAt": created_at})
        return resp.get("Item")
    resp = table.query(
        KeyConditionExpression="orderId = :oid",
        ExpressionAttributeValues={":oid": order_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _assign_tracking(order_id: str, created_at: str) -> str | None:
    tracking_id = str(uuid.uuid4())
    try:
        table.update_item(
            Key={"orderId": order_id, "createdAt": created_at},
            UpdateExpression="SET #st = :s, trackingId = :t",
            ConditionExpression="attribute_not_exists(trackingId)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "SHIPPED", ":t": tracking_id},
        )
        return tracking_id
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def _publish_order_shipped(order_id: str, tracking_id: str, customer_id: str) -> None:
    events_client.put_events(
        Entries=[
            {
                "Source": "microservices.shipment",
                "DetailType": "OrderShipped",
                "EventBusName": EVENT_BUS_NAME,
                "Detail": json.dumps({
                    "orderId": order_id,
                    "trackingId": tracking_id,
                    "customerId": customer_id,
                }),
            }
        ]
    )


def _process_record(record: dict) -> dict:
    body = json.loads(record["body"])
    detail: dict = body.get("detail", body)
    order_id: str = detail.get("orderId", "")
    created_at: str = detail.get("createdAt", "")

    if not order_id:
        log.warning("Shipment record missing orderId — skipping.")
        return {"status": "skipped", "reason": "missing orderId"}

    order = _find_order(order_id, created_at)
    if not order:
        log.warning("Order not found for shipment: orderId=%s", order_id)
        return {"orderId": order_id, "status": "not_found"}

    if order.get("trackingId"):
        log.info("Order already shipped: orderId=%s trackingId=%s", order_id, order["trackingId"])
        return {
            "orderId": order_id,
            "status": "already_shipped",
            "trackingId": order["trackingId"],
        }

    tracking_id = _assign_tracking(order_id, order["createdAt"])
    if not tracking_id:
        existing = _find_order(order_id, order["createdAt"])
        log.info("Concurrent shipment for orderId=%s — returning existing trackingId.", order_id)
        return {
            "orderId": order_id,
            "status": "already_shipped",
            "trackingId": existing.get("trackingId") if existing else "unknown",
        }

    try:
        _publish_order_shipped(order_id, tracking_id, order.get("customerId", ""))
    except Exception as exc:
        log.error("Failed to publish OrderShipped event for orderId=%s: %s", order_id, exc)

    log.info("Order shipped: orderId=%s trackingId=%s", order_id, tracking_id)
    return {"orderId": order_id, "status": "shipped", "trackingId": tracking_id}


def handler(event: dict, context) -> dict:
    log.info("Shipment handler invoked with %d record(s).", len(event.get("Records", [])))

    results = []
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            result = _process_record(record)
            results.append(result)
        except ClientError as exc:
            log.error("AWS error for messageId=%s: %s", message_id, exc, exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})
        except Exception as exc:
            log.error("Unexpected error for messageId=%s: %s", message_id, exc, exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})

    response = {"processed": len(results), "results": results}
    if batch_item_failures:
        response["batchItemFailures"] = batch_item_failures

    return response
