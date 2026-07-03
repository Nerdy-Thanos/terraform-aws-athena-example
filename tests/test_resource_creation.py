"""
Integration tests for the Athena Terraform module (data catalog, database,
workgroup defined in main.tf).

These connect to the REAL AWS account where the resources are deployed. They check:
  1. Each resource exists with the configuration main.tf declares
  2. The three resources actually work together (a real query executes)

Resource names come from environment variables (defaults match
terraform.tfvars.example) -- override them to match whatever you've deployed.
ATHENA_DB_BUCKET has no sensible default and must always be set.

Setup
-----
  pip install boto3 pytest

  Requires AWS credentials for the target account (same one the resources
  were deployed into).

Run
---
  AWS_REGION=us-east-1 ATHENA_DB_BUCKET=<your-results-bucket> pytest -v
"""

import os
import time

import boto3
import pytest

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
QUERY_TIMEOUT_SECONDS = 60

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Test environment config -- env vars, defaulting to terraform.tfvars.example
# ---------------------------------------------------------------------------
CONFIG = {
    "athena_data_catalog_name": os.environ.get("ATHENA_DATA_CATALOG_NAME", "testSandboxCatalog"),
    "athena_db_name": os.environ.get("ATHENA_DB_NAME", "test_sandbox_athena_db"),
    "athena_db_bucket": os.environ.get("ATHENA_DB_BUCKET", "REPLACE_ME"),
    "athena_workgroup_name": os.environ.get("ATHENA_WORKGROUP_NAME", "testSandboxAthenaWorkgroup"),
}


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
def account_id():
    return boto3.client("sts", region_name=AWS_REGION).get_caller_identity()["Account"]


@pytest.fixture(scope="session")
def athena_client():
    return boto3.client("athena", region_name=AWS_REGION)


@pytest.fixture(scope="session")
def glue_client():
    return boto3.client("glue", region_name=AWS_REGION)


@pytest.fixture(scope="session")
def s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def _wait_for_query(athena_client, execution_id, timeout=QUERY_TIMEOUT_SECONDS):
    elapsed = 0
    while elapsed < timeout:
        resp = athena_client.get_query_execution(QueryExecutionId=execution_id)
        execution = resp["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return execution
        time.sleep(2)
        elapsed += 2
    pytest.fail(f"Query {execution_id} did not finish within {timeout}s")


def _run_query(athena_client, query, config, workgroup=None):
    execution_id = athena_client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": config["athena_db_name"],
            "Catalog": config["athena_data_catalog_name"],
        },
        WorkGroup=workgroup or config["athena_workgroup_name"],
    )["QueryExecutionId"]
    return _wait_for_query(athena_client, execution_id)


# ---------------------------------------------------------------------------
# aws_athena_data_catalog
# ---------------------------------------------------------------------------

class TestAthenaDataCatalog:
    def test_athena_data_catalog_exists_and_is_glue_type(self, athena_client, config):
        resp = athena_client.get_data_catalog(Name=config["athena_data_catalog_name"])
        catalog = resp["DataCatalog"]
        assert catalog["Name"] == config["athena_data_catalog_name"]
        assert catalog["Type"] == "GLUE"

    def test_catalog_id_parameter_matches_account(self, athena_client, config, account_id):
        resp = athena_client.get_data_catalog(Name=config["athena_data_catalog_name"])
        params = resp["DataCatalog"].get("Parameters", {})
        assert params.get("catalog-id") == account_id


# ---------------------------------------------------------------------------
# aws_athena_database
# ---------------------------------------------------------------------------

class TestAthenaDatabase:
    def test_athena_database_exists_in_glue(self, glue_client, config):
        # aws_athena_database runs a CREATE DATABASE DDL that lands in Glue.
        resp = glue_client.get_database(Name=config["athena_db_name"])
        assert resp["Database"]["Name"] == config["athena_db_name"]

    def test_athena_database_visible_via_athena(self, athena_client, config):
        resp = athena_client.list_databases(
            CatalogName=config["athena_data_catalog_name"]
        )
        names = [db["Name"] for db in resp["DatabaseList"]]
        assert config["athena_db_name"] in names

# ---------------------------------------------------------------------------
# aws_athena_workgroup
# ---------------------------------------------------------------------------

