athena_data_catalog_name = "testSandboxCatalog"
athena_db_name = "test_sandbox_athena_db"
athena_db_bucket = "sandbox-glue-optimisation"
athena_workgroup_name = "testSandboxAthenaWorkgroup"
athena_kms_key_arn = "arn:aws:kms:us-east-1:381492036057:key/dea963b8-6b9f-4354-bd79-f4ba91f74cc8"

athena_data_bucket = "sandbox-glue-optimisation"
athena_data_kms_key_arn = "arn:aws:kms:us-east-1:381492036057:key/dea963b8-6b9f-4354-bd79-f4ba91f74cc8"

# --- EKS test pod ---
eks_cluster_name         = "glue-eks"
k8s_namespace            = "glue"                 # must match eks/k8s/*.yaml (namespace: glue)
k8s_service_account_name = "athena-query-runner"  # must match eks/k8s/*.yaml