"""
BDD-style integration tests for the *secure Athena access path*.

Where test_resource_creation.py proves the Athena resources exist, this suite proves the
end-to-end enablement we built is locked down AND works:

  Feature: A workload (Lambda / EKS pod) can query Athena programmatically, privately, and
           with least privilege.

Two flavours, both against REAL AWS (no mocks):

  * @pytest.mark.security   - configuration/posture assertions: the Lambda is private, the
                              network path is tightly scoped, and the IAM policies are
                              least-privilege. The IAM checks parse the *deployed* policy
                              documents (get_role_policy) and assert their structure
                              directly - scoped ARNs, the aws:CalledVia guardrail on every
                              Glue/S3/KMS statement, no kms:Encrypt, etc.
  * @pytest.mark.functional - invokes the deployed Lambda and asserts it really runs queries.

Setup
-----
  pip install boto3 pytest
  AWS credentials for the target account, with permission to: lambda:GetFunctionConfiguration
  / lambda:InvokeFunction, iam:GetRole / iam:ListRolePolicies / iam:GetRolePolicy,
  ec2:Describe*, eks:DescribeCluster.

Run
---
  ATHENA_DB_BUCKET=<your-results-bucket> pytest -v    # from this directory

Resource names come from environment variables (defaults match
terraform.tfvars.example). The EKS tests are skipped unless EKS_CLUSTER_NAME is set
(matching the optional eks_cluster_name Terraform variable).

The @security / @functional markers are just labels for optional filtering (e.g.
`pytest -m security`); they do NOT gate execution, so a plain run includes everything.
"""

import json
import os

import boto3
import pytest

pytestmark = pytest.mark.integration

REGION = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Deployed-resource identifiers -- env vars, defaulting to terraform.tfvars.example
# ---------------------------------------------------------------------------
CONFIG = {
    "athena_data_catalog_name": os.environ.get("ATHENA_DATA_CATALOG_NAME", "testSandboxCatalog"),
    "athena_db_name": os.environ.get("ATHENA_DB_NAME", "test_sandbox_athena_db"),
    "athena_db_bucket": os.environ.get("ATHENA_DB_BUCKET", "REPLACE_ME"),
    "athena_data_bucket": os.environ.get("ATHENA_DATA_BUCKET")
    or os.environ.get("ATHENA_DB_BUCKET", "REPLACE_ME"),
    "athena_workgroup_name": os.environ.get("ATHENA_WORKGROUP_NAME", "testSandboxAthenaWorkgroup"),
    "lambda_function_name": "athena_query_runner",
    "lambda_role_name": "athena_lambda_execution_role",
    "eks_role_name": "athena_eks_query_role",
    "eks_cluster_name": os.environ.get("EKS_CLUSTER_NAME", ""),
    "eks_namespace": os.environ.get("K8S_NAMESPACE", "default"),
    "eks_service_account": os.environ.get("K8S_SERVICE_ACCOUNT_NAME", "athena-query-runner"),
    "vpc_name": os.environ.get("VPC_NAME", "data-vpc"),
}

# The EKS/IRSA resources are optional (created only when eks_cluster_name is set in
# Terraform); mirror that here so the suite passes on a Lambda-only deployment.
requires_eks = pytest.mark.skipif(
    not CONFIG["eks_cluster_name"],
    reason="EKS test disabled - set EKS_CLUSTER_NAME to run against a deployed IRSA role",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def config():
    placeholders = [k for k, v in CONFIG.items() if v == "REPLACE_ME"]
    if placeholders:
        pytest.fail(f"Set environment variable(s): {[k.upper() for k in placeholders]}")
    return CONFIG


@pytest.fixture(scope="session")
def lambda_client():
    return boto3.client("lambda", region_name=REGION)


@pytest.fixture(scope="session")
def ec2_client():
    return boto3.client("ec2", region_name=REGION)


@pytest.fixture(scope="session")
def iam_client():
    return boto3.client("iam", region_name=REGION)


@pytest.fixture(scope="session")
def eks_client():
    return boto3.client("eks", region_name=REGION)


@pytest.fixture(scope="session")
def lambda_config(lambda_client, config):
    return lambda_client.get_function_configuration(FunctionName=config["lambda_function_name"])


@pytest.fixture(scope="session")
def vpc_id(ec2_client, config):
    vpcs = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [config["vpc_name"]]}]
    )["Vpcs"]
    assert len(vpcs) == 1, f"expected exactly one VPC named {config['vpc_name']}, got {len(vpcs)}"
    return vpcs[0]["VpcId"]


@pytest.fixture(scope="session")
def athena_endpoint(ec2_client, vpc_id):
    endpoints = ec2_client.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.athena"]},
        ]
    )["VpcEndpoints"]
    assert len(endpoints) == 1, f"expected exactly one Athena interface endpoint, got {len(endpoints)}"
    return endpoints[0]


@pytest.fixture(scope="session")
def eks_cluster(eks_client, config):
    return eks_client.describe_cluster(name=config["eks_cluster_name"])["cluster"]


@pytest.fixture(scope="session")
def lambda_role_statements(iam_client, config):
    return _role_inline_statements(iam_client, config["lambda_role_name"])


@pytest.fixture(scope="session")
def eks_role_statements(iam_client, config):
    return _role_inline_statements(iam_client, config["eks_role_name"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _as_list(x):
    return x if isinstance(x, list) else [x]


def _role_inline_statements(iam_client, role_name):
    """All statements across the role's inline policies (boto3 returns docs already decoded)."""
    stmts = []
    for name in iam_client.list_role_policies(RoleName=role_name)["PolicyNames"]:
        doc = iam_client.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
        stmts.extend(_as_list(doc["Statement"]))
    return stmts


def _by_sid(statements, sid):
    for s in statements:
        if s.get("Sid") == sid:
            return s
    return None


def _has_calledvia(statement):
    """True if the statement is gated on aws:CalledVia == athena.amazonaws.com."""
    for _op, kv in statement.get("Condition", {}).items():
        for key, values in kv.items():
            if key.lower() == "aws:calledvia" and "athena.amazonaws.com" in _as_list(values):
                return True
    return False


def _sg_rules(ec2_client, group_ids):
    return ec2_client.describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": list(group_ids)}]
    )["SecurityGroupRules"]


def _has_ingress_from_sg(rules, source_sg, port=443):
    for r in rules:
        if (
            not r["IsEgress"]
            and r.get("ReferencedGroupInfo", {}).get("GroupId") == source_sg
            and r.get("FromPort") == port
            and r.get("ToPort") == port
            and r.get("IpProtocol") == "tcp"
        ):
            return True
    return False


def _invoke(lambda_client, function_name, payload):
    resp = lambda_client.invoke(
        FunctionName=function_name,
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(resp["Payload"].read())
    return resp, body


# ===========================================================================
# Feature: the Lambda is deployed privately and configured for Athena
# ===========================================================================
@pytest.mark.security
class TestLambdaPrivateConfiguration:
    def test_lambda_runs_inside_the_vpc(self, lambda_config):
        """Given the query Lambda, Then it must be attached to the VPC (has subnets + SG),
        so its traffic to Athena is forced onto the private network."""
        vpc = lambda_config.get("VpcConfig") or {}
        assert vpc.get("SubnetIds"), "Lambda is not attached to any VPC subnets (runs on public internet)"
        assert vpc.get("SecurityGroupIds"), "Lambda has no security group"

    def test_lambda_subnets_are_the_endpoint_subnets(self, lambda_config, athena_endpoint):
        """Given the Lambda, Then its subnets are within the Athena endpoint's subnets,
        guaranteeing same-AZ reachability of the endpoint ENIs."""
        lambda_subnets = set(lambda_config["VpcConfig"]["SubnetIds"])
        endpoint_subnets = set(athena_endpoint["SubnetIds"])
        # Guard: issubset() of an empty set is vacuously True.
        assert lambda_subnets, "Lambda reported no subnets"
        assert endpoint_subnets, "endpoint reported no subnets"
        assert lambda_subnets.issubset(endpoint_subnets), (
            f"Lambda subnets {lambda_subnets} not within endpoint subnets {endpoint_subnets}"
        )

    def test_lambda_environment_is_configured(self, lambda_config, config):
        """Given the Lambda, Then database/catalog/workgroup come from env (query-only from
        the event), and AWS_REGION is NOT hard-set (it is Lambda-reserved)."""
        env = lambda_config["Environment"]["Variables"]
        assert env["ATHENA_DATABASE"] == config["athena_db_name"]
        assert env["ATHENA_CATALOG"] == config["athena_data_catalog_name"]
        assert env["ATHENA_WORKGROUP"] == config["athena_workgroup_name"]
        assert "AWS_REGION" not in env, "AWS_REGION must not be set explicitly (reserved key)"

    def test_lambda_uses_the_expected_execution_role(self, lambda_config, config):
        assert lambda_config["Role"].endswith(f":role/{config['lambda_role_name']}")

    def test_lambda_is_not_publicly_invokable_via_url(self, lambda_client, config):
        """Given the Lambda, Then it must NOT expose a public Function URL."""
        with pytest.raises(lambda_client.exceptions.ResourceNotFoundException):
            lambda_client.get_function_url_config(FunctionName=config["lambda_function_name"])


# ===========================================================================
# Feature: the network path is private and tightly scoped
# ===========================================================================
@pytest.mark.security
class TestNetworkIsPrivate:
    def test_athena_endpoint_is_interface_type_with_private_dns(self, athena_endpoint):
        """Given the Athena endpoint, Then it is an Interface endpoint with private DNS,
        so athena.<region>.amazonaws.com resolves to it (no internet path)."""
        assert athena_endpoint["VpcEndpointType"] == "Interface"
        assert athena_endpoint["PrivateDnsEnabled"] is True

    def test_lambda_egress_is_restricted_to_the_endpoint(self, ec2_client, lambda_config, athena_endpoint):
        """Given the Lambda SG, Then its ONLY egress is 443 to the endpoint SG - no
        0.0.0.0/0. Combined with a successful query (functional suite), this proves the
        query can only have travelled through the VPC endpoint."""
        lambda_sgs = lambda_config["VpcConfig"]["SecurityGroupIds"]
        endpoint_sgs = {g["GroupId"] for g in athena_endpoint["Groups"]}
        egress = [r for r in _sg_rules(ec2_client, lambda_sgs) if r["IsEgress"]]

        assert egress, "Lambda SG has no egress rules at all"
        for r in egress:
            assert r.get("CidrIpv4") != "0.0.0.0/0", "Lambda SG allows open internet egress"
            assert r.get("CidrIpv6") != "::/0", "Lambda SG allows open internet egress (v6)"
            assert r["IpProtocol"] == "tcp" and r["FromPort"] == 443 and r["ToPort"] == 443
            assert r.get("ReferencedGroupInfo", {}).get("GroupId") in endpoint_sgs, (
                "Lambda egress does not target the Athena endpoint SG"
            )

    def test_endpoint_accepts_the_lambda_sg(self, ec2_client, lambda_config, athena_endpoint):
        """Given the endpoint SG, Then it allows 443 ingress from the Lambda SG."""
        endpoint_sgs = [g["GroupId"] for g in athena_endpoint["Groups"]]
        lambda_sg = lambda_config["VpcConfig"]["SecurityGroupIds"][0]
        assert _has_ingress_from_sg(_sg_rules(ec2_client, endpoint_sgs), lambda_sg), (
            "Athena endpoint SG does not allow 443 from the Lambda SG"
        )

    @requires_eks
    def test_endpoint_accepts_the_eks_cluster_sg(self, ec2_client, athena_endpoint, eks_cluster):
        """Given the endpoint SG, Then it allows 443 ingress from the EKS cluster SG."""
        endpoint_sgs = [g["GroupId"] for g in athena_endpoint["Groups"]]
        cluster_sg = eks_cluster["resourcesVpcConfig"]["clusterSecurityGroupId"]
        assert _has_ingress_from_sg(_sg_rules(ec2_client, endpoint_sgs), cluster_sg), (
            "Athena endpoint SG does not allow 443 from the EKS cluster SG"
        )


# ===========================================================================
# Feature: the Lambda role is least-privilege (deployed policy is asserted directly)
# ===========================================================================
@pytest.mark.security
class TestLambdaRoleLeastPrivilege:
    def test_athena_scoped_to_its_workgroup_and_not_calledvia(self, lambda_role_statements, config):
        """Direct Athena calls: scoped to the one workgroup (not '*') and NOT CalledVia-gated
        (they are the caller's own initial calls)."""
        s = _by_sid(lambda_role_statements, "AthenaWorkgroupAccess")
        assert s is not None
        resources = _as_list(s["Resource"])
        assert "*" not in resources
        assert any(r.endswith(f"workgroup/{config['athena_workgroup_name']}") for r in resources)
        assert "athena:StartQueryExecution" in _as_list(s["Action"])
        assert not _has_calledvia(s)

    def test_named_data_catalog_permission_is_present_and_scoped(self, lambda_role_statements, config):
        s = _by_sid(lambda_role_statements, "AthenaDataCatalogAccess")
        assert s is not None
        assert "athena:GetDataCatalog" in _as_list(s["Action"])
        assert any(r.endswith(f"datacatalog/{config['athena_data_catalog_name']}") for r in _as_list(s["Resource"]))

    def test_glue_read_is_scoped_to_the_database_and_calledvia_gated(self, lambda_role_statements, config):
        """The Glue guardrail: read is limited to catalog + this database + its tables
        (never '*'), and only usable via Athena (aws:CalledVia)."""
        s = _by_sid(lambda_role_statements, "GlueCatalogReadAccess")
        assert s is not None
        resources = _as_list(s["Resource"])
        assert "*" not in resources, "Glue read must not be '*'"
        assert any(r.endswith(":catalog") for r in resources)
        assert any(r.endswith(f"database/{config['athena_db_name']}") for r in resources)
        assert any(f"table/{config['athena_db_name']}/" in r for r in resources)
        assert _has_calledvia(s), "Glue read must be gated on aws:CalledVia"

    def test_glue_write_is_granted_scoped_and_calledvia_gated(self, lambda_role_statements):
        """Read+write scope: write actions granted, still scoped (not '*') and CalledVia-gated."""
        s = _by_sid(lambda_role_statements, "GlueCatalogWriteAccess")
        assert s is not None
        actions = _as_list(s["Action"])
        assert "glue:CreateTable" in actions and "glue:UpdateTable" in actions
        assert "*" not in _as_list(s["Resource"])
        assert _has_calledvia(s)

    def test_results_s3_has_multipart_actions_and_calledvia(self, lambda_role_statements):
        """The multipart trap: large result writes need Abort/ListMultipartUploadParts and
        GetBucketLocation - assert they are present, and the statement is CalledVia-gated."""
        s = _by_sid(lambda_role_statements, "AthenaQueryResultsS3Access")
        assert s is not None
        actions = _as_list(s["Action"])
        for a in ("s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:GetBucketLocation"):
            assert a in actions, f"results-bucket write missing {a}"
        assert _has_calledvia(s)

    def test_data_s3_write_is_granted_scoped_and_calledvia(self, lambda_role_statements, config):
        s = _by_sid(lambda_role_statements, "AthenaDataS3Access")
        assert s is not None
        actions = _as_list(s["Action"])
        assert "s3:PutObject" in actions and "s3:DeleteObject" in actions
        resources = _as_list(s["Resource"])
        assert resources and all(config["athena_data_bucket"] in r for r in resources)
        assert _has_calledvia(s)

    def test_kms_has_no_encrypt_and_is_calledvia_gated(self, lambda_role_statements):
        s = _by_sid(lambda_role_statements, "AthenaKmsAccess")
        assert s is not None
        actions = set(_as_list(s["Action"]))
        assert actions == {"kms:Decrypt", "kms:GenerateDataKey"}, actions
        assert "kms:Encrypt" not in actions
        assert _has_calledvia(s)

    def test_calledvia_guardrail_covers_every_data_plane_statement(self, lambda_role_statements):
        """Every Glue/S3/KMS statement must be CalledVia-gated; the direct Athena/logs/ENI
        statements must NOT be (those are the Lambda's own calls, not made 'via' Athena)."""
        data_plane = {
            "GlueCatalogReadAccess", "GlueCatalogWriteAccess",
            "AthenaQueryResultsS3Access", "AthenaDataS3Access", "AthenaKmsAccess",
        }
        direct = {"AthenaWorkgroupAccess", "AthenaDataCatalogAccess", "CloudWatchLogs", "LambdaVpcNetworking"}
        # Guard: without this, an empty/restructured policy makes the loop below a no-op
        # and the test passes vacuously. Assert the statements we reason about exist.
        found = {s.get("Sid") for s in lambda_role_statements}
        assert data_plane <= found, f"policy missing data-plane statements: {data_plane - found}"
        assert direct <= found, f"policy missing direct statements: {direct - found}"
        for s in lambda_role_statements:
            sid = s.get("Sid")
            if sid in data_plane:
                assert _has_calledvia(s), f"{sid} must be CalledVia-gated"
            elif sid in direct:
                assert not _has_calledvia(s), f"{sid} must NOT be CalledVia-gated"

    def test_no_statement_grants_a_wildcard_action(self, lambda_role_statements):
        """No statement may grant Action '*' (admin-equivalent). Scoped wildcards like
        ec2:Describe* are fine; a bare '*' is not."""
        assert lambda_role_statements, "role has no inline policy statements"
        for s in lambda_role_statements:
            assert "*" not in _as_list(s["Action"]), f"{s.get('Sid')} grants Action '*'"


# ===========================================================================
# Feature: the EKS IRSA role is correctly federated and least-privilege
# ===========================================================================
@pytest.mark.security
@requires_eks
class TestEksIrsaRole:
    def test_trust_policy_is_scoped_to_the_serviceaccount(self, iam_client, config):
        """Given the EKS role, Then it may be assumed ONLY via web identity, ONLY by the
        specific namespace:serviceaccount - not any pod in the cluster."""
        role = iam_client.get_role(RoleName=config["eks_role_name"])["Role"]
        stmts = _as_list(role["AssumeRolePolicyDocument"]["Statement"])
        expected_sub = f"system:serviceaccount:{config['eks_namespace']}:{config['eks_service_account']}"

        assert any("sts:AssumeRoleWithWebIdentity" in _as_list(s["Action"]) for s in stmts)
        assert any(
            "Federated" in s.get("Principal", {}) and "oidc-provider" in s["Principal"]["Federated"]
            for s in stmts
        )
        subs = []
        for s in stmts:
            for cond in s.get("Condition", {}).values():
                subs.extend(v for k, v in cond.items() if k.endswith(":sub"))
        assert expected_sub in subs, f"trust policy sub condition {subs} != {expected_sub}"

    def test_eks_role_shares_the_query_permissions(self, eks_role_statements, config):
        wg = _by_sid(eks_role_statements, "AthenaWorkgroupAccess")
        assert wg is not None and "athena:StartQueryExecution" in _as_list(wg["Action"])
        glue = _by_sid(eks_role_statements, "GlueCatalogReadAccess")
        assert glue is not None and any(
            f"table/{config['athena_db_name']}/" in r for r in _as_list(glue["Resource"])
        )

    def test_eks_role_keeps_the_calledvia_guardrail(self, eks_role_statements):
        for sid in ("GlueCatalogReadAccess", "AthenaQueryResultsS3Access", "AthenaKmsAccess"):
            s = _by_sid(eks_role_statements, sid)
            assert s is not None and _has_calledvia(s), f"{sid} must be CalledVia-gated"

    def test_eks_role_has_no_lambda_only_permissions(self, eks_role_statements):
        """The EKS role shares only the query permissions - it must NOT carry the Lambda's
        CloudWatch Logs / ENI grants (pods log to stdout; nodes handle networking)."""
        # Guard: `not any(...)` over an empty statement list is vacuously True.
        assert eks_role_statements, "EKS role has no inline policy statements"
        assert _by_sid(eks_role_statements, "CloudWatchLogs") is None
        assert _by_sid(eks_role_statements, "LambdaVpcNetworking") is None
        actions = [a for s in eks_role_statements for a in _as_list(s["Action"])]
        assert not any(a.startswith("logs:") for a in actions), "EKS role must not have logs perms"
        assert not any(a.startswith("ec2:") for a in actions), "EKS role must not have ec2/ENI perms"


# ===========================================================================
# Feature: the Lambda actually runs queries end-to-end (real invokes)
# ===========================================================================
@pytest.mark.functional
class TestLambdaQueryFunctionality:
    def test_show_tables_succeeds(self, lambda_client, config):
        """Given the deployed Lambda, When invoked with SHOW TABLES, Then it returns 200
        with the table list - proving the full private+IAM+Athena path works."""
        resp, body = _invoke(
            lambda_client, config["lambda_function_name"],
            {"query": f"SHOW TABLES IN {config['athena_db_name']}"},
        )
        assert "FunctionError" not in resp, body
        assert body["statusCode"] == 200, body
        flat = [cell for row in body["rows"] for cell in row]
        assert body["rowCount"] >= 1 and any(flat), "no tables returned"

    def test_select_returns_result_rows(self, lambda_client, config):
        resp, body = _invoke(lambda_client, config["lambda_function_name"], {"query": "SELECT 1"})
        assert "FunctionError" not in resp, body
        assert body["statusCode"] == 200, body
        assert any("1" in (cell or "") for row in body["rows"] for cell in row)

    def test_missing_query_returns_400(self, lambda_client, config):
        resp, body = _invoke(lambda_client, config["lambda_function_name"], {})
        assert "FunctionError" not in resp, body
        assert body["statusCode"] == 400, body

    def test_invalid_query_returns_500(self, lambda_client, config):
        resp, body = _invoke(
            lambda_client, config["lambda_function_name"],
            {"query": "SELECT * FROM table_that_does_not_exist_zzz"},
        )
        assert "FunctionError" not in resp, body
        assert body["statusCode"] == 500, body
