from boto3.dynamodb.types import TypeSerializer

from app import fraud

_serializer = TypeSerializer()


def stream_record(entry, seq="1", event_name="INSERT"):
    return {
        "eventName": event_name,
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {k: _serializer.serialize(v) for k, v in entry.items()},
        },
    }


def debit(amount, account_id="acc-1", transfer_id="t-1"):
    return {"account_id": account_id, "transfer_id": transfer_id, "direction": "DEBIT",
            "amount": amount, "currency": "GBP"}


def test_small_transfer_is_not_flagged():
    assert fraud.evaluate(debit(5_000)) == []


def test_large_transfer_is_flagged(monkeypatch):
    monkeypatch.setenv("LARGE_TRANSFER_THRESHOLD", "100000")
    reasons = fraud.evaluate(debit(150_000))
    assert len(reasons) == 1
    assert reasons[0].startswith("large_transfer")


def test_many_transfers_in_a_short_window_are_flagged(open_account, transfer, monkeypatch):
    monkeypatch.setenv("VELOCITY_LIMIT", "5")
    alice = open_account("alice", deposit=10_000)
    bob = open_account("bob")
    for i in range(6):
        transfer("alice", alice, bob, 10, key=f"k{i}")

    reasons = fraud.evaluate(debit(10, account_id=alice))

    assert any(r.startswith("velocity") for r in reasons)


def test_handler_publishes_alert_for_flagged_debit(monkeypatch):
    published = []
    monkeypatch.setattr(fraud, "_flag", lambda entry, reasons: published.append(entry))

    result = fraud.handler({"Records": [
        stream_record(debit(500_000), seq="1"),
        stream_record({**debit(500_000), "direction": "CREDIT"}, seq="2"),  # ignored
        stream_record(debit(100), seq="3"),  # small, not flagged
    ]}, None)

    assert result == {"batchItemFailures": []}
    assert len(published) == 1


def test_flag_publishes_to_sns():
    # Runs against the mocked SNS topic; should not raise.
    fraud._flag(debit(500_000), ["large_transfer"])


def test_handler_reports_first_failure_and_stops(monkeypatch):
    calls = []

    def boom(entry):
        calls.append(entry["transfer_id"])
        raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setattr(fraud, "evaluate", boom)
    result = fraud.handler({"Records": [
        stream_record(debit(1, transfer_id="a"), seq="10"),
        stream_record(debit(1, transfer_id="b"), seq="11"),
    ]}, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "10"}]}
    assert calls == ["a"]  # stopped after the first failure
