"""
Highly Available Web Application — CloudFormation Template Generator
Generates production-grade HA infrastructure: ALB, ASG, RDS, ElastiCache, CloudFront.
"""
import json
from typing import Optional


# VPC Configuration
VPC_CONFIG = {
    "cidr_block": "10.0.0.0/16",
    "public_subnets": ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"],
    "private_subnets": ["10.0.10.0/24", "10.0.11.0/24", "10.0.12.0/24"],
    "database_subnets": ["10.0.20.0/24", "10.0.21.0/24", "10.0.22.0/24"],
    "availability_zones": ["eu-west-2a", "eu-west-2b", "eu-west-2c"],
}


def generate_alb_listener(alb_arn: str, target_group_arn: str, certificate_arn: str,
                           port: int = 443) -> dict:
    """Generate ALB HTTPS listener with redirect from HTTP."""
    https_listener = {
        "Type": "AWS::ElasticLoadBalancingV2::Listener",
        "Properties": {
            "DefaultActions": [{
                "Type": "forward",
                "TargetGroupArn": target_group_arn,
            }],
            "LoadBalancerArn": alb_arn,
            "Port": port,
            "Protocol": "HTTPS",
            "Certificates": [{"CertificateArn": certificate_arn}],
            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
        },
    }

    http_redirect = {
        "Type": "AWS::ElasticLoadBalancingV2::Listener",
        "Properties": {
            "DefaultActions": [{
                "Type": "redirect",
                "RedirectConfig": {
                    "Protocol": "HTTPS",
                    "Port": "443",
                    "Host": "#{host}",
                    "Path": "/#{path}",
                    "Query": "#{query}",
                    "StatusCode": "HTTP_301",
                },
            }],
            "LoadBalancerArn": alb_arn,
            "Port": 80,
            "Protocol": "HTTP",
        },
    }

    return {"HttpsListener": https_listener, "HttpRedirect": http_redirect}


def generate_asg_config(launch_template_id: str, instance_type: str = "t3.medium",
                         min_size: int = 2, max_size: int = 6, desired: int = 3) -> dict:
    """Generate Auto Scaling Group with scaling policies."""
    asg = {
        "Type": "AWS::AutoScaling::AutoScalingGroup",
        "Properties": {
            "LaunchTemplate": {
                "LaunchTemplateId": launch_template_id,
                "Version": {"Fn::GetAtt": ["LaunchTemplate", "LatestVersionNumber"]},
            },
            "MinSize": str(min_size),
            "MaxSize": str(max_size),
            "DesiredCapacity": str(desired),
            "VPCZoneIdentifier": [
                {"Ref": "PrivateSubnet1"},
                {"Ref": "PrivateSubnet2"},
                {"Ref": "PrivateSubnet3"},
            ],
            "HealthCheckType": "ELB",
            "HealthCheckGracePeriod": 300,
            "TargetGroupARNs": [{"Ref": "WebTargetGroup"}],
            "Tags": [
                {"Key": "Name", "Value": "snaptorent-web", "PropagateAtLaunch": True},
                {"Key": "Environment", "Value": {"Ref": "Environment"}, "PropagateAtLaunch": True},
            ],
        },
    }

    scale_up = {
        "Type": "AWS::AutoScaling::ScalingPolicy",
        "Properties": {
            "AutoScalingGroupName": {"Ref": "WebServerASG"},
            "PolicyType": "TargetTrackingScaling",
            "TargetTrackingConfiguration": {
                "PredefinedMetricSpecification": {
                    "PredefinedMetricType": "ASGAverageCPUUtilization",
                },
                "TargetValue": 70.0,
                "Cooldown": 300,
            },
        },
    }

    return {"ASG": asg, "ScaleUpPolicy": scale_up}


def generate_rds_config(db_name: str = "snaptorent", instance_class: str = "db.t3.medium",
                          multi_az: bool = True, storage_encrypted: bool = True) -> dict:
    """Generate RDS Multi-AZ configuration."""
    return {
        "Type": "AWS::RDS::DBInstance",
        "Properties": {
            "DBInstanceIdentifier": f"snaptorent-{db_name}",
            "DBName": db_name,
            "Engine": "postgres",
            "EngineVersion": "15.4",
            "DBInstanceClass": instance_class,
            "AllocatedStorage": "100",
            "StorageType": "gp3",
            "StorageEncrypted": storage_encrypted,
            "MultiAZ": multi_az,
            "MasterUsername": {"Fn::Sub": "{{resolve:secretsmanager:${DBSecret}::username}}"},
            "MasterUserPassword": {"Fn::Sub": "{{resolve:secretsmanager:${DBSecret}::password}}"},
            "VPCSecurityGroups": [{"Ref": "DBSecurityGroup"}],
            "DBSubnetGroupName": {"Ref": "DBSubnetGroup"},
            "BackupRetentionPeriod": 14,
            "PreferredBackupWindow": "03:00-04:00",
            "PreferredMaintenanceWindow": "sun:04:00-sun:05:00",
            "MonitoringInterval": 60,
            "MonitoringRoleArn": {"Fn::GetAtt": ["RDSMonitoringRole", "Arn"]},
            "EnablePerformanceInsights": True,
            "PerformanceInsightsRetentionPeriod": 7,
            "DeletionProtection": True,
            "DeleteAutomatedBackups": False,
        },
        "DependsOn": ["DBSubnetGroup", "RDSSecret"],
    }


def generate_cloudfront_distribution(origin_domain: str, certificate_arn: str) -> dict:
    """Generate CloudFront CDN distribution."""
    return {
        "Type": "AWS::CloudFront::Distribution",
        "Properties": {
            "DistributionConfig": {
                "Enabled": True,
                "Origins": [{
                    "Id": "ALBOrigin",
                    "DomainName": origin_domain,
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "https-only",
                        "OriginSslProtocols": ["TLSv1.2"],
                    },
                    "OriginCustomHeaders": [{
                        "HeaderName": "X-CloudFront-Auth",
                        "HeaderValue": {"Fn::Sub": "{{resolve:secretsmanager:${CloudFrontSecret}}}"},
                    }],
                }],
                "DefaultCacheBehavior": {
                    "TargetOriginId": "ALBOrigin",
                    "ViewerProtocolPolicy": "redirect-to-https",
                    "AllowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    "CachedMethods": ["GET", "HEAD"],
                    "ForwardedValues": {
                        "QueryString": True,
                        "Cookies": {"Forward": "all"},
                        "Headers": ["Host", "Authorization", "X-API-Key"],
                    },
                    "MinTTL": 0,
                    "DefaultTTL": 3600,
                    "MaxTTL": 86400,
                    "Compress": True,
                    "LambdaFunctionAssociations": [{
                        "EventType": "origin-request",
                        "LambdaFunctionARN": {"Ref": "EdgeLambdaArn"},
                    }],
                },
                "ViewerCertificate": {
                    "AcmCertificateArn": certificate_arn,
                    "SslSupportMethod": "sni-only",
                    "MinimumProtocolVersion": "TLSv1.2_2021",
                },
                "PriceClass": "PriceClass_100",
                "WAFWebACLRef": {"Ref": "WebACL"},
                "Logging": {
                    "Bucket": {"Fn::Sub": "${LoggingBucket}.s3.amazonaws.com"},
                    "Prefix": "cloudfront/",
                    "IncludeCookies": False,
                },
                "CustomErrorResponses": [{
                    "ErrorCode": 503,
                    "ResponseCode": 200,
                    "ResponsePagePath": "/maintenance.html",
                    "ErrorCachingMinTTL": 30,
                }],
                "DefaultRootObject": "index.html",
            },
        },
    }


def generate_full_template() -> dict:
    """Generate the complete CloudFormation template for the HA web application."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "SnapToRent Highly Available Web Application Infrastructure",
        "Parameters": {
            "Environment": {
                "Type": "String",
                "Default": "production",
                "AllowedValues": ["staging", "production"],
                "Description": "Environment name",
            },
            "SSLCertificateArn": {
                "Type": "String",
                "Description": "ARN of the SSL certificate in ACM",
            },
        },
        "Resources": {
            "VPC": {"Type": "AWS::EC2::VPC", "Properties": {
                "CidrBlock": VPC_CONFIG["cidr_block"],
                "EnableDnsHostnames": True,
                "EnableDnsSupport": True,
                "Tags": [{"Key": "Name", "Value": "snaptorent-vpc"}],
            }},
            **generate_alb_listener({"Ref": "ApplicationLoadBalancer"}, {"Ref": "WebTargetGroup"}, {"Ref": "SSLCertificateArn"}),
            **generate_asg_config({"Ref": "LaunchTemplate"}),
            "Database": generate_rds_config(),
            "CloudFront": generate_cloudfront_distribution(
                {"Fn::GetAtt": ["ApplicationLoadBalancer", "DNSName"]},
                {"Ref": "SSLCertificateArn"},
            ),
        },
        "Outputs": {
            "LoadBalancerDNS": {"Value": {"Fn::GetAtt": ["ApplicationLoadBalancer", "DNSName"]}},
            "CloudFrontURL": {"Value": {"Fn::GetAtt": ["CloudFront", "DomainName"]}},
            "RDSEndpoint": {"Value": {"Fn::GetAtt": ["Database", "Endpoint"]}},
        },
    }


if __name__ == "__main__":
    template = generate_full_template()
    print(json.dumps(template, indent=2, default=str))
    print(f"\nTemplate has {len(template['Resources'])} resources")