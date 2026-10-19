"""Accounts API: open accounts, view balances, simulated deposits, transaction history."""

import base64
import binascii
import json
import os
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.common import (
    ApiError,
    api_handler,
    ddb,
    ledger_entry,
    now_iso,
    parse_amount,
    parse_body,
    parse_currency,
    path_param,
    public_entry,
    response,
    table,
    user_id,
)

MAX_DEPOSIT_MINOR = 10_000_00  # demo funding limit: 10,000.00 per deposit


def _public_account(item: dict) -> dict:
    return {
        "account_id": item["account_id"],
        "currency": item["currency"],
        "balance": int(item["balance"]),
        "status": item["status"],
        "created_at": item["created_at"],
    }


def _get_owned_account(account_id: str, owner: str) -> dict:
    item = table("ACCOUNTS_TABLE").get_item(Key={"account_id": account_id}).get("Item")
    # Return 404 (not 403) for other people's accounts so we don't reveal they exist.
    if not item or item["owner_id"] != owner:
        raise ApiError(404, "account_not_found", "Account not found")
    return item


def create_account(event):
    owner = user_id(event)
    body = parse_body(event)
    currency = parse_currency(body.get("currency", "GBP"))
    item = {
        "account_id": str(uuid.uuid4()),
        "owner_id": owner,
        "currency": currency,
        "balance": 0,
        "status": "ACTIVE",
        "created_at": now_iso(),
    }
    table("ACCOUNTS_TABLE").put_item(Item=item, ConditionExpression="attribute_not_exists(account_id)")
    return response(201, _public_account(item))


def list_accounts(event):
    owner = user_id(event)
    result = table("ACCOUNTS_TABLE").query(
        IndexName="owner_id-index", KeyConditionExpression=Key("owner_id").eq(owner)
    )
    return response(200, {"accounts": [_public_account(a) for a in result["Items"]]})


def get_account(event):
    account = _get_owned_account(path_param(event, "account_id"), user_id(event))
    return response(200, _public_account(account))


def deposit(event):
    """Simulated funding (stands in for a card top-up or incoming bank payment)."""
    owner = user_id(event)
    account_id = path_param(event, "account_id")
    amount = parse_amount(parse_body(event).get("amount"))
    if amount > MAX_DEPOSIT_MINOR:
        raise ApiError(400, "deposit_limit", f"Deposits are limited to {MAX_DEPOSIT_MINOR} minor units")
    account = _get_owned_account(account_id, owner)

    deposit_id = str(uuid.uuid4())
    created_at = now_iso()
    entry = ledger_entry(
        account_id=account_id, created_at=created_at, transfer_id=deposit_id, direction="CREDIT",
        amount=amount, currency=account["currency"], counterparty="EXTERNAL", reference="Deposit",
    )
    try:
        ddb().meta.client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": os.environ["ACCOUNTS_TABLE"],
                "Key": {"account_id": account_id},
                "UpdateExpression": "SET balance = balance + :amt",
                "ConditionExpression": "owner_id = :owner AND #status = :active",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":amt": amount, ":owner": owner, ":active": "ACTIVE"},
            }},
            {"Put": {
                "TableName": os.environ["TRANSACTIONS_TABLE"],
                "Item": entry,
                "ConditionExpression": "attribute_not_exists(sk)",
            }},
        ])
    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            raise ApiError(409, "account_not_active", "Account is not active") from None
        raise
    return response(201, {
        "deposit_id": deposit_id, "account_id": account_id, "amount": amount,
        "currency": account["currency"], "created_at": created_at,
    })


def _encode_token(last_key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_token(token: str, account_id: str) -> dict:
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, binascii.Error):
        raise ApiError(400, "invalid_token", "next_token is invalid") from None
    # A tampered token must never let someone page through a different account.
    if not isinstance(key, dict) or key.get("account_id") != account_id or "sk" not in key:
        raise ApiError(400, "invalid_token", "next_token is invalid")
    return {"account_id": key["account_id"], "sk": key["sk"]}


def list_transactions(event):
    owner = user_id(event)
    account_id = path_param(event, "account_id")
    _get_owned_account(account_id, owner)

    params = event.get("queryStringParameters") or {}
    try:
        limit = int(params.get("limit", 25))
    except ValueError:
        raise ApiError(400, "invalid_limit", "limit must be a number") from None
    limit = max(1, min(limit, 100))

    query = {
        "KeyConditionExpression": Key("account_id").eq(account_id),
        "ScanIndexForward": False,  # newest first
        "Limit": limit,
    }
    if params.get("next_token"):
        query["ExclusiveStartKey"] = _decode_token(params["next_token"], account_id)

    result = table("TRANSACTIONS_TABLE").query(**query)
    body = {"transactions": [public_entry(i) for i in result["Items"]]}
    if "LastEvaluatedKey" in result:
        body["next_token"] = _encode_token(result["LastEvaluatedKey"])
    return response(200, body)


handler = api_handler({
    "POST /accounts": create_account,
    "GET /accounts": list_accounts,
    "GET /accounts/{account_id}": get_account,
    "POST /accounts/{account_id}/deposits": deposit,
    "GET /accounts/{account_id}/transactions": list_transactions,
})
