"""
Test suite for the Serverless Microservices Platform
Unit tests for Lambda handlers, validators, and API Gateway configuration.
"""
import pytest
import json
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lambdas.checkout_handler import CheckoutValidator, OrderManager, OrderState
from lambdas.checkout_processor import PaymentProcessor
from api_gateway_config import APIGatewayConfig, generate_cors_preflight


class TestCheckoutValidator:
    def test_validate_valid_order(self):
        order = {
            "items": [
                {"id": "item-1", "quantity": 2, "price": 10.00},
                {"id": "item-2", "quantity": 1, "price": 25.00},
            ],
        }
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is True
        assert error is None

    def test_validate_empty_order(self):
        order = {"items": []}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "at least one item" in error

    def test_validate_too_many_items(self):
        order = {"items": [{"id": f"item-{i}", "quantity": 1, "price": 1.00} for i in range(51)]}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "maximum" in error

    def test_validate_zero_quantity(self):
        order = {"items": [{"id": "item-1", "quantity": 0, "price": 10.00}]}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "Invalid quantity" in error

    def test_validate_negative_price(self):
        order = {"items": [{"id": "item-1", "quantity": 1, "price": -5.00}]}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "Invalid price" in error

    def test_validate_order_below_minimum(self):
        order = {"items": [{"id": "item-1", "quantity": 1, "price": 0.01}]}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "below minimum" in error

    def test_validate_order_above_maximum(self):
        order = {"items": [{"id": "item-1", "quantity": 10000, "price": 10.00}]}
        valid, error = CheckoutValidator.validate_order(order)
        assert valid is False
        assert "exceeds maximum" in error

    def test_generate_order_id(self):
        order_id = CheckoutValidator.generate_order_id()
        assert order_id.startswith("ORD-")
        assert len(order_id) > 10

    def test_compute_order_hash(self):
        order = {"items": [{"id": "item-1", "quantity": 1, "price": 10.00}]}
        hash1 = CheckoutValidator.compute_order_hash(order)
        hash2 = CheckoutValidator.compute_order_hash(order)
        assert hash1 == hash2
        assert len(hash1) == 16


class TestPaymentProcessor:
    def test_process_payment(self):
        result = PaymentProcessor.process_payment("ORD-123", 50.00)
        assert result["status"] == "CONFIRMED"
        assert result["payment_id"].startswith("PAY-")
        assert result["amount"] == 50.00
        assert result["currency"] == "GBP"

    def test_refund_payment(self):
        result = PaymentProcessor.refund_payment("PAY-123", 25.00)
        assert result["status"] == "PROCESSED"
        assert result["refund_id"].startswith("REF-")
        assert result["amount"] == 25.00


class TestOrderManager:
    def test_create_order(self):
        manager = OrderManager()
        items = [{"id": "item-1", "quantity": 2, "price": 15.00}]
        order = manager.create_order(items, "customer-123")
        assert order["order_id"].startswith("ORD-")
        assert order["status"] == OrderState.VALIDATED
        assert order["total"] == 30.00
        assert order["customer_id"] == "customer-123"

    def test_process_checkout(self):
        manager = OrderManager()
        items = [{"id": "item-1", "quantity": 1, "price": 10.00}]
        order = manager.create_order(items, "customer-123")
        completed = manager.process_checkout(order)
        assert completed["status"] == OrderState.PAYMENT_CONFIRMED
        assert "payment" in completed


class TestAPIGatewayConfig:
    def test_default_config(self):
        config = APIGatewayConfig()
        assert config.stage_name == "v1"
        assert len(config.endpoints) > 0

    def test_to_cloudformation(self):
        config = APIGatewayConfig()
        resources = config.to_cloudformation()
        assert len(resources) > 0

    def test_cors_preflight(self):
        cors = generate_cors_preflight()
        assert cors["Type"] == "AWS::ApiGateway::Method"
        assert cors["Properties"]["HttpMethod"] == "OPTIONS"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])