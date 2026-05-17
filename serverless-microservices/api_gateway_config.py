"""
Serverless Microservices — API Gateway Configuration
Defines REST API endpoints, request/response models, and CORS settings.
"""
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class APIEndpoint:
    path: str
    method: str
    handler: str
    cors: bool = True
    auth: Optional[str] = None  # None = no auth, 'cognito' | 'api_key' | 'iam'
    throttle_rate: int = 100  # requests per second
    throttle_burst: int = 50


@dataclass
class APIGatewayConfig:
    stage_name: str = "v1"
    description: str = "Serverless Microservices API"
    minimum_compression_size: int = 1024  # bytes
    log_level: str = "INFO"
    enable_access_logging: bool = True
    enable_detailed_metrics: bool = True
    waf_acl_id: Optional[str] = None

    # Endpoints
    endpoints: list = field(default_factory=lambda: [
        # Inventory Service
        APIEndpoint("/inventory", "GET", "inventory_handler.list_items",
                     auth="cognito", throttle_rate=200),
        APIEndpoint("/inventory/{id}", "GET", "inventory_handler.get_item",
                     auth="cognito"),
        APIEndpoint("/inventory", "POST", "inventory_handler.create_item",
                     auth="cognito", throttle_rate=50),
        APIEndpoint("/inventory/{id}", "PUT", "inventory_handler.update_item",
                     auth="cognito", throttle_rate=50),
        APIEndpoint("/inventory/{id}", "DELETE", "inventory_handler.delete_item",
                     auth="cognito", throttle_rate=20),

        # Checkout Service
        APIEndpoint("/checkout", "POST", "checkout_handler.create_order",
                     auth="cognito", throttle_rate=100),
        APIEndpoint("/checkout/{id}", "GET", "checkout_handler.get_order",
                     auth="cognito"),
        APIEndpoint("/checkout/{id}/status", "GET", "checkout_handler.order_status",
                     auth="cognito"),

        # Shipment Service
        APIEndpoint("/shipment/{order_id}", "GET", "shipment_handler.get_shipment",
                     auth="cognito"),
        APIEndpoint("/shipment/{order_id}/track", "GET", "shipment_handler.track_shipment",
                     auth="cognito"),

        # Notification Service
        APIEndpoint("/notification/preferences", "GET",
                     "notification_handler.get_preferences", auth="cognito"),
        APIEndpoint("/notification/preferences", "PUT",
                     "notification_handler.update_preferences", auth="cognito"),
    ])

    def to_cloudformation(self) -> dict:
        """Generate CloudFormation resources for API Gateway."""
        resources = {}
        for i, ep in enumerate(self.endpoints):
            resource_key = f"ApiResource{i}"
            method_key = f"ApiMethod{i}"
            resources[resource_key] = {
                "Type": "AWS::ApiGateway::Resource",
                "Properties": {
                    "ParentId": {"Ref": "ApiGatewayRootResourceId"},
                    "PathPart": ep.path.strip("/").split("/")[-1].replace("{id}", "{proxy+}"),
                    "RestApiId": {"Ref": "ApiGatewayRestApi"},
                }
            }
            resources[method_key] = {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {
                    "HttpMethod": ep.method.upper(),
                    "ResourceId": {"Ref": resource_key},
                    "RestApiId": {"Ref": "ApiGatewayRestApi"},
                    "AuthorizationType": "COGNITO_USER_POOLS" if ep.auth == "cognito" else "NONE",
                    "Integration": {
                        "Type": "AWS_PROXY",
                        "IntegrationHttpMethod": "POST",
                        "Uri": {
                            "Fn::Sub": (
                                "arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31"
                                f"/functions/${{{ep.handler.replace('.', '_').upper()}_LAMBDA_ARN}}/invocations"
                            )
                        },
                    },
                }
            }
        return resources


def generate_cors_preflight(allowed_origins: str = "*", allowed_methods: str = "GET,POST,PUT,DELETE,OPTIONS"):
    """Generate CORS OPTIONS method for API Gateway."""
    return {
        "Type": "AWS::ApiGateway::Method",
        "Properties": {
            "HttpMethod": "OPTIONS",
            "ResourceId": {"Ref": "ApiGatewayRootResourceId"},
            "RestApiId": {"Ref": "ApiGatewayRestApi"},
            "AuthorizationType": "NONE",
            "Integration": {
                "Type": "MOCK",
                "IntegrationResponses": [{
                    "StatusCode": "200",
                    "ResponseParameters": {
                        "method.response.header.Access-Control-Allow-Headers": "'Content-Type,X-Amz-Date,Authorization,X-Api-Key'",
                        "method.response.header.Access-Control-Allow-Methods": f"'{allowed_methods}'",
                        "method.response.header.Access-Control-Allow-Origin": f"'{allowed_origins}'",
                    },
                }],
            },
            "MethodResponses": [{
                "StatusCode": "200",
                "ResponseModels": {"application/json": "Empty"},
                "ResponseParameters": {
                    "method.response.header.Access-Control-Allow-Headers": True,
                    "method.response.header.Access-Control-Allow-Methods": True,
                    "method.response.header.Access-Control-Allow-Origin": True,
                },
            }],
        }
    }


if __name__ == "__main__":
    config = APIGatewayConfig()
    print(f"Configured {len(config.endpoints)} API endpoints")
    for ep in config.endpoints:
        print(f"  {ep.method:6} {ep.path:40} → {ep.handler} (auth={ep.auth})")