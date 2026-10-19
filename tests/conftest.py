import json

import boto3
import pytest
from moto import mock_aws

from app import accounts, common, transfers

REGION = "eu-west-2"


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    """Fresh, fully mocked AWS (DynamoDB + SNS) for every test."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ACCOUNTS_TABLE", "accounts")
    monkeypatch.setenv("TRANSACTIONS_TABLE", "transactions")
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "idempotency")

    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName="accounts",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "account_id", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "owner_id-index",
                "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        client.create_table(
            TableName="transactions",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "account_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "account_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
        )
        client.create_table(
            TableName="idempotency",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        )
        topic = boto3.client("sns", region_name=REGION).create_topic(Name="fraud")["TopicArn"]
        monkeypatch.setenv("FRAUD_TOPIC_ARN", topic)

        common.reset_clients()
        yield
        common.reset_clients()


def make_event(route_key, user, body=None, path_params=None, query=None, headers=None):
    return {
        "routeKey": route_key,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": user}}}},
        "headers": headers or {},
        "pathParameters": path_params,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def call():
    """Invoke the right Lambda handler for a route; returns (status, body, headers)."""

    def _call(route_key, user="alice", **kwargs):
        handler = transfers.handler if route_key == "POST /transfers" else accounts.handler
        resp = handler(make_event(route_key, user, **kwargs), None)
        return resp["statusCode"], json.loads(resp["body"]), resp["headers"]

    return _call


@pytest.fixture
def open_account(call):
    def _open(user, currency="GBP", deposit=0):
        status, account, _ = call("POST /accounts", user, body={"currency": currency})
        assert status == 201
        if deposit:
            status, _, _ = call("POST /accounts/{account_id}/deposits", user, body={"amount": deposit},
                                path_params={"account_id": account["account_id"]})
            assert status == 201
        return account["account_id"]

    return _open


@pytest.fixture
def balance(call):
    def _balance(user, account_id):
        status, account, _ = call("GET /accounts/{account_id}", user,
                                  path_params={"account_id": account_id})
        assert status == 200
        return account["balance"]

    return _balance


@pytest.fixture
def transfer(call):
    def _transfer(user, src, dst, amount, key="key-1", currency="GBP", reference="Rent"):
        body = {"from_account_id": src, "to_account_id": dst, "amount": amount,
                "currency": currency, "reference": reference}
        return call("POST /transfers", user, body=body, headers={"idempotency-key": key})

    return _transfer
