locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # The named Athena data catalog is an Athena-only name that resolves to the
  # account's DEFAULT Glue Data Catalog (its catalog-id parameter = the account id). So in
  # Glue/IAM terms there is no separate catalog resource - it is arn:...glue...:catalog.
  # Glue's permission model is hierarchical (catalog -> database -> table), so scoping the
  # role to our database means granting the catalog, the database, and all tables in it.
  glue_catalog_arn  = "arn:aws:glue:${local.region}:${local.account_id}:catalog"
  glue_database_arn = "arn:aws:glue:${local.region}:${local.account_id}:database/${var.athena_db_name}"
  glue_tables_arn   = "arn:aws:glue:${local.region}:${local.account_id}:table/${var.athena_db_name}/*"
}
