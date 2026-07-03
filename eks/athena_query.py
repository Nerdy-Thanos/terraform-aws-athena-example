"""Run-once Athena connectivity check for an EKS pod.

Submits one query (default: SHOW TABLES IN <database>) through the Athena API, waits for
it to finish, prints the result, and exits 0 on SUCCEEDED / non-zero otherwise. Designed
to be run as a manually triggered Kubernetes Pod so it lands in Completed on success.

Credentials come from IRSA (the EKS webhook injects AWS_ROLE_ARN and
AWS_WEB_IDENTITY_TOKEN_FILE when the ServiceAccount is annotated); boto3 picks them up
automatically. Network path: the pod reaches Athena via the interface VPC endpoint, so a
broken SG/route surfaces as a fast connection timeout rather than a hang.
"""

import os
import sys
import time

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
DATABASE = os.environ["ATHENA_DATABASE"]
CATALOG = os.environ["ATHENA_CATALOG"]
WORKGROUP = os.environ["ATHENA_WORKGROUP"]
QUERY = os.environ.get("ATHENA_QUERY", f"SHOW TABLES IN {DATABASE}")

# Fail fast and loudly if the endpoint is unreachable, instead of the default ~60s x retries.
BOTO_CONFIG = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2})


def main():
    athena = boto3.client("athena", region_name=REGION, config=BOTO_CONFIG)
    print(
        f"Submitting query (workgroup={WORKGROUP}, catalog={CATALOG}, db={DATABASE}): {QUERY}",
        flush=True,
    )

    execution_id = athena.start_query_execution(
        QueryString=QUERY,
        QueryExecutionContext={"Database": DATABASE, "Catalog": CATALOG},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    print(f"QueryExecutionId={execution_id}", flush=True)

    while True:
        execution = athena.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)

    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "")
        print(f"Query {state}: {reason}", flush=True)
        sys.exit(1)

    results = athena.get_query_results(QueryExecutionId=execution_id, MaxResults=100)
    rows = [[cell.get("VarCharValue") for cell in row["Data"]] for row in results["ResultSet"]["Rows"]]
    print(f"Query SUCCEEDED - {len(rows)} row(s) returned:", flush=True)
    for row in rows:
        print(row, flush=True)


if __name__ == "__main__":
    main()
