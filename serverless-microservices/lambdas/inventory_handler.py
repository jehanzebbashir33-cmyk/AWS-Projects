import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
table = dynamodb.Table(ORDERS_TABLE)


def _get_order(order_id: str, created_at: str) -> dict | None:
    resp = table.get_item(Key={"orderId": order_id, "createdAt": created_at})
    return resp.get("Item")


def _find_order_by_id(order_id: str) -> dict | None:
    resp = table.query(
        KeyConditionExpression="orderId = :oid",
        ExpressionAttributeValues={":oid": order_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _reserve_inventory(order_id: str, created_at: str) -> bool:
    try:
        table.update_item(
            Key={"orderId": order_id, "createdAt": created_at},
            UpdateExpression="SET #st = :s, inventoryReserved = :r",
            ConditionExpression="inventoryReserved <> :r",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "INVENTORY_RESERVED", ":r": True},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _process_record(record: dict) -> dict:
    body = json.loads(record["body"])
    detail = body.get("detail", body)
    order_id: str = detail.get("orderId", "")
    created_at: str = detail.get("createdAt", "")

    if not order_id:
        log.warning("Record missing orderId — skipping.")
        return {"status": "skipped", "reason": "missing orderId"}

    order = _get_order(order_id, created_at) if created_at else _find_order_by_id(order_id)
    if not order:
        log.warning("Order not found: orderId=%s", order_id)
        return {"orderId": order_id, "status": "not_found"}

    if order.get("inventoryReserved"):
        log.info("Inventory already reserved for orderId=%s — idempotent skip.", order_id)
        return {"orderId": order_id, "status": "already_reserved"}

    reserved = _reserve_inventory(order_id, order["createdAt"])
    if not reserved:
        log.info("Concurrent reservation detected for orderId=%s.", order_id)
        return {"orderId": order_id, "status": "already_reserved"}

    log.info("Inventory reserved for orderId=%s", order_id)
    return {"orderId": order_id, "status": "reserved"}


def handler(event: dict, context) -> dict:
    log.info("Inventory handler invoked with %d record(s).", len(event.get("Records", [])))

    results = []
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            result = _process_record(record)
            results.append(result)
        except Exception as exc:
            log.error("Failed to process messageId=%s: %s", message_id, exc, exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})

    response = {"processed": len(results), "results": results}
    if batch_item_failures:
        response["batchItemFailures"] = batch_item_failures

    return response
