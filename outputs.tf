output "athena_workgroup_arn" {
    description = "ARN of the Athena Workgroup"
    value       = aws_athena_workgroup.athena_workgroup.arn
}

output "athena_vpc_endpoint_id" {
    description = "ID of the Athena interface VPC endpoint the Lambda routes through"
    value       = data.aws_vpc_endpoint.athena.id
}

output "lambda_security_group_id" {
    description = "Security group attached to the Lambda's VPC ENIs"
    value       = aws_security_group.lambda.id
}

output "eks_query_role_arn" {
    description = "IRSA role ARN to annotate on the Kubernetes ServiceAccount (null when the EKS test is disabled)"
    value       = one(aws_iam_role.eks_athena_query_role[*].arn)
}