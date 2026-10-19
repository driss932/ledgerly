"""Transfers API: atomic, idempotent, double-entry money movement between accounts.

One DynamoDB transaction does all of this, or none of it:
  1. debit the source account   (only if caller owns it, it's active, same currency, enough funds)
  2. credit the destination     (only if it exists, it's active, same currency)
  3. write the DEBIT ledger entry
  4. write the CREDIT ledger entry
  5. store the idempotency record (only if this key hasn't been used yet)
"""

import hashlib
import json
import os
import time
import uuid

from botocore.exceptions import ClientError

from app.common import (
    ApiError,
    api_handler,
    cancellation_reasons,
    ddb,
    ledger_entry,
    logger,
    now_iso,
    parse_amount,
    parse_body,
    parse_currency,
    response,
    table,
    user_id,
)

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
MAX_REFERENCE_LENGTH = 140

# Positions of the items in the transaction, used to read cancellation reasons.
SOURCE, DESTINATION, DEBIT, CREDIT, IDEMPOTENCY = range(5)


def _idempotency_key(event: dict) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    key = headers.get("idempotency-key")
    if not key or len(key) > 128:
        raise ApiError(400, "idempotency_key_required", "Idempotency-Key header (max 128 chars) is required")
    return key


def _request_hash(owner: str, body: dict) -> str:
    fields = ("from_account_id", "to_account_id", "amount", "currency", "reference")
    canonical = json.dumps({"owner": owner, **{f: body.get(f) for f in fields}}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _replay(record_key: str, request_hash: str):
    """Return the stored response if this idempotency key was already used, else None."""
    item = table("IDEMPOTENCY_TABLE").get_item(
        Key={"idempotency_key": record_key}, ConsistentRead=True
    ).get("Item")
    if item is None:
        return None
    if item["request_hash"] != request_hash:
        raise ApiError(422, "idempotency_key_reused",
                       "This Idempotency-Key was already used with a different request")
    return response(int(item["status_code"]), json.loads(item["response_body"]),
                    {"Idempotent-Replayed": "true"})


def _explain_source_failure(account_id: str, owner: str, currency: str, amount: int) -> ApiError:
    """The transaction only says 'condition failed'; read the account to say why."""
    item = table("ACCOUNTS_TABLE").get_item(Key={"account_id": account_id}).get("Item")
    if not item or item["owner_id"] != owner:
        return ApiError(404, "account_not_found", "Source account not found")
    if item["status"] != "ACTIVE":
        return ApiError(409, "account_not_active", "Source account is not active")
    if item["currency"] != currency:
        return ApiError(422, "currency_mismatch", "Source account currency does not match")
    if int(item["balance"]) < amount:
        return ApiError(422, "insufficient_funds", "Insufficient funds")
    return ApiError(409, "conflict", "Account changed during the transfer, please retry")


def _validate(body: dict) -> tuple[str, str, int, str, str]:
    from_id, to_id = body.get("from_account_id"), body.get("to_account_id")
    if not isinstance(from_id, str) or not isinstance(to_id, str) or not from_id or not to_id:
        raise ApiError(400, "invalid_accounts", "from_account_id and to_account_id are required")
    if from_id == to_id:
        raise ApiError(400, "same_account", "Cannot transfer to the same account")
    amount = parse_amount(body.get("amount"))
    currency = parse_currency(body.get("currency"))
    reference = body.get("reference", "")
    if not isinstance(reference, str) or len(reference) > MAX_REFERENCE_LENGTH:
        raise ApiError(400, "invalid_reference", f"reference must be text up to {MAX_REFERENCE_LENGTH} chars")
    return from_id, to_id, amount, currency, reference


def create_transfer(event):
    owner = user_id(event)
    key = _idempotency_key(event)
    body = parse_body(event)
    from_id, to_id, amount, currency, reference = _validate(body)

    record_key = f"{owner}#{key}"  # keys are scoped per user
    request_hash = _request_hash(owner, body)
    replay = _replay(record_key, request_hash)
    if replay:
        return replay

    transfer_id = str(uuid.uuid4())
    created_at = now_iso()
    result = {
        "transfer_id": transfer_id,
        "from_account_id": from_id,
        "to_account_id": to_id,
        "amount": amount,
        "currency": currency,
        "reference": reference,
        "status": "COMPLETED",
        "created_at": created_at,
    }
    accounts_table = os.environ["ACCOUNTS_TABLE"]
    transactions_table = os.environ["TRANSACTIONS_TABLE"]
    common_entry = {"created_at": created_at, "transfer_id": transfer_id, "amount": amount,
                    "currency": currency, "reference": reference}

    items = [None] * 5
    items[SOURCE] = {"Update": {
        "TableName": accounts_table,
        "Key": {"account_id": from_id},
        "UpdateExpression": "SET balance = balance - :amt",
        "ConditionExpression": (
            "owner_id = :owner AND #status = :active AND currency = :cur AND balance >= :amt"
        ),
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":amt": amount, ":owner": owner, ":active": "ACTIVE",
                                      ":cur": currency},
    }}
    items[DESTINATION] = {"Update": {
        "TableName": accounts_table,
        "Key": {"account_id": to_id},
        "UpdateExpression": "SET balance = balance + :amt",
        "ConditionExpression": "attribute_exists(account_id) AND #status = :active AND currency = :cur",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":amt": amount, ":active": "ACTIVE", ":cur": currency},
    }}
    items[DEBIT] = {"Put": {
        "TableName": transactions_table,
        "Item": ledger_entry(account_id=from_id, direction="DEBIT", counterparty=to_id, **common_entry),
        "ConditionExpression": "attribute_not_exists(sk)",
    }}
    items[CREDIT] = {"Put": {
        "TableName": transactions_table,
        "Item": ledger_entry(account_id=to_id, direction="CREDIT", counterparty=from_id, **common_entry),
        "ConditionExpression": "attribute_not_exists(sk)",
    }}
    items[IDEMPOTENCY] = {"Put": {
        "TableName": os.environ["IDEMPOTENCY_TABLE"],
        "Item": {
            "idempotency_key": record_key,
            "request_hash": request_hash,
            "status_code": 201,
            "response_body": json.dumps(result),
            "expires_at": int(time.time()) + IDEMPOTENCY_TTL_SECONDS,
        },
        "ConditionExpression": "attribute_not_exists(idempotency_key)",
    }}

    try:
        ddb().meta.client.transact_write_items(TransactItems=items)
    except ClientError as err:
        if err.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = cancellation_reasons(err)
        failed = lambda i: len(reasons) > i and reasons[i] == "ConditionalCheckFailed"  # noqa: E731
        if failed(IDEMPOTENCY):
            # A concurrent request with the same key won the race: return its result.
            replay = _replay(record_key, request_hash)
            if replay:
                return replay
        if failed(SOURCE):
            raise _explain_source_failure(from_id, owner, currency, amount) from None
        if failed(DESTINATION):
            raise ApiError(422, "invalid_destination",
                           "Destination account not found, not active or in another currency") from None
        if "TransactionConflict" in reasons:
            raise ApiError(409, "conflict", "Concurrent update, please retry") from None
        raise

    logger.info(json.dumps({"event": "transfer_completed", "transfer_id": transfer_id,
                            "amount": amount, "currency": currency}))
    return response(201, result)


handler = api_handler({"POST /transfers": create_transfer})
