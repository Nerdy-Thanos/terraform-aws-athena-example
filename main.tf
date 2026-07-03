
terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
  }
}

resource "aws_athena_data_catalog" "athena_data_catalog" {
    name = var.athena_data_catalog_name
    description = "Catalog for Athena Queries"
    type = "GLUE"

    parameters = {
        "catalog-id" = local.account_id
    }
}

resource "aws_athena_database" "athena_database" {
    name = var.athena_db_name
    bucket = var.athena_db_bucket
}

resource "aws_athena_workgroup" "athena_workgroup" {
    name = var.athena_workgroup_name
    configuration {
        enforce_workgroup_configuration = true
        publish_cloudwatch_metrics_enabled = true

        engine_version {
            selected_engine_version = "Athena engine version 3"
        }

        result_configuration {
            output_location = "s3://${var.athena_db_bucket}/output/"
            expected_bucket_owner = local.account_id

            encryption_configuration {
                encryption_option = "SSE_KMS"
                kms_key_arn = var.athena_kms_key_arn
            }

            acl_configuration {
                s3_acl_option = "BUCKET_OWNER_FULL_CONTROL"
            }
        }

    }
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "lambda_execution_role" {
  name               = "athena_lambda_execution_role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

data "aws_iam_policy_document" "athena_query_permissions" {
  # --- Direct calls the caller makes to Athena. No CalledVia condition here - these are
  # --- the initial calls, not calls made "via" Athena. Scoped to the specific workgroup
  # --- and data catalog rather than "*".
  statement {
    sid    = "AthenaWorkgroupAccess"
    effect = "Allow"

    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:GetWorkGroup",
    ]

    resources = [aws_athena_workgroup.athena_workgroup.arn]
  }

  # Callers pass Catalog=<named catalog> in QueryExecutionContext. Querying a
  # named/registered data catalog requires athena:GetDataCatalog on that catalog
  # (the default "AwsDataCatalog" would not, but this deployment uses a named one).
  statement {
    sid    = "AthenaDataCatalogAccess"
    effect = "Allow"

    actions   = ["athena:GetDataCatalog"]
    resources = [aws_athena_data_catalog.athena_data_catalog.arn]
  }

  # --- Everything below is only usable when Athena makes the call on the caller's
  # --- behalf (forward access session). The CalledVia condition blocks the caller's own
  # --- code from calling Glue/S3/KMS directly - only a live Athena query execution can
  # --- exercise these. NOTE: validated live; AWS's own example scopes CalledVia only to
  # --- lambda:InvokeFunction, so re-confirm with a real query if you change this.

  statement {
    sid    = "GlueCatalogReadAccess"
    effect = "Allow"

    actions = [
      "glue:GetCatalog",
      "glue:GetCatalogs",
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetTableVersions",
      "glue:GetPartition",
      "glue:GetPartitions",
    ]

    # Default Glue catalog + our database + every table in it (Glue authorizes hierarchically).
    resources = [
      local.glue_catalog_arn,
      local.glue_database_arn,
      local.glue_tables_arn,
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["athena.amazonaws.com"]
    }
  }

  # Required because write access is being granted. UpdateTable/CreateTable cover
  # CTAS and Iceberg commits (an Iceberg write is fundamentally an UpdateTable call
  # under the hood). The BatchXPartition actions only matter for classic Hive-style
  # partitioned tables - drop them if every table here is Iceberg.
  statement {
    sid    = "GlueCatalogWriteAccess"
    effect = "Allow"

    actions = [
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:BatchCreatePartition",
      "glue:BatchUpdatePartition",
      "glue:BatchDeletePartition",
    ]

    # Default Glue catalog + our database + every table in it (Glue authorizes hierarchically).
    resources = [
      local.glue_catalog_arn,
      local.glue_database_arn,
      local.glue_tables_arn,
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["athena.amazonaws.com"]
    }
  }

  # Query-results bucket. Object actions (incl. multipart, which large result sets
  # require) target output/*; bucket actions (ListBucket, GetBucketLocation) target
  # the bucket itself. Missing multipart/GetBucketLocation is the classic "SELECT 1
  # passes but a big result fails" trap.
  statement {
    sid    = "AthenaQueryResultsS3Access"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]

    resources = [
      "arn:aws:s3:::${var.athena_db_bucket}",
      "arn:aws:s3:::${var.athena_db_bucket}/output/*",
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["athena.amazonaws.com"]
    }
  }

  # PLACEHOLDER: var.athena_data_bucket - the bucket holding the actual table
  # data, as opposed to athena_db_bucket, which is only the query-results bucket.
  # DeleteObject is included alongside GetObject/PutObject since Iceberg writes,
  # overwrites, and compaction can replace/remove underlying data files.
  statement {
    sid    = "AthenaDataS3Access"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [
      "arn:aws:s3:::${var.athena_data_bucket}",
      "arn:aws:s3:::${var.athena_data_bucket}/*",
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["athena.amazonaws.com"]
    }
  }

  # PLACEHOLDER: var.athena_data_kms_key_arn - only needed if the data bucket is
  # encrypted with a different key than the query-results bucket (athena_kms_key_arn).
  # If it's the same key, just point both variables at the same ARN.
  # S3 SSE-KMS only exercises GenerateDataKey (writes) and Decrypt (reads); kms:Encrypt
  # is intentionally omitted.
  statement {
    sid    = "AthenaKmsAccess"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
    ]

    resources = [
      var.athena_kms_key_arn,
      var.athena_data_kms_key_arn,
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["athena.amazonaws.com"]
    }
  }
}

# Lambda execution role policy = shared Athena query permissions + Lambda-only extras
# (writing to its own CloudWatch log group and managing its VPC ENIs).
data "aws_iam_policy_document" "lambda_athena_policy" {
  source_policy_documents = [data.aws_iam_policy_document.athena_query_permissions.json]

  # Lambda writes its own logs. Scoped to this function's log group; no CalledVia
  # (not an Athena call).
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.athena_lambda.arn}:*"]
  }

  # Lets the Lambda service attach the function to the VPC by creating/deleting the ENIs
  # in the private subnets. Describe* has no resource-level scoping, so "*". No CalledVia
  # (this is the Lambda service acting, not a call "via" Athena).
  statement {
    sid    = "LambdaVpcNetworking"
    effect = "Allow"

    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]

    resources = ["*"]
  }
}

# ---- EKS IRSA: identity + permissions for pods that run Athena queries -------------
# OPTIONAL - everything in this section is created only when eks_cluster_name is set.
# IRSA maps a Kubernetes ServiceAccount to this IAM role via the cluster's OIDC provider.
# The pod assumes it with a projected web-identity token; the trust condition pins it to a
# single namespace:serviceaccount so no other workload can borrow the role. Prerequisite:
# the cluster's IAM OIDC provider must be associated (aws_iam_openid_connect_provider or
# `eksctl utils associate-iam-oidc-provider`).
locals {
  eks_enabled     = var.eks_cluster_name != ""
  eks_oidc_issuer = local.eks_enabled ? replace(data.aws_eks_cluster.this[0].identity[0].oidc[0].issuer, "https://", "") : null
}

data "aws_iam_policy_document" "eks_irsa_assume" {
  count = local.eks_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${local.account_id}:oidc-provider/${local.eks_oidc_issuer}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.eks_oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.eks_oidc_issuer}:sub"
      values   = ["system:serviceaccount:${var.k8s_namespace}:${var.k8s_service_account_name}"]
    }
  }
}

resource "aws_iam_role" "eks_athena_query_role" {
  count = local.eks_enabled ? 1 : 0

  name               = "athena_eks_query_role"
  assume_role_policy = data.aws_iam_policy_document.eks_irsa_assume[0].json
}

# Same shared query permissions as the Lambda - no logs/ENI extras (pod logs go to stdout
# and pod networking is handled by the node, not this role).
data "aws_iam_policy_document" "eks_athena_policy" {
  count = local.eks_enabled ? 1 : 0

  source_policy_documents = [data.aws_iam_policy_document.athena_query_permissions.json]
}

resource "aws_iam_role_policy" "eks_athena_query" {
  count = local.eks_enabled ? 1 : 0

  name   = "athena_query_permissions"
  role   = aws_iam_role.eks_athena_query_role[0].id
  policy = data.aws_iam_policy_document.eks_athena_policy[0].json
}

resource "aws_iam_role_policy" "lambda_execution_role" {
  name   = "lambda_athena_policy"
  role   = aws_iam_role.lambda_execution_role.id
  policy = data.aws_iam_policy_document.lambda_athena_policy.json
}

# Explicit log group with retention, replacing the broad AWSLambdaBasicExecutionRole
# managed policy. Gives the scoped CloudWatchLogs statement a concrete ARN to target
# and stops logs from accruing forever.
resource "aws_cloudwatch_log_group" "athena_lambda" {
  name              = "/aws/lambda/athena_query_runner"
  retention_in_days = 14
}

# --- VPC networking for the Lambda -------------------------------------------------
# Dedicated SG for the function's ENIs. Its only job is to reach the Athena interface
# endpoint on 443; the paired rules below open exactly that path and nothing else, so a
# broken network config surfaces cleanly as a connection timeout to Athena.
resource "aws_security_group" "lambda" {
  name        = "athena_query_runner_lambda"
  description = "Athena query-runner Lambda ENIs - egress to the Athena VPC endpoint only"
  vpc_id      = data.aws_vpc.data_vpc.id
}

resource "aws_vpc_security_group_egress_rule" "lambda_to_athena_vpce" {
  for_each = toset(data.aws_vpc_endpoint.athena.security_group_ids)

  security_group_id            = aws_security_group.lambda.id
  description                  = "HTTPS to the Athena interface VPC endpoint"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = each.value
}

# Ingress rule on the (externally managed) Athena endpoint SG so it accepts the Lambda.
# Removing this is the intended way to reproduce the "connection timed out" failure mode.
resource "aws_vpc_security_group_ingress_rule" "athena_vpce_from_lambda" {
  for_each = toset(data.aws_vpc_endpoint.athena.security_group_ids)

  security_group_id            = each.value
  description                  = "HTTPS from athena_query_runner Lambda"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.lambda.id
}

# Ingress on the Athena endpoint SG so it accepts the EKS cluster. With the default VPC
# CNI, pod traffic egresses via the node ENI using the cluster security group, so allowing
# that SG lets the test pod reach the endpoint. Assumes the cluster is in the same VPC.
# Only created when the optional EKS test is enabled (eks_cluster_name set).
resource "aws_vpc_security_group_ingress_rule" "athena_vpce_from_eks" {
  for_each = local.eks_enabled ? toset(data.aws_vpc_endpoint.athena.security_group_ids) : toset([])

  security_group_id            = each.value
  description                  = "HTTPS from EKS cluster (Athena connectivity test)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = data.aws_eks_cluster.this[0].vpc_config[0].cluster_security_group_id
}

data "archive_file" "athena_lambda" {
  type        = "zip"
  source_file = "${path.module}/src/athena_lambda.py"
  output_path = "${path.module}/build/athena_lambda.zip"
}

resource "aws_lambda_function" "athena_query" {
  function_name    = "athena_query_runner"
  filename         = data.archive_file.athena_lambda.output_path
  source_code_hash = data.archive_file.athena_lambda.output_base64sha256
  handler          = "athena_lambda.lambda_handler"
  runtime          = "python3.11"
  role             = aws_iam_role.lambda_execution_role.arn
  timeout          = 300 # query is polled to completion in-handler

  # The function must not be created until (a) its log group exists and (b) the inline
  # policy granting the EC2 ENI permissions is attached - otherwise the VPC attach fails
  # with "execution role does not have permissions to call CreateNetworkInterface".
  depends_on = [
    aws_cloudwatch_log_group.athena_lambda,
    aws_iam_role_policy.lambda_execution_role,
  ]

  # Run inside data-vpc, in the same subnets as the Athena interface endpoint, so the
  # only route to the Athena API is through that endpoint (private DNS resolves
  # athena.<region>.amazonaws.com to it). A broken SG/route surfaces as a timeout.
  vpc_config {
    subnet_ids         = data.aws_vpc_endpoint.athena.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      # AWS_REGION is reserved and auto-injected by Lambda - do NOT set it here.
      ATHENA_DATABASE  = var.athena_db_name
      ATHENA_CATALOG   = var.athena_data_catalog_name
      ATHENA_WORKGROUP = var.athena_workgroup_name
    }
  }
}
