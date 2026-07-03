data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# The VPC the Lambda runs in. Looked up by its Name tag.
data "aws_vpc" "data_vpc" {
  filter {
    name   = "tag:Name"
    values = [var.vpc_name]
  }
}

# The existing Athena interface VPC endpoint in data-vpc. We reuse its subnets (so the
# Lambda lands in the same AZs as the endpoint ENIs) and its security group(s) (to open
# exactly the 443 path between the Lambda and the endpoint). Assumes a single Athena
# interface endpoint in this VPC.
data "aws_vpc_endpoint" "athena" {
  vpc_id       = data.aws_vpc.data_vpc.id
  service_name = "com.amazonaws.${data.aws_region.current.region}.athena"
}

# The EKS cluster the test pod runs in (optional - only looked up when
# eks_cluster_name is set). Provides the OIDC issuer (for IRSA trust) and the cluster
# security group (allowed on the Athena endpoint). Assumes the cluster is in the same
# VPC so the Athena interface endpoint is reachable from its nodes/pods.
data "aws_eks_cluster" "this" {
  count = var.eks_cluster_name == "" ? 0 : 1
  name  = var.eks_cluster_name
}