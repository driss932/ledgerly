import base64
import json

import pytest


def test_create_and_list_only_returns_my_accounts(call, open_account):
    mine = open_account("alice")
    open_account("bob")

    status, body, _ = call("GET /accounts", "alice")

    assert status == 200
    assert [a["account_id"] for a in body["accounts"]] == [mine]
    assert body["accounts"][0]["balance"] == 0


def test_cannot_see_someone_elses_account(call, open_account):
    bobs = open_account("bob")

    status, body, _ = call("GET /accounts/{account_id}", "alice", path_params={"account_id": bobs})

    assert status == 404  # not 403: don't reveal that the account exists
    assert body["error"]["code"] == "account_not_found"


def test_deposit_increases_balance_and_writes_ledger_entry(call, open_account, balance):
    acc = open_account("alice", deposit=5000)

    assert balance("alice", acc) == 5000
    status, body, _ = call("GET /accounts/{account_id}/transactions", "alice",
                           path_params={"account_id": acc})
    assert status == 200
    assert body["transactions"][0]["direction"] == "CREDIT"
    assert body["transactions"][0]["counterparty_account_id"] == "EXTERNAL"


def test_deposit_limit(call, open_account):
    acc = open_account("alice")

    status, body, _ = call("POST /accounts/{account_id}/deposits", "alice",
                           body={"amount": 10_000_01}, path_params={"account_id": acc})

    assert status == 400
    assert body["error"]["code"] == "deposit_limit"


def test_unsupported_currency_rejected(call):
    status, body, _ = call("POST /accounts", "alice", body={"currency": "BTC"})
    assert status == 400
    assert body["error"]["code"] == "invalid_currency"


def test_transactions_are_paginated_newest_first(call, open_account):
    acc = open_account("alice")
    for amount in (100, 200, 300):
        call("POST /accounts/{account_id}/deposits", "alice", body={"amount": amount},
             path_params={"account_id": acc})

    status, page1, _ = call("GET /accounts/{account_id}/transactions", "alice",
                            path_params={"account_id": acc}, query={"limit": "2"})
    assert status == 200
    assert [t["amount"] for t in page1["transactions"]] == [300, 200]

    status, page2, _ = call("GET /accounts/{account_id}/transactions", "alice",
                            path_params={"account_id": acc},
                            query={"limit": "2", "next_token": page1["next_token"]})
    assert status == 200
    assert [t["amount"] for t in page2["transactions"]] == [100]


def test_tampered_page_token_cannot_read_another_account(call, open_account):
    mine = open_account("alice")
    bobs = open_account("bob", deposit=100)
    token = base64.urlsafe_b64encode(json.dumps({"account_id": bobs, "sk": "z"}).encode()).decode()

    status, body, _ = call("GET /accounts/{account_id}/transactions", "alice",
                           path_params={"account_id": mine}, query={"next_token": token})

    assert status == 400
    assert body["error"]["code"] == "invalid_token"


@pytest.mark.parametrize("raw_body", ["not json", "[1, 2]"])
def test_invalid_body(raw_body):
    from app import accounts
    from tests.conftest import make_event

    event = make_event("POST /accounts", "alice")
    event["body"] = raw_body
    resp = accounts.handler(event, None)

    assert resp["statusCode"] == 400


def test_unknown_route_returns_404(call):
    status, _, _ = call("DELETE /accounts", "alice")
    assert status == 404


def test_missing_identity_returns_401():
    from app import accounts

    resp = accounts.handler({"routeKey": "GET /accounts", "requestContext": {}}, None)
    assert resp["statusCode"] == 401
