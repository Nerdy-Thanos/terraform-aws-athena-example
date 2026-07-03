variable "athena_data_catalog_name" {
  description = "Name of the Athena Data Catalog"
  type        = string
}

variable "athena_db_name" {
  description = "Name of the Athena Database"
  type        = string
}

variable "athena_db_bucket" {
  description = "S3 bucket for Athena Database"
  type        = string
}

variable "athena_workgroup_name" {
  description = "Name of the Athena Workgroup"
  type        = string
}

variable "athena_kms_key_arn" {
  description = "KMS Key ARN for Athena Workgroup Encryption"
  type        = string
}

variable "athena_data_bucket" {
  description = "S3 bucket for Athena Data"
  type        = string
}

variable "athena_data_kms_key_arn" {
  description = "KMS Key ARN for Athena Data Bucket Encryption"
  type        = string
}

variable "vpc_name" {
  description = "Name tag of the VPC containing the Athena interface VPC endpoint (the Lambda runs here)"
  type        = string
  default     = "data-vpc"
}

variable "eks_cluster_name" {
  description = "Name of the EKS cluster the Athena test pod runs in (must be in the vpc_name VPC). Leave empty to skip all EKS resources."
  type        = string
  default     = ""
}

variable "k8s_namespace" {
  description = "Kubernetes namespace for the Athena query test ServiceAccount/Job"
  type        = string
  default     = "default"
}

variable "k8s_service_account_name" {
  description = "Kubernetes ServiceAccount name bound to the EKS IRSA role"
  type        = string
  default     = "athena-query-runner"
}