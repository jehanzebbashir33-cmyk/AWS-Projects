import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

sns_client = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
NOTIFICATION_TOPIC_ARN = os.environ["NOTIFICATION_TOPIC_ARN"]

table = dynamodb.Table(ORDERS_TABLE)

EVENT_SUBJECTS = {
    "OrderPlaced": "New Order Placed",
    "OrderShipped": "Your Order Has Shipped",
    "InventoryReserved": "Inventory Reserved for Order",
}


def _get_order(order_id: str) -> dict | None:
    resp = table.query(
        KeyConditionExpression="orderId = :oid",
        ExpressionAttributeValues={":oid": order_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _already_notified(order: dict, event_type: str) -> bool:
    notified = order.get("notifiedEvents", [])
    return event_type in notified


def _mark_notified(order_id: str, created_at: str, event_type: str) -> None:
    table.update_item(
        Key={"orderId": order_id, "createdAt": created_at},
        UpdateExpression="ADD notifiedEvents :e",
        ExpressionAttributeValues={":e": {event_type}},
    )


def _publish_notification(order_id: str, event_type: str, detail: dict) -> str:
    subject = EVENT_SUBJECTS.get(event_type, f"Order Update: {event_type}")
    message_body = {
        "orderId": order_id,
        "eventType": event_type,
        "detail": detail,
    }
    resp = sns_client.publish(
        TopicArn=NOTIFICATION_TOPIC_ARN,
        Subject=subject[:100],
        Message=json.dumps(message_body, indent=2),
        MessageAttributes={
            "eventType": {
                "DataType": "String",
                "StringValue": event_type,
            }
        },
    )
    return resp["MessageId"]


def _process_record(record: dict) -> dict:
    body = json.loads(record["body"])
    event_type: str = body.get("detail-type", "OrderEvent")
    detail: dict = body.get("detail", body)
    order_id: str = detail.get("orderId", "")

    if not order_id:
        log.warning("Notification record missing orderId — skipping.")
        return {"status": "skipped", "reason": "missing orderId"}

    order = _get_order(order_id)
    if not order:
        log.warning("Order not found for notification: orderId=%s", order_id)
        return {"orderId": order_id, "status": "order_not_found"}

    if _already_notified(order, event_type):
        log.info("Notification already sent for orderId=%s eventType=%s — idempotent skip.", order_id, event_type)
        return {"orderId": order_id, "status": "already_notified", "eventType": event_type}

    message_id = _publish_notification(order_id, event_type, detail)
    _mark_notified(order_id, order["createdAt"], event_type)

    log.info("Notification sent for orderId=%s eventType=%s messageId=%s", order_id, event_type, message_id)
    return {"orderId": order_id, "status": "notified", "messageId": message_id, "eventType": event_type}


def handler(event: dict, context) -> dict:
    log.info("Notification handler invoked with %d record(s).", len(event.get("Records", [])))

    results = []
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            result = _process_record(record)
            results.append(result)
        except ClientError as exc:
            log.error("AWS error processing messageId=%s: %s", message_id, exc, exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})
        except Exception as exc:
            log.error("Unexpected error for messageId=%s: %s", message_id, exc, exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})

    response = {"notified": len(results), "results": results}
    if batch_item_failures:
        response["batchItemFailures"] = batch_item_failures

    return response
