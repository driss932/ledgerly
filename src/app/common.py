"""Shared helpers: AWS clients, HTTP responses, validation and ledger entries.

Money is always handled as an integer number of minor units (pence, cents).
Never use floats for money: 0.1 + 0.2 != 0.3.
"""

import base64
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import boto3

logger = logging.getLogger("ledgerly")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SUPPORTED_CURRENCIES = {"GBP", "EUR", "USD"}
MAX_AMOUNT_MINOR = 1_000_000_00  # 1,000,000.00 per operation
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Clients are created lazily and reused across warm Lambda invocations.
_clients: dict = {}


def ddb():
    if "dynamodb" not in _clients:
        _clients["dynamodb"] = boto3.resource("dynamodb")
    return _clients["dynamodb"]


def sns():
    if "sns" not in _clients:
        _clients["sns"] = boto3.client("sns")
    return _clients["sns"]


def reset_clients():
    """Used by tests so each test gets clients bound to a fresh mock."""
    _clients.clear()


def table(env_name: str):
    return ddb().Table(os.environ[env_name])


def now_iso() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    raise TypeError(f"Cannot serialise {type(value).__name__}")


def response(status: int, body: dict, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=_json_default),
    }


def error_response(err: ApiError) -> dict:
    return response(err.status, {"error": {"code": err.code, "message": err.message}})


def user_id(event: dict) -> str:
    """The Cognito user id, already verified by the API Gateway JWT authorizer."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise ApiError(401, "unauthorized", "Missing identity") from None


def path_param(event: dict, name: str) -> str:
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ApiError(400, "missing_parameter", f"Missing path parameter {name}")
    return value


def parse_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "invalid_json", "Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Request body must be a JSON object")
    return body


def parse_amount(value) -> int:
    # bool is a subclass of int in Python, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_amount", "amount must be an integer in minor units, e.g. 1050 = 10.50")
    if value <= 0 or value > MAX_AMOUNT_MINOR:
        raise ApiError(400, "invalid_amount", f"amount must be between 1 and {MAX_AMOUNT_MINOR}")
    return value


def parse_currency(value) -> str:
    if value not in SUPPORTED_CURRENCIES:
        raise ApiError(400, "invalid_currency", f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}")
    return value


def ledger_entry(*, account_id, created_at, transfer_id, direction, amount, currency,
                 counterparty, reference) -> dict:
    """One side of a double-entry posting. Every transfer writes a DEBIT and a CREDIT."""
    return {
        "account_id": account_id,
        "sk": f"{created_at}#{transfer_id}",  # sorts chronologically within an account
        "transfer_id": transfer_id,
        "direction": direction,
        "amount": amount,
        "currency": currency,
        "counterparty_account_id": counterparty,
        "reference": reference,
        "created_at": created_at,
    }


def public_entry(item: dict) -> dict:
    return {
        "transfer_id": item["transfer_id"],
        "direction": item["direction"],
        "amount": int(item["amount"]),
        "currency": item["currency"],
        "counterparty_account_id": item["counterparty_account_id"],
        "reference": item.get("reference", ""),
        "created_at": item["created_at"],
    }


def cancellation_reasons(client_error) -> list[str]:
    """Reason codes from a TransactionCanceledException, one per transaction item."""
    return [r.get("Code", "None") for r in client_error.response.get("CancellationReasons", [])]


def api_handler(routes: dict):
    """Build a Lambda handler that dispatches HTTP API (payload v2) routes."""

    def handler(event, context):
        route = event.get("routeKey")
        fn = routes.get(route)
        if fn is None:
            return error_response(ApiError(404, "not_found", "Route not found"))
        try:
            return fn(event)
        except ApiError as err:
            logger.info(json.dumps({"event": "client_error", "route": route, "code": err.code}))
            return error_response(err)
        except Exception:
            logger.exception("unhandled_error route=%s", route)
            return error_response(ApiError(500, "internal_error", "Something went wrong"))

    return handler