class TestAthenaWorkgroup:
    @pytest.fixture(scope="class")
    def workgroup(self, athena_client, config):
        resp = athena_client.get_work_group(WorkGroup=config["athena_workgroup_name"])
        return resp["WorkGroup"]

    def test_athena_workgroup_exists(self, workgroup, config):
        assert workgroup["Name"] == config["athena_workgroup_name"]

    def test_engine_version_3(self, workgroup):
        engine = workgroup["Configuration"]["EngineVersion"]
        assert engine["SelectedEngineVersion"] == "Athena engine version 3"

    def test_enforce_workgroup_configuration(self, workgroup):
        assert workgroup["Configuration"]["EnforceWorkGroupConfiguration"] is True

    def test_cloudwatch_metrics_enabled(self, workgroup):
        assert workgroup["Configuration"]["PublishCloudWatchMetricsEnabled"] is True

    def test_output_location(self, workgroup, config):
        output_location = workgroup["Configuration"]["ResultConfiguration"]["OutputLocation"]
        assert output_location == f"s3://{config['athena_db_bucket']}/output/"

    def test_expected_bucket_owner(self, workgroup, account_id):
        result_config = workgroup["Configuration"]["ResultConfiguration"]
        assert result_config["ExpectedBucketOwner"] == account_id

    def test_acl_bucket_owner_full_control(self, workgroup):
        acl = workgroup["Configuration"]["ResultConfiguration"]["AclConfiguration"]
        assert acl["S3AclOption"] == "BUCKET_OWNER_FULL_CONTROL"

    def test_encryption_option_is_sse_kms(self, workgroup):
        enc = workgroup["Configuration"]["ResultConfiguration"]["EncryptionConfiguration"]
        assert enc["EncryptionOption"] == "SSE_KMS"

# ---------------------------------------------------------------------------
# Functional: catalog + database + workgroup actually work together
# ---------------------------------------------------------------------------

class TestAthenaFunctionality:
    def test_simple_query_executes_successfully(self, athena_client, config):
        execution = _run_query(athena_client, "SELECT 1", config)
        assert execution["Status"]["State"] == "SUCCEEDED", (
            execution["Status"].get("StateChangeReason")
        )

    def test_query_results_land_in_expected_s3_location(
        self, athena_client, s3_client, config
    ):
        execution = _run_query(athena_client, "SELECT 1", config)
        output_location = execution["ResultConfiguration"]["OutputLocation"]
        bucket = config["athena_db_bucket"]

        assert output_location.startswith(f"s3://{bucket}/output/")

        key = output_location.replace(f"s3://{bucket}/", "")
        head = s3_client.head_object(Bucket=bucket, Key=key)
        # SSE_KMS surfaces as aws:kms in the S3 object's encryption header.
        assert head.get("ServerSideEncryption") == "aws:kms"

    def test_show_tables_in_athena_database(self, athena_client, config):
        """Confirms the database is actually queryable through the workgroup,
        not just present in the Glue catalog."""
        execution = _run_query(
            athena_client, f"SHOW TABLES IN {config['athena_db_name']}", config
        )
        assert execution["Status"]["State"] == "SUCCEEDED", (
            execution["Status"].get("StateChangeReason")
        )

    def test_athena_workgroup_enforcement(self, athena_client, config):
        """
        enforce_workgroup_configuration = true means the workgroup's own
        settings should win even if a query tries to override them. This
        confirms enforcement is actually active, not just set in config.
        """
        execution_id = athena_client.start_query_execution(
            QueryString="SELECT 1",
            QueryExecutionContext={
                "Database": config["athena_db_name"],
                "Catalog": config["athena_data_catalog_name"],
            },
            WorkGroup=config["athena_workgroup_name"],
            ResultConfiguration={
                "OutputLocation": f"s3://{config['athena_db_bucket']}/should-be-ignored/"
            },
        )["QueryExecutionId"]
        execution = _wait_for_query(athena_client, execution_id)

        assert execution["Status"]["State"] == "SUCCEEDED"
        actual_location = execution["ResultConfiguration"]["OutputLocation"]
        assert "should-be-ignored" not in actual_location
        assert actual_location.startswith(
            f"s3://{config['athena_db_bucket']}/output/"
        )