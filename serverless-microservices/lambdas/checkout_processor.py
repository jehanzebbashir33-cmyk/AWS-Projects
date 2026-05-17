"""
Order Validation & Processing Pipeline
Handles checkout validation, payment simulation, and order state management.
"""
import json
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Optional


# Order states matching DynamoDB state machine
class OrderState:
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    PAYMENT_PROCESSING = "PAYMENT_PROCESSING"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class CheckoutValidator:
    """Validates order items, quantities, and pricing before checkout."""

    MAX_ITEMS_PER_ORDER = 50
    MAX_QUANTITY_PER_ITEM = 100
    MIN_ORDER_VALUE = 0.50  # GBP
    MAX_ORDER_VALUE = 10000.00  # GBP

    @staticmethod
    def validate_order(order: dict) -> tuple[bool, Optional[str]]:
        items = order.get("items", [])
        if not items:
            return False, "Order must contain at least one item"
        if len(items) > CheckoutValidator.MAX_ITEMS_PER_ORDER:
            return False, f"Order exceeds maximum of {CheckoutValidator.MAX_ITEMS_PER_ORDER} items"

        total = 0.0
        for item in items:
            qty = item.get("quantity", 0)
            if qty <= 0 or qty > CheckoutValidator.MAX_QUANTITY_PER_ITEM:
                return False, f"Invalid quantity for item {item.get('id', 'unknown')}"
            price = item.get("price", 0)
            if price <= 0:
                return False, f"Invalid price for item {item.get('id', 'unknown')}"
            total += qty * price

        if total < CheckoutValidator.MIN_ORDER_VALUE:
            return False, f"Order value below minimum ({CheckoutValidator.MIN_ORDER_VALUE})"
        if total > CheckoutValidator.MAX_ORDER_VALUE:
            return False, f"Order value exceeds maximum ({CheckoutValidator.MAX_ORDER_VALUE})"

        return True, None

    @staticmethod
    def generate_order_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"ORD-{timestamp}-{short_uuid}"

    @staticmethod
    def compute_order_hash(order: dict) -> str:
        canonical = json.dumps(order, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class PaymentProcessor:
    """Simulates payment processing integration (Stripe/PayPal)."""

    @staticmethod
    def process_payment(order_id: str, amount: float, currency: str = "GBP") -> dict:
        # In production, this would call Stripe/PayPal API
        # For the portfolio, we simulate the payment flow
        payment_id = f"PAY-{uuid.uuid4().hex[:12]}"
        return {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "status": "CONFIRMED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": "stripe_simulation",
        }

    @staticmethod
    def refund_payment(payment_id: str, amount: float, reason: str = "customer_request") -> dict:
        return {
            "refund_id": f"REF-{uuid.uuid4().hex[:8]}",
            "payment_id": payment_id,
            "amount": amount,
            "reason": reason,
            "status": "PROCESSED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class OrderManager:
    """Manages order lifecycle in DynamoDB."""

    def __init__(self, table_name: str = "Orders"):
        self.table_name = table_name

    def create_order(self, items: list, customer_id: str) -> dict:
        order = {
            "order_id": CheckoutValidator.generate_order_id(),
            "customer_id": customer_id,
            "items": items,
            "total": sum(i["quantity"] * i["price"] for i in items),
            "status": OrderState.PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "order_hash": CheckoutValidator.compute_order_hash({"items": items}),
        }

        valid, error = CheckoutValidator.validate_order(order)
        if not valid:
            raise ValueError(f"Order validation failed: {error}")

        # Transition to VALIDATED
        order["status"] = OrderState.VALIDATED
        return order

    def process_checkout(self, order: dict) -> dict:
        payment = PaymentProcessor.process_payment(
            order["order_id"],
            order["total"],
        )
        order["payment"] = payment
        order["status"] = OrderState.PAYMENT_CONFIRMED
        order["updated_at"] = datetime.now(timezone.utc).isoformat()
        return order


def lambda_handler(event, context):
    """AWS Lambda handler for checkout processing."""
    try:
        body = json.loads(event.get("body", "{}"))
        items = body.get("items", [])
        customer_id = body.get("customer_id", "anonymous")

        manager = OrderManager()
        order = manager.create_order(items, customer_id)
        completed_order = manager.process_checkout(order)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "order_id": completed_order["order_id"],
                "status": completed_order["status"],
                "total": completed_order["total"],
                "payment_id": completed_order["payment"]["payment_id"],
            }),
        }
    except ValueError as e:
        return {"statusCode": 400, "body": json.dumps({"error": str(e)})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error"})}