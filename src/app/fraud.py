"""Fraud checks, triggered asynchronously by the DynamoDB Stream on the transactions table.

Reading the stream (instead of the transfer Lambda publishing an event itself) means we never
get a transfer that was saved but whose event was lost: the stream only contains committed writes.

Rules (deliberately simple; a real bank would use a scoring model):
  - large_transfer: a single debit at or above LARGE_TRANSFER_THRESHOLD
  - velocity:       more than VELOCITY_LIMIT debits on one account within VELOCITY_WINDOW_MINUTES
"""

import json
import os
import time
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer

from app.common import TIMESTAMP_FORMAT, logger, sns, table

_deserializer = TypeDeserializer()


def _new_image(record: dict) -> dict:
    return {k: _deserializer.deserialize(v) for k, v in record["dynamodb"]["NewImage"].items()}


def evaluate(entry: dict) -> list[str]:
    """Return the list of rules this ledger entry breaks (empty list = looks fine)."""
    reasons = []
    threshold = int(os.environ.get("LARGE_TRANSFER_THRESHOLD", "100000"))
    if int(entry["amount"]) >= threshold:
        reasons.append(f"large_transfer: amount >= {threshold}")

    window = int(os.environ.get("VELOCITY_WINDOW_MINUTES", "10"))
    limit = int(os.environ.get("VELOCITY_LIMIT", "5"))
    since = (datetime.now(UTC) - timedelta(minutes=window)).strftime(TIMESTAMP_FORMAT)
    result = table("TRANSACTIONS_TABLE").query(
        KeyConditionExpression=Key("account_id").eq(entry["account_id"]) & Key("sk").gte(since),
        FilterExpression=Attr("direction").eq("DEBIT"),
        Select="COUNT",
    )
    if result["Count"] > limit:
        reasons.append(f"velocity: {result['Count']} debits in {window} minutes")
    return reasons


def _flag(entry: dict, reasons: list[str]) -> None:
    alert = {
        "transfer_id": entry["transfer_id"],
        "account_id": entry["account_id"],
        "amount": int(entry["amount"]),
        "currency": entry["currency"],
        "reasons": reasons,
    }
    sns().publish(
        TopicArn=os.environ["FRAUD_TOPIC_ARN"],
        Subject="Ledgerly: transfer flagged for review",
        Message=json.dumps(alert, indent=2),
    )
    # CloudWatch Embedded Metric Format: this log line becomes a FraudFlags metric for free.
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "Ledgerly",
                "Dimensions": [["Environment"]],
                "Metrics": [{"Name": "FraudFlags", "Unit": "Count"}],
            }],
        },
        "Environment": os.environ.get("ENVIRONMENT", "dev"),
        "FraudFlags": 1,
        **alert,
    }))


def handler(event, context):
    for record in event.get("Records", []):
        try:
            if record.get("eventName") != "INSERT":
                continue
            entry = _new_image(record)
            if entry.get("direction") != "DEBIT":
                continue
            reasons = evaluate(entry)
            if reasons:
                _flag(entry, reasons)
        except Exception:
            logger.exception("fraud_check_failed")
            # Partial batch response: Lambda retries from this record onwards.
            # Stop here so later records aren't processed twice (duplicate alerts).
            return {"batchItemFailures": [{"itemIdentifier": record["dynamodb"]["SequenceNumber"]}]}
    return {"batchItemFailures": []}
