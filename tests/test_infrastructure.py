"""
Integration test for HA Web Application — CloudFormation template validation
"""
import json
import pytest

from infrastructure import (
    generate_full_template, generate_alb_config, generate_asg_config,
    generate_rds_config, generate_cloudfront_distribution, VPC_CONFIG
)


class TestCloudFormationTemplate:
    def test_generates_valid_json(self):
        template = generate_full_template()
        output = json.dumps(template, indent=2)
        parsed = json.loads(output)
        assert "Resources" in parsed
        assert "Parameters" in parsed

    def test_has_required_outputs(self):
        template = generate_full_template()
        assert "LoadBalancerDNS" in template["Outputs"]
        assert "CloudFrontURL" in template["Outputs"]
        assert "RDSEndpoint" in template["Outputs"]

    def test_vpc_config_has_three_azs(self):
        assert len(VPC_CONFIG["availability_zones"]) == 3
        assert len(VPC_CONFIG["public_subnets"]) == 3
        assert len(VPC_CONFIG["private_subnets"]) == 3
        assert len(VPC_CONFIG["database_subnets"]) == 3

    def test_rds_multi_az(self):
        rds = generate_rds_config()
        assert rds["Properties"]["MultiAZ"] is True
        assert rds["Properties"]["StorageEncrypted"] is True
        assert rds["Properties"]["DeletionProtection"] is True

    def test_rds_postgres_engine(self):
        rds = generate_rds_config()
        assert rds["Properties"]["Engine"] == "postgres"
        assert rds["Properties"]["EngineVersion"] == "15.4"

    def test_asg_config_defaults(self):
        asg = generate_asg_config("lt-12345")
        assert asg["ASG"]["Properties"]["MinSize"] == "2"
        assert asg["ASG"]["Properties"]["MaxSize"] == "6"
        assert asg["ASG"]["Properties"]["DesiredCapacity"] == "3"

    def test_cloudfront_https_only(self):
        cf = generate_cloudfront_distribution("api.example.com", "arn:aws:acm:...")
        behavior = cf["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]
        assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])