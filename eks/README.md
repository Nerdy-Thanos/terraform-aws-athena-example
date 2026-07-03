# EKS → Athena connectivity test

Proves an EKS pod can run Athena queries programmatically (a common pattern: services run
on EKS and need API access to Athena). Reuses the **same IAM query permissions** and the
**same Athena interface VPC endpoint** as the Lambda test; only the identity (IRSA) and the
run-once workload (a manually triggered **Pod**) are new.

> **Optional.** These resources are created only when `eks_cluster_name` is set in
> `terraform.tfvars`. Leave it empty (`""`) to skip everything in this directory.

## What Terraform creates (in the root module)

- `aws_iam_role.eks_athena_query_role` — IRSA role trusting the cluster's OIDC provider,
  pinned to `system:serviceaccount:<namespace>:<serviceaccount>`.
- Its inline policy = the shared `athena_query_permissions` document (identical to the
  Lambda's Athena/Glue/S3/KMS access).
- `aws_vpc_security_group_ingress_rule.athena_vpce_from_eks` — allows the EKS cluster SG
  on the Athena endpoint (443).

## Prerequisites (from the AWS account)

1. Set `eks_cluster_name` in `terraform.tfvars` (the cluster must be in the VPC named by
   `vpc_name`, so it can reach the Athena interface endpoint).
2. The cluster's **IAM OIDC provider** must be associated:
   `eksctl utils associate-iam-oidc-provider --cluster <name> --approve`
   (or a `aws_iam_openid_connect_provider` resource).
3. An **ECR repo** to push the image to, and nodes able to pull from it.

## Steps

```bash
# 1. Apply Terraform (creates the IRSA role + endpoint SG ingress)
terraform apply
terraform output eks_query_role_arn        # paste into k8s/serviceaccount.yaml

# 2. Build + push the image to your ECR
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1; REPO=athena-query-test
aws ecr create-repository --repository-name "$REPO" --region "$REGION" 2>/dev/null || true
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker build -t "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest" .
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest"
# then set that image URI in k8s/pod.yaml (<ECR_IMAGE_URI>)

# 3. Create the ServiceAccount once (persists across runs)
kubectl apply -f k8s/serviceaccount.yaml

# 4. Manually trigger the pod (set <ECR_IMAGE_URI> in k8s/pod.yaml first)
kubectl apply -f k8s/pod.yaml

# 5. Verify
kubectl get pod athena-connectivity-test           # STATUS should reach Completed
kubectl logs -f athena-connectivity-test           # prints the SHOW TABLES result
```

### Re-running

A Pod name is unique and won't re-run on a second `apply`, so recreate it:

```bash
kubectl delete pod athena-connectivity-test --ignore-not-found
kubectl apply -f k8s/pod.yaml
# or, in one shot:
kubectl replace --force -f k8s/pod.yaml
```

### Alternative: one-off imperative trigger (no manifest, auto-cleans)

```bash
kubectl run athena-test --image=<ECR_IMAGE_URI> --restart=Never --rm -i \
  --overrides='{"spec":{"serviceAccountName":"athena-query-runner"}}' \
  --env="AWS_REGION=us-east-1" \
  --env="ATHENA_DATABASE=test_sandbox_athena_db" \
  --env="ATHENA_CATALOG=testSandboxCatalog" \
  --env="ATHENA_WORKGROUP=testSandboxAthenaWorkgroup" \
  --env="ATHENA_QUERY=SHOW TABLES IN test_sandbox_athena_db"
```

`--rm -i` streams the output and deletes the pod when it exits — the fastest manual run.
(`serviceAccountName` must go through `--overrides`; `kubectl run` has no flag for it.)

## Expected outcome

- **Success:** pod `STATUS: Completed` (phase `Succeeded`), logs show `Query SUCCEEDED` and
  the table list.
- **Broken network** (e.g. cluster not in the endpoint's VPC, endpoint SG doesn't allow
  the cluster SG, or private DNS off): the pod fails fast with a **connect timeout to the
  Athena endpoint** (5s connect timeout in the script), ending in `Error` — the negative
  signal you're validating.
- **Identity misconfigured** (SA annotation/role trust mismatch): an
  `AccessDenied`/`AssumeRoleWithWebIdentity` error instead of a timeout — distinguishes an
  IAM problem from a network problem.

## Notes

- Pod logs go to stdout (`kubectl logs`), not CloudWatch — so the IRSA role deliberately
  has **no** `logs:*` permissions, unlike the Lambda role.
- The ServiceAccount name/namespace in the manifests must match `k8s_service_account_name`
  / `k8s_namespace` in Terraform, or the trust condition won't match and the pod can't
  assume the role.
