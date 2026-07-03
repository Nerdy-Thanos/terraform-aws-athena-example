# Athena Enablement — Test Case Catalogue

Real-AWS integration tests (pytest, no mocks) that validate the Athena tenant-enablement
setup: the Athena resources, the secure programmatic-access path (VPC-attached Lambda and
EKS/IRSA pod over the Athena interface VPC endpoint), and least-privilege IAM.

- **Framework:** pytest, BDD style (Given → When → Then)
- **Scope:** hits the real AWS account where the module is deployed (no mocks)
- **Run:** `cd tests && pytest -v` (runs every test in both files; no flags needed)
- **Region:** us-east-1
- **Total:** 42 cases — 16 infrastructure + 26 secure-access/functional

The **Result** column describes what a passing run demonstrates.

---

## File 1 — `test_resource_creation.py` (Infrastructure)

Verifies the Athena data catalog, database, and workgroup exist with the declared
configuration, and that the three work together to run a real query.

### Given: the Athena data catalog

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_athena_data_catalog_exists_and_is_glue_type` | the catalog is fetched via `athena:GetDataCatalog` | it exists and its Type is `GLUE` | Asserts name matches and `Type == "GLUE"`. Result: the named catalog is registered and backed by Glue. |
| `test_catalog_id_parameter_matches_account` | the catalog parameters are read | `catalog-id` equals the AWS account id | Asserts `Parameters["catalog-id"] == accountId`. Result: the catalog points at this account's Glue Data Catalog. |

### Given: the Athena database

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_athena_database_exists_in_glue` | the database is looked up in Glue | it exists with the expected name | `glue:GetDatabase`; asserts the name. Result: the CREATE DATABASE DDL landed in Glue. |
| `test_athena_database_visible_via_athena` | databases are listed via Athena for the catalog | the database appears in the list | `athena:ListDatabases`; asserts the db name is present. Result: the database is discoverable through Athena. |

### Given: the Athena workgroup

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_athena_workgroup_exists` | the workgroup is fetched | its name matches | `athena:GetWorkGroup`; asserts the name. |
| `test_engine_version_3` | the workgroup engine version is read | it is "Athena engine version 3" | Asserts `SelectedEngineVersion`. Result: engine is pinned. |
| `test_enforce_workgroup_configuration` | the workgroup config is read | `EnforceWorkGroupConfiguration` is true | Result: tenants cannot override workgroup settings. |
| `test_cloudwatch_metrics_enabled` | the workgroup config is read | `PublishCloudWatchMetricsEnabled` is true | Result: query metrics are emitted to CloudWatch. |
| `test_output_location` | the result config is read | output location is `s3://<db-bucket>/output/` | Asserts `ResultConfiguration.OutputLocation`. Result: results land in the mandated location. |
| `test_expected_bucket_owner` | the result config is read | `ExpectedBucketOwner` equals the account id | Result: guards against writing to a bucket owned by another account. |
| `test_acl_bucket_owner_full_control` | the result ACL config is read | `S3AclOption` is `BUCKET_OWNER_FULL_CONTROL` | Result: the bucket owner retains full control of result objects. |
| `test_encryption_option_is_sse_kms` | the result encryption config is read | `EncryptionOption` is `SSE_KMS` | Result: query results are encrypted with KMS. |

### Given: the catalog + database + workgroup working together

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_simple_query_executes_successfully` | `SELECT 1` is run through the workgroup | the query reaches `SUCCEEDED` | Submits and polls to completion; asserts state. Result: the three resources function together end-to-end. |
| `test_query_results_land_in_expected_s3_location` | `SELECT 1` is run and the result object inspected | output starts with `s3://<bucket>/output/` and the object is SSE-KMS encrypted | Reads `OutputLocation`, `s3:HeadObject`; asserts prefix and `ServerSideEncryption == aws:kms`. Result: results are written and encrypted as configured. |
| `test_show_tables_in_athena_database` | `SHOW TABLES IN <db>` is run | it succeeds | Asserts `SUCCEEDED`. Result: the database is queryable through the workgroup, not just present in Glue. |
| `test_athena_workgroup_enforcement` | a query tries to override the output location | the workgroup's location wins and the override is ignored | Submits with a "should-be-ignored" result location; asserts `SUCCEEDED` and the actual location is the enforced one. Result: enforcement is actively applied, not just declared. |

