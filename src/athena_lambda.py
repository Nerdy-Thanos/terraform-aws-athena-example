import boto3
import os
import time

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DATABASE = os.environ["ATHENA_DATABASE"]
CATALOG = os.environ["ATHENA_CATALOG"]
WORKGROUP = os.environ["ATHENA_WORKGROUP"]

athena_client = boto3.client("athena", region_name=AWS_REGION)


def lambda_handler(event, context):
    query = (event or {}).get("query")
    if not query:
        return {"statusCode": 400, "body": "Missing required 'query' in event"}

    # Workgroup enforces output location + SSE-KMS, so no ResultConfiguration here.
    execution_id = athena_client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": DATABASE, "Catalog": CATALOG},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]

    while True:
        execution = athena_client.get_query_execution(
            QueryExecutionId=execution_id
        )["QueryExecution"]
        status = execution["Status"]["State"]
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)

    if status != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "")
        return {
            "statusCode": 500,
            "body": f"Query {status} (execution {execution_id}): {reason}",
        }

    # Collect all rows across pages. Each cell is {'VarCharValue': ...} (missing when null).
    rows = []
    next_token = None
    while True:
        kwargs = {"QueryExecutionId": execution_id, "MaxResults": 1000}
        if next_token:
            kwargs["NextToken"] = next_token
        result = athena_client.get_query_results(**kwargs)
        for row in result["ResultSet"]["Rows"]:
            rows.append([cell.get("VarCharValue") for cell in row["Data"]])
        next_token = result.get("NextToken")
        if not next_token:
            break

    return {
        "statusCode": 200,
        "queryExecutionId": execution_id,
        "rowCount": len(rows),
        "rows": rows,  # first row is the column header
    }
