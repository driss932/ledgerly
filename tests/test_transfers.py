import pytest


def test_successful_transfer_moves_money_and_writes_both_entries(
    call, open_account, balance, transfer
):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob")

    status, body, _ = transfer("alice", alice, bob, 2_500)

    assert status == 201
    assert body["status"] == "COMPLETED"
    assert balance("alice", alice) == 7_500
    assert balance("bob", bob) == 2_500

    _, history, _ = call("GET /accounts/{account_id}/transactions", "bob",
                         path_params={"account_id": bob})
    credit = history["transactions"][0]
    assert credit["direction"] == "CREDIT"
    assert credit["transfer_id"] == body["transfer_id"]
    assert credit["counterparty_account_id"] == alice


def test_money_is_conserved_across_many_transfers(open_account, balance, transfer):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob", deposit=10_000)

    for i in range(10):
        transfer("alice", alice, bob, 300, key=f"a-{i}")
        transfer("bob", bob, alice, 200, key=f"b-{i}")

    assert balance("alice", alice) + balance("bob", bob) == 20_000


def test_insufficient_funds_rejected_and_nothing_moves(open_account, balance, transfer):
    alice = open_account("alice", deposit=1_000)
    bob = open_account("bob")

    status, body, _ = transfer("alice", alice, bob, 1_001)

    assert status == 422
    assert body["error"]["code"] == "insufficient_funds"
    assert balance("alice", alice) == 1_000
    assert balance("bob", bob) == 0


def test_retry_with_same_idempotency_key_does_not_double_charge(open_account, balance, transfer):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob")

    status1, first, _ = transfer("alice", alice, bob, 1_000, key="retry-me")
    status2, second, headers = transfer("alice", alice, bob, 1_000, key="retry-me")

    assert status1 == status2 == 201
    assert first["transfer_id"] == second["transfer_id"]
    assert headers["Idempotent-Replayed"] == "true"
    assert balance("alice", alice) == 9_000  # charged once


def test_reusing_key_for_a_different_request_is_rejected(open_account, transfer):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob")

    transfer("alice", alice, bob, 1_000, key="k")
    status, body, _ = transfer("alice", alice, bob, 9_999, key="k")

    assert status == 422
    assert body["error"]["code"] == "idempotency_key_reused"


def test_idempotency_keys_are_scoped_per_user(open_account, balance, transfer):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob", deposit=10_000)

    transfer("alice", alice, bob, 1_000, key="same-key")
    status, _, headers = transfer("bob", bob, alice, 1_000, key="same-key")

    assert status == 201
    assert "Idempotent-Replayed" not in headers


def test_cannot_spend_from_someone_elses_account(open_account, balance, transfer):
    alice = open_account("alice", deposit=10_000)
    mallory = open_account("mallory")

    status, body, _ = transfer("mallory", alice, mallory, 5_000)

    assert status == 404
    assert body["error"]["code"] == "account_not_found"
    assert balance("alice", alice) == 10_000


def test_currency_mismatch_on_source(open_account, transfer):
    alice = open_account("alice", currency="EUR", deposit=10_000)
    bob = open_account("bob", currency="EUR")

    status, body, _ = transfer("alice", alice, bob, 100, currency="GBP")

    assert status == 422
    assert body["error"]["code"] == "currency_mismatch"


def test_destination_in_another_currency_rejected(open_account, balance, transfer):
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob", currency="USD")

    status, body, _ = transfer("alice", alice, bob, 100)

    assert status == 422
    assert body["error"]["code"] == "invalid_destination"
    assert balance("alice", alice) == 10_000


def test_missing_destination_rejected(open_account, transfer):
    alice = open_account("alice", deposit=10_000)

    status, body, _ = transfer("alice", alice, "does-not-exist", 100)

    assert status == 422
    assert body["error"]["code"] == "invalid_destination"


def test_failed_transfer_does_not_burn_the_idempotency_key(open_account, transfer, call):
    alice = open_account("alice", deposit=100)
    bob = open_account("bob")

    status, _, _ = transfer("alice", alice, bob, 500, key="k")
    assert status == 422
    call("POST /accounts/{account_id}/deposits", "alice", body={"amount": 1_000},
         path_params={"account_id": alice})
    status, _, _ = transfer("alice", alice, bob, 500, key="k")
    assert status == 201


def test_same_account_rejected(open_account, transfer):
    alice = open_account("alice", deposit=1_000)
    status, body, _ = transfer("alice", alice, alice, 100)
    assert status == 400
    assert body["error"]["code"] == "same_account"


@pytest.mark.parametrize("amount", [0, -5, 10.5, "100", True, None, 1_000_000_01])
def test_invalid_amounts_rejected(open_account, transfer, amount):
    alice = open_account("alice", deposit=1_000)
    bob = open_account("bob")

    status, body, _ = transfer("alice", alice, bob, amount)

    assert status == 400
    assert body["error"]["code"] == "invalid_amount"


def test_idempotency_key_header_is_required(call, open_account):
    alice = open_account("alice", deposit=1_000)
    bob = open_account("bob")

    status, body, _ = call("POST /transfers", "alice", body={
        "from_account_id": alice, "to_account_id": bob, "amount": 100, "currency": "GBP"})

    assert status == 400
    assert body["error"]["code"] == "idempotency_key_required"