---

## File 2 — `test_secure_athena_access.py` (Secure access & functionality)

Verifies the enablement is **private, least-privilege, and works**: the Lambda is
VPC-attached, the network path is locked to the VPC endpoint, both IAM roles (Lambda and
EKS/IRSA) are least-privilege with the `aws:CalledVia` guardrail, and the Lambda actually
runs queries. IAM checks parse the deployed policy documents directly.

### Given: the query Lambda is deployed

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_lambda_runs_inside_the_vpc` | the Lambda config is inspected | it is attached to VPC subnets and a security group | `lambda:GetFunctionConfiguration`; asserts `SubnetIds`/`SecurityGroupIds` non-empty. Result: runs privately inside data-vpc, not the public internet. |
| `test_lambda_subnets_are_the_endpoint_subnets` | the Lambda subnets are compared to the endpoint subnets | the Lambda subnets are a subset of the endpoint subnets | `GetFunctionConfiguration` + `ec2:DescribeVpcEndpoints`; asserts both non-empty and subset. Result: same AZs as the endpoint ENIs, guaranteeing reachability. |
| `test_lambda_environment_is_configured` | the env vars are read | database/catalog/workgroup are set and `AWS_REGION` is absent | Asserts the three values; `AWS_REGION` not set (reserved). Result: only the query comes from the event. |
| `test_lambda_uses_the_expected_execution_role` | the execution role is checked | it is `athena_lambda_execution_role` | Asserts the role ARN. Result: uses the dedicated least-privilege role. |
| `test_lambda_is_not_publicly_invokable_via_url` | a Function URL is requested | none exists | `lambda:GetFunctionUrlConfig` must raise ResourceNotFound. Result: no public HTTP entrypoint. |

### Given: the VPC endpoint and security groups

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_athena_endpoint_is_interface_type_with_private_dns` | the Athena endpoint is described | it is an Interface endpoint with private DNS enabled | `ec2:DescribeVpcEndpoints`; asserts type and `PrivateDnsEnabled`. Result: the default Athena hostname resolves privately to the endpoint. |
| `test_lambda_egress_is_restricted_to_the_endpoint` | the Lambda SG egress rules are examined | the only egress is TCP 443 to the endpoint SG, no `0.0.0.0/0` | `ec2:DescribeSecurityGroupRules`; asserts every egress is tcp/443 to an endpoint SG and none open. Result (with a passing query): traffic can only reach Athena via the endpoint. |
| `test_endpoint_accepts_the_lambda_sg` | the endpoint SG ingress is examined | it allows 443 from the Lambda SG | Asserts an ingress rule referencing the Lambda SG on 443. Result: the Lambda is admitted to the endpoint. |
| `test_endpoint_accepts_the_eks_cluster_sg` | the endpoint SG ingress is examined | it allows 443 from the EKS cluster SG | `eks:DescribeCluster` for the cluster SG + rule check. Result: EKS pods can reach the endpoint. |

