# AWS Projects Portfolio

[![AWS](https://img.shields.io/badge/AWS-Solutions%20Architect%20Professional-orange)](https://www.credly.com/badges/)
[![Python](https://img.shields.io/badge/Python-3.9+-blue)](https://python.org)
[![CloudFormation](https://img.shields.io/badge/CloudFormation-IaC-green)](https://aws.amazon.com/cloudformation/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-EKS-blue)](https://kubernetes.io)

> A collection of production-grade AWS cloud infrastructure projects demonstrating expertise in serverless architecture, multi-region deployment, data engineering, container orchestration, and high-availability design.

## 🏗️ Projects

### 1. Serverless Microservices Platform
**Path:** `serverless-microservices/`

A fully serverless e-commerce backend built with AWS Lambda, API Gateway, and DynamoDB. Features event-driven architecture with SQS queues for decoupled order processing.

- **Services:** Inventory, Checkout, Shipment, Notification
- **Tech Stack:** Lambda (Python), API Gateway, DynamoDB, SQS, CloudFormation
- **Features:** Auto-scaling, dead-letter queues, CORS handling, deployment automation

### 2. Multi-Region Disaster Recovery
**Path:** `multi-region-disaster-recovery/`

Cross-region disaster recovery solution with automated failover between AWS regions. Implements active-passive DR with CloudFormation stack sets and Route 53 health checks.

- **Regions:** eu-west-2 (primary), eu-west-1 (secondary)
- **Tech Stack:** CloudFormation, Route 53, S3 Cross-Region Replication, Lambda
- **Features:** Automated failover, DR testing scripts, health monitoring, RPO < 15 min

### 3. Data Engineering & Analytics Platform
**Path:** `data-engineering-analytics-platform/`

Real-time data processing pipeline using Kinesis Data Streams, Lambda, and S3. Supports both streaming and batch analytics with Athena integration.

- **Components:** Event Producer, Stream Processor, Analytics Store
- **Tech Stack:** Kinesis, Lambda, S3, Glue, Athena, CloudFormation
- **Features:** Schema validation, dead-letter handling, partition projection, cost monitoring

### 4. Kubernetes DevOps Platform
**Path:** `kubernetes-devops-platform/`

EKS-based container orchestration platform with full CI/CD, RBAC, and auto-scaling. Demonstrates production Kubernetes best practices on AWS.

- **Resources:** Namespace, RBAC, HPA, Ingress, Deployment, Service
- **Tech Stack:** EKS, Helm, CloudFormation, ALB Ingress Controller
- **Features:** Pod security policies, network policies, HPA auto-scaling, IRSA

### 5. Highly Available Web Application
**Path:** `highly-available-web-app/`

Multi-AZ web application with ALB, Auto Scaling Group, and CloudFront CDN. Represents a production-grade HA architecture pattern.

- **Components:** ALB, ASG, CloudFront, RDS Multi-AZ, ElastiCache
- **Tech Stack:** CloudFormation, EC2, RDS, ElastiCache, CloudFront
- **Features:** Blue/green deployment, health checks, connection draining, WAF rules

## 🛠️ Common Infrastructure

Each project includes:
- `config.py` — Configurable parameters (region, instance types, CIDR blocks)
- `deploy.py` — Automated deployment script with dependency checking
- `teardown.py` — Clean teardown with resource verification
- `template.yaml` — CloudFormation infrastructure-as-code template

## 📊 Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│                    AWS Projects                          │
├────────────┬──────────────┬──────────────┬──────────────┤
│ Serverless │ Multi-Region │ Data Eng.    │ K8s DevOps   │
│ Microsvc   │ DR           │ Analytics    │ Platform      │
├────────────┼──────────────┼──────────────┼──────────────┤
│ Lambda     │ CloudForm    │ Kinesis      │ EKS           │
│ API GW     │ Route53      │ Lambda       │ Helm          │
│ DynamoDB   │ S3 Replicate │ S3/Athena    │ RBAC/HPA      │
│ SQS        │ Lambda       │ Glue         │ ALB Ingress   │
└────────────┴──────────────┴──────────────┴──────────────┘
```

## 🔐 Security

All projects follow AWS Well-Architected Framework security best practices:
- IAM least-privilege policies
- VPC with public/private subnets
- Encryption at rest and in transit
- Security groups with minimal ingress rules
- AWS Secrets Manager for credential management

## 👤 Author

**Jehanzeb Bashir**
- AWS Certified Solutions Architect – Professional
- LinkedIn: [jehanzeb-bashir-cloud](https://www.linkedin.com/in/jehanzeb-bashir-cloud/)
- GitHub: [jehanzebbashir33-cmyk](https://github.com/jehanzebbashir33-cmyk)

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.