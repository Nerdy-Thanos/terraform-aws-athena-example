# Athena Service Enablement — Permissions & Network Guardrails

**Audience:** Platform engineers defining the guardrails and limits under which tenants
may run Amazon Athena via API.

**What this documents:** the exact IAM permissions required to run Athena queries
through the API (here simulated by a Lambda function; in production the same API calls
traverse an Athena **interface VPC endpoint**), why each permission is needed, the
guardrails you control, and the full VPC endpoint configuration (security groups,
endpoint policy, and the set of endpoints actually required).

Every source linked in this document is official AWS documentation. See
[Sources](#sources).

The repo is a **runnable sandbox**: the Terraform in the root deploys everything the
guide describes, and the pytest suite in `tests/` proves the deployed setup is private,
least-privilege, and functional against real AWS.

---

## Repository layout

| Path | What it is |
|------|------------|
| `main.tf`, `variables.tf`, `data.tf`, `locals.tf`, `outputs.tf` | Terraform: Athena catalog/database/workgroup, least-privilege IAM, the VPC-attached query Lambda, and the optional EKS/IRSA role |
| `terraform.tfvars.example` | Template for your variable values — copy to `terraform.tfvars` |
| `src/athena_lambda.py` | Lambda handler: submits a query, polls it to completion, returns the rows |
| `eks/` | Optional EKS pod connectivity test (Dockerfile, script, k8s manifests) — see [eks/README.md](eks/README.md) |
| `tests/` | Real-AWS pytest suite (42 cases) validating the resources, the private network path, and the IAM guardrails — see [tests/TEST_CASES.md](tests/TEST_CASES.md) |
| `evidence/` | Sample output from a passing run |

## Quick start

### Prerequisites

Already existing in your AWS account — this module does **not** create them:

1. **An S3 bucket** for query results (and optionally a second bucket for table data),
   SSE-KMS encrypted.
2. **A KMS key** encrypting the bucket(s).
3. **A VPC** (identified by its `Name` tag → the `vpc_name` variable) containing an
   **Athena interface VPC endpoint** (`com.amazonaws.<region>.athena`) with **private DNS
   enabled**. The Lambda is deployed into the endpoint's subnets so its only route to the
   Athena API is through that endpoint. Because those subnets are private, you also need
   an interface endpoint for **CloudWatch Logs** (and ideally **STS**) — see
   [§5.1](#51-which-endpoints-do-you-actually-need) and the reference Terraform in
   [§5.6](#56-terraform-reference-prod--not-applied-in-this-sandbox).
4. *(Optional)* An **EKS cluster** in that VPC with its IAM OIDC provider associated, for
   the pod-based test. Leave `eks_cluster_name = ""` to skip every EKS resource.

On your machine: **Terraform ≥ 1.5**, the **AWS CLI** with credentials for the target
account, **Python 3.11+** (for the tests), and **Docker + kubectl** only if you run the
EKS test.

### Deploy

```bash
cp terraform.tfvars.example terraform.tfvars   # fill in your bucket, KMS ARN, VPC name
terraform init
terraform plan
terraform apply
```

This creates the Athena data catalog, database, and workgroup (SSE-KMS results, enforced
config), the least-privilege execution role, the `athena_query_runner` Lambda inside the
VPC, the paired security-group rules opening exactly the 443 path to the Athena endpoint,
and a 14-day-retention log group. When `eks_cluster_name` is set, it also creates the
IRSA role and the endpoint ingress rule for the cluster.

### Test it

Invoke the Lambda:

```bash
aws lambda invoke --function-name athena_query_runner \
  --cli-binary-format raw-in-base64-out \
  --payload '{"query": "SELECT 1"}' /dev/stdout
```

Run the integration suite (hits real AWS — no mocks):

```bash
cd tests
pip install boto3 pytest
ATHENA_DB_BUCKET=<your-results-bucket> pytest -v
```

Resource-name env vars default to the values in `terraform.tfvars.example`; override them
(`ATHENA_DB_NAME`, `ATHENA_WORKGROUP_NAME`, `VPC_NAME`, `AWS_REGION`, …) if you changed
the names. The EKS cases are skipped unless `EKS_CLUSTER_NAME` is set.

For the EKS pod test, follow [eks/README.md](eks/README.md).

### Tear down

```bash
terraform destroy
```

---

## 1. Mental model: two permission planes

This is the single most important concept for getting Athena permissions right, and it
drives both the IAM policy and the VPC design. An Athena query touches AWS services on
**two distinct planes**:

| Plane | Who makes the call | Credentials used | `aws:CalledVia` present? | Network path |
|-------|--------------------|------------------|--------------------------|--------------|
| **A — Direct** | The caller (Lambda / app / JDBC) | Caller's own role | No | Caller's VPC → **Athena endpoint** |
| **B — Forward Access Session (FAS)** | The **Athena service**, on the caller's behalf | Caller's role, re-vended by Athena | **Yes** (`athena.amazonaws.com`) | AWS backbone (Athena → Glue/S3/KMS) — **not your VPC** |

- **Plane A** = `athena:StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`,
  `GetWorkGroup`, `GetDataCatalog`. These are the calls the caller makes directly.
- **Plane B** = the Glue Data Catalog lookups, S3 reads/writes (source data **and**
  results), and KMS operations that Athena performs *while executing the query*. Athena
  uses [forward access sessions](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_forward_access_sessions.html),
  which re-vend the caller's identity and **populate `aws:CalledVia` with
  `athena.amazonaws.com`**.

**Why it matters for guardrails:**
1. You can require `aws:CalledVia = athena.amazonaws.com` on the Plane-B permissions so
   the tenant role can touch Glue/S3/KMS **only through a live Athena query**, never
   directly. (See [§3](#3-the-calledvia-guardrail).)
2. Plane-B traffic **does not flow through your VPC**, so you **cannot** gate it with
   `aws:SourceVpc` / `aws:SourceVpce` / `aws:SourceIp`. Athena reaches S3/Glue from its
   own service network. This is explicitly documented and is a common architecture-review
   trap. (See [§5](#5-vpc-endpoint-configuration-production).)

---

## 2. Required IAM permissions (the tenant execution role)

These are the permissions attached to the role the caller assumes
(`athena_lambda_execution_role` in this sandbox). Scope every ARN to the tenant's
workgroup / catalog / buckets.

### 2.1 Plane A — Athena API (no `CalledVia`)

| Action | Resource scope | Why required | Source |
|--------|----------------|--------------|--------|
| `athena:StartQueryExecution` | workgroup ARN | Submit the query | [1] |
| `athena:GetQueryExecution` | workgroup ARN | Poll query status | [1] |
| `athena:GetQueryResults` | workgroup ARN | Fetch result rows via the API | [1] |
| `athena:GetWorkGroup` | workgroup ARN | Resolve/enforce workgroup config | [1][7] |
| `athena:GetDataCatalog` | **datacatalog ARN** | **Required when the query names a non-default (registered) data catalog** — e.g. `Catalog=testSandboxCatalog` in `QueryExecutionContext`. Omit only if using the default `AwsDataCatalog`. | [2][7] |
| `athena:StopQueryExecution` | workgroup ARN | Optional — only if the app cancels queries | [1] |

> **Guardrail:** scope these to `arn:aws:athena:<region>:<account>:workgroup/<name>` and
> `arn:aws:athena:<region>:<account>:datacatalog/<name>` — never `"*"`.

### 2.2 Plane B — AWS Glue Data Catalog (via `CalledVia`)

Athena resolves databases/tables/partitions from Glue using the caller's identity.

**Read (all `SELECT`):**
`glue:GetCatalog(s)`, `glue:GetDatabase(s)`, `glue:GetTable(s)`, `glue:GetTableVersions`,
`glue:GetPartition(s)`.

**Write (only if tenants run `CTAS` / `INSERT` / Iceberg / DDL):**
`glue:CreateTable`, `glue:UpdateTable`, `glue:BatchCreatePartition`,
`glue:BatchUpdatePartition`, `glue:BatchDeletePartition`.
(An Iceberg write is fundamentally an `UpdateTable`; the `Batch*Partition` actions matter
only for classic Hive-partitioned tables.)

Sources: [3][6]. Best practice: scope to specific catalog/database/table ARNs rather than
`"*"` (see [Fine-grained access to Glue resources][6]).

### 2.3 Plane B — Amazon S3 (via `CalledVia`)

Athena reads source data and **writes query results** to S3 using the caller's identity.

**Query-results bucket / prefix** (`s3://<results-bucket>/output/*`):

| Action | Level | Why |
|--------|-------|-----|
| `s3:PutObject` | object | Write result files |
| `s3:GetObject` | object | Read results back (e.g. `GetQueryResults`) |
| `s3:AbortMultipartUpload` | object | **Large result sets are written via multipart upload** |
| `s3:ListMultipartUploadParts` | object | Same — multipart result writes |
| `s3:ListBucket` | bucket | List result objects |
| `s3:GetBucketLocation` | bucket | Resolve bucket region |

> ⚠️ **The multipart trap:** without `AbortMultipartUpload` / `ListMultipartUploadParts`,
> a `SELECT 1` **succeeds** (single small PUT) but any result large enough to trigger a
> multipart upload **fails with AccessDenied** — i.e. it breaks in production, not in your
> smoke test. AWS's own example policy includes these actions. Source: [4][8].

**Source-data bucket** — read: `s3:GetObject`, `s3:ListBucket`, `s3:GetBucketLocation`.
Add `s3:PutObject` + `s3:DeleteObject` only for write workloads (CTAS/INSERT/Iceberg
compaction can replace or delete underlying files). Source: [4].

### 2.4 Plane B — AWS KMS (via `CalledVia`, only if buckets are SSE-KMS)

| Action | Why |
|--------|-----|
| `kms:GenerateDataKey` | Encrypt objects being written (S3 SSE-KMS) |
| `kms:Decrypt` | Read encrypted source data / results |

> `kms:Encrypt` is **not** used by S3 SSE-KMS — omit it. Grant on the specific key ARN(s).
> The workgroup here enforces `SSE_KMS` on results (see [§4](#4-guardrails-you-own)).

### 2.5 Caller logging (no `CalledVia`)

The Lambda/app writes its own logs directly: `logs:CreateLogStream`, `logs:PutLogEvents`,
scoped to the function's log group ARN. Prefer an explicit log group with retention over
the broad `AWSLambdaBasicExecutionRole` managed policy (which grants `logs:*` on `*`).

### 2.6 What Athena does **not** need from you

The workgroup's `result_configuration` enforces the output location + encryption
(`enforce_workgroup_configuration = true`), so the **caller must not pass
`ResultConfiguration`** on `StartQueryExecution` — the workgroup overrides it anyway.

---

## 3. The `CalledVia` guardrail

`aws:CalledVia` is an [AWS global condition key][9] populated when a service makes a FAS
call on a principal's behalf. Conditioning the Plane-B statements on:

```json
"Condition": { "ForAnyValue:StringEquals": { "aws:CalledVia": "athena.amazonaws.com" } }
```

means the tenant role **cannot** call Glue/S3/KMS directly — only a live Athena query
execution can exercise those permissions. This is a strong, tenant-facing guardrail.

**Caveats to defend in review:**
- AWS's *documented example* applies `CalledVia` only to `lambda:InvokeFunction` (the
  federated-connector Lambda). For S3/Glue/KMS it is **supported** (those are FAS calls,
  so `CalledVia` is populated) but not shown in the vetted example. **Validate with a live
  query** — including a write/CTAS and a large-result query — before relying on it. Source:
  [5][4].
- `aws:CalledVia` is **not compatible with trusted identity propagation** (IAM Identity
  Center). Source: [5].

---

## 4. Guardrails you own (platform-engineer levers)

As the platform team, these are the controls you set; tenants inherit them.

| Guardrail | Mechanism | Effect |
|-----------|-----------|--------|
| **Force output location & encryption** | Workgroup `result_configuration` + `enforce_workgroup_configuration = true` | Tenants cannot redirect results or disable SSE-KMS | [10][7] |
| **Cost cap** | Workgroup `bytes_scanned_cutoff_per_query` | Kills queries scanning more than the limit | [10] |
| **Engine pinning** | Workgroup `engine_version` | Tenants can't silently change engine | [10] |
| **Confine role to Athena** | `aws:CalledVia` on Glue/S3/KMS | Role usable only via Athena queries | [5] |
| **Least-privilege scoping** | Workgroup/catalog/bucket/key ARNs (not `"*"`) | Blast radius per tenant | [1][6] |
| **Network origin** | VPC endpoint + `aws:SourceVpce` on **Plane-A** actions | Athena API reachable only via your endpoint | [11][12] |
| **Data perimeter** | `aws:PrincipalOrgID` / `aws:ResourceOrgID` in the endpoint policy | Only org identities to org resources | [11][13] |

---

## 5. VPC endpoint configuration (production)

In production the tenant's API calls to Athena go through an **interface VPC endpoint
(AWS PrivateLink)** instead of the public internet. This section is the reference config.

### 5.1 Which endpoints do you actually need?

Decide per plane (see [§1](#1-mental-model-two-permission-planes)). **Only Plane-A traffic
uses your VPC.**

| Endpoint | Type | Needed? | Reason |
|----------|------|---------|--------|
| `com.amazonaws.<region>.athena` | Interface | **Required** | The tenant's `athena:*` API calls (Plane A) |
| `com.amazonaws.<region>.logs` | Interface | **Required** (VPC Lambda) | Caller writes CloudWatch Logs from a private subnet |
| `com.amazonaws.<region>.sts` | Interface | Recommended | SDK/regional STS credential calls from the caller |
| `com.amazonaws.<region>.glue` | Interface | Recommended by AWS | AWS pairs the Athena endpoint with a **Glue endpoint**; needed if in-VPC clients call Glue directly (Plane A). Query-time Glue access by Athena is Plane B (backbone). | [12] |
| `com.amazonaws.<region>.s3` | Gateway | **Only if the client reads S3 directly** | boto3 `GetQueryResults` does **not** touch S3; JDBC/ODBC "streaming" result mode **does** |
| `com.amazonaws.<region>.kms` | Interface | Only if the client calls KMS directly | Athena's KMS use during a query is Plane B (backbone) |

> **Do not** try to add an S3/Glue/KMS endpoint expecting to route *Athena's* access
> through it — that traffic is Plane B and never enters your VPC.

### 5.2 Private DNS

Enable **private DNS** on the Athena interface endpoint so the default
`https://athena.<region>.amazonaws.com` resolves to the endpoint — no SDK/code change
needed. Requires `enableDnsSupport` + `enableDnsHostnames` on the VPC. Without it, callers
must target `<vpce-id>.athena.<region>.vpce.amazonaws.com`. Source: [12].

### 5.3 Security groups

Interface endpoints are ENIs in your subnets, protected by a security group. Athena's API
is **HTTPS/TCP 443**.

**Endpoint SG** (attached to the interface endpoints):
- **Ingress:** TCP **443** from the **client SG** (the Lambda/app SG) — or the private
  subnet CIDRs. Nothing else.
- **Egress:** not used by the endpoint ENI to initiate connections; leave default or none.

**Client SG** (attached to the Lambda ENIs / app):
- **Egress:** TCP **443** to the **endpoint SG** (least privilege) or to the VPC CIDR /
  the endpoint's prefix.
- **Ingress:** none (the Lambda does not receive inbound connections).

Source: [11][12].

### 5.4 Endpoint policy

The endpoint policy is a resource-style guardrail evaluated **in addition** to the tenant's
IAM policy — the intersection wins.

**(a) Data-perimeter baseline (verbatim from AWS [12]):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowRequestsByOrgsIdentitiesToOrgsResources",
      "Effect": "Allow",
      "Principal": { "AWS": "*" },
      "Action": "*",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:PrincipalOrgID": "{{my-org-id}}",
          "aws:ResourceOrgID": "{{my-org-id}}"
        }
      }
    },
    {
      "Sid": "AllowRequestsByAWSServicePrincipals",
      "Effect": "Allow",
      "Principal": { "AWS": "*" },
      "Action": "*",
      "Resource": "*",
      "Condition": { "Bool": { "aws:PrincipalIsAWSService": "true" } }
    }
  ]
}
```

**(b) Tenant-scoped (tighter — pin principal, actions, and workgroup/catalog):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowTenantAthenaViaEndpoint",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::<account>:role/athena_lambda_execution_role" },
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:GetWorkGroup",
        "athena:GetDataCatalog",
        "athena:StopQueryExecution"
      ],
      "Resource": [
        "arn:aws:athena:<region>:<account>:workgroup/testSandboxAthenaWorkgroup",
        "arn:aws:athena:<region>:<account>:datacatalog/testSandboxCatalog"
      ]
    }
  ]
}
```

### 5.5 Force the API through the endpoint (defense in depth)

Add to the **tenant IAM policy** (on the Plane-A statement only):

```json
"Condition": { "StringEquals": { "aws:SourceVpce": "vpce-0123456789abcdef0" } }
```

> ✅ Works for `athena:*` (Plane A).
> ❌ **Cannot** be applied to the Glue/S3/KMS (Plane-B) statements — Athena reaches those
> from its service network, not your VPC endpoint. Use `aws:CalledVia` there instead.
> Source: [4].

### 5.6 Terraform reference (prod — not applied in this sandbox)

```hcl
resource "aws_security_group" "athena_vpce" {
  name        = "athena-vpce"
  description = "Athena interface endpoint - HTTPS from tenant workloads"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from tenant/app security group"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.client_sg_id]   # the Lambda/app SG
  }
  # No egress needed on the endpoint ENI itself.
}

resource "aws_vpc_endpoint" "athena" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.athena"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.athena_vpce.id]
  policy              = data.aws_iam_policy_document.athena_vpce_policy.json
}

# Repeat aws_vpc_endpoint for logs + sts (Interface). Add an S3 Gateway endpoint only
# if clients read results directly from S3 (JDBC/ODBC streaming), not for boto3.
```

---

## 6. Common failure modes (and the permission behind each)

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| `SELECT 1` works, large query fails with S3 `AccessDenied` | Missing `s3:AbortMultipartUpload` / `ListMultipartUploadParts` on results | Add multipart actions (§2.3) |
| AccessDenied resolving a named catalog | Missing `athena:GetDataCatalog` | Add it, scoped to datacatalog ARN (§2.1) |
| Query fails only when role calls Glue/S3 directly | `aws:CalledVia` condition (working as intended) | Route through Athena, or relax the condition (§3) |
| Lambda times out in private subnet | No Athena/Logs/STS endpoint reachable | Provision interface endpoints (§5.1) |
| Results never expire / no logs | Missing log-group retention / scoped logs policy | Explicit `aws_cloudwatch_log_group` (§2.5) |
| Trying to gate Athena→S3 by `aws:SourceVpce` has no effect | Plane-B traffic isn't in your VPC | Use `aws:CalledVia` (§1, §5.5) |

---

## 7. Validation matrix

Run against a real workgroup; each row isolates one permission path.

| Test query | Proves |
|------------|--------|
| `SELECT 1` | Plane A + results write + KMS + `GetQueryResults` |
| `SHOW TABLES IN <db>` | Glue read + `GetDataCatalog` + `CalledVia`(Glue) |
| `CREATE TABLE ... WITH (external_location=...) AS SELECT ...` | Glue **write** + data-bucket write + KMS + `CalledVia` (full read+write path) |
| `SELECT * FROM <ctas_table>` | Source-data S3 read + `CalledVia` |
| `SELECT ... FROM UNNEST(SEQUENCE(1, 400000))` | **Multipart** results write (confirm the query reaches `SUCCEEDED` via `get-query-execution` — the caller's full-row return will exceed Lambda's 6 MB cap) |

---

## Sources

All official AWS documentation:

1. [Identity and access management in Athena](https://docs.aws.amazon.com/athena/latest/ug/security-iam-athena.html)
2. [StartQueryExecution — Athena API Reference](https://docs.aws.amazon.com/athena/latest/APIReference/API_StartQueryExecution.html)
3. [Access to databases and tables in AWS Glue (Athena)](https://docs.aws.amazon.com/athena/latest/ug/fine-grained-access-to-glue-resources.html)
4. [Control access to Amazon S3 from Athena](https://docs.aws.amazon.com/athena/latest/ug/s3-permissions.html)
5. [Use CalledVia context keys for Athena](https://docs.aws.amazon.com/athena/latest/ug/security-iam-athena-calledvia.html)
6. [Fine-grained access to Glue resources](https://docs.aws.amazon.com/athena/latest/ug/fine-grained-access-to-glue-resources.html)
7. [Allow access to Athena Federated Query: example policies](https://docs.aws.amazon.com/athena/latest/ug/federated-query-iam-access.html)
8. [AWS managed policies for Amazon Athena](https://docs.aws.amazon.com/athena/latest/ug/managed-policies.html)
9. [AWS global condition context keys (`aws:CalledVia`)](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html)
10. [Use IAM policies to control workgroup access / workgroup settings](https://docs.aws.amazon.com/athena/latest/ug/workgroups-iam-policy.html)
11. [Control access to VPC endpoints using endpoint policies](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-access.html)
12. [Connect to Amazon Athena using an interface VPC endpoint](https://docs.aws.amazon.com/athena/latest/ug/interface-vpc-endpoint.html)
13. [Building a data perimeter on AWS — VPC endpoint policy examples](https://docs.aws.amazon.com/whitepapers/latest/building-a-data-perimeter-on-aws/appendix-2-vpc-endpoint-policy-examples.html)
14. [Forward access sessions — IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_forward_access_sessions.html)