### Given: the Lambda's deployed IAM policy

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_athena_scoped_to_its_workgroup_and_not_calledvia` | the Athena workgroup statement is inspected | scoped to the one workgroup (not `*`), and not CalledVia-gated | `iam:GetRolePolicy`; asserts workgroup ARN present, no `*`, includes StartQueryExecution, no CalledVia. Result: direct Athena calls are least-privilege. |
| `test_named_data_catalog_permission_is_present_and_scoped` | the data-catalog statement is inspected | grants `athena:GetDataCatalog` on the named catalog | Asserts action and catalog ARN. Result: the Lambda can resolve the named catalog (required for queries). |
| `test_glue_read_is_scoped_to_the_database_and_calledvia_gated` | the Glue read statement is inspected | limited to catalog + this database + its tables (not `*`), CalledVia-gated | Asserts catalog, database, table ARNs, no `*`, and the CalledVia condition. Result: Glue reachable only via Athena, only for this database. |
| `test_glue_write_is_granted_scoped_and_calledvia_gated` | the Glue write statement is inspected | write actions granted, scoped (not `*`), CalledVia-gated | Asserts CreateTable/UpdateTable, resources not `*`, CalledVia. Result: read+write scope correctly bounded. |
| `test_results_s3_has_multipart_actions_and_calledvia` | the results-bucket statement is inspected | includes PutObject + multipart + GetBucketLocation, CalledVia-gated | Asserts AbortMultipartUpload/ListMultipartUploadParts/GetBucketLocation. Result: large (multipart) result writes will not hit AccessDenied. |
| `test_data_s3_write_is_granted_scoped_and_calledvia` | the data-bucket statement is inspected | Put/DeleteObject granted, scoped to the data bucket, CalledVia-gated | Asserts write actions, bucket in all resources, CalledVia. Result: write path bounded to the data bucket. |
| `test_kms_has_no_encrypt_and_is_calledvia_gated` | the KMS statement is inspected | actions are exactly Decrypt + GenerateDataKey (no Encrypt), CalledVia-gated | Asserts the exact action set and CalledVia. Result: least-privilege KMS; superfluous Encrypt absent. |
| `test_calledvia_guardrail_covers_every_data_plane_statement` | all statements are swept | every Glue/S3/KMS statement is CalledVia-gated; direct Athena/logs/ENI are not | Asserts the expected statements exist, then verifies gating per statement. Result: the confused-deputy guardrail is applied consistently. |
| `test_no_statement_grants_a_wildcard_action` | all statements are swept | no statement grants Action `*` | Asserts `*` is not an action in any statement (scoped wildcards like `ec2:Describe*` allowed). Result: no admin-equivalent grant. |

### Given: the EKS IRSA role

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_trust_policy_is_scoped_to_the_serviceaccount` | the role trust policy is inspected | assumable only via web identity, only by the specific namespace and service account | `iam:GetRole`; asserts AssumeRoleWithWebIdentity, a Federated OIDC principal, and the exact `sub` condition. Result: no other pod can assume the role. |
| `test_eks_role_shares_the_query_permissions` | the EKS inline policy is inspected | has the shared workgroup access and database-scoped Glue read | Asserts StartQueryExecution and Glue read scoped to the database tables. Result: pods get exactly the query permissions. |
| `test_eks_role_keeps_the_calledvia_guardrail` | the EKS Glue/S3/KMS statements are inspected | each is CalledVia-gated | Asserts CalledVia on the three data-plane statements. Result: the guardrail carries over to the EKS role. |
| `test_eks_role_has_no_lambda_only_permissions` | the EKS statements are inspected | no CloudWatch Logs or EC2/ENI permissions | Asserts those statements absent and no `logs:`/`ec2:` actions. Result: the shared policy did not leak Lambda-specific grants to pods. |

### Given: the deployed Lambda (real invocation)

| Test name | When | Then | Details / Result |
|---|---|---|---|
| `test_show_tables_succeeds` | invoked with `SHOW TABLES IN <db>` | returns statusCode 200 with a non-empty table list | `lambda:Invoke`; asserts no FunctionError, 200, rowCount >= 1. Result: the full private + IAM + Athena path works end-to-end. |
| `test_select_returns_result_rows` | invoked with `SELECT 1` | returns 200 with rows containing `1` | Asserts 200 and a cell contains `1`. Result: results are fetched and returned. |
| `test_missing_query_returns_400` | invoked with an empty event | returns statusCode 400 | Asserts no FunctionError and 400. Result: input validation rejects a missing query. |
| `test_invalid_query_returns_500` | invoked with a query on a non-existent table | returns statusCode 500 | Asserts 500. Result: query failures are reported cleanly, not crashed. |
