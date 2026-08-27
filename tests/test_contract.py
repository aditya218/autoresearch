import json

import pytest

from autoresearch.contract import (
    ContractError,
    PhaseResult,
    check_produces,
    read_result,
    write_result,
)


def test_round_trip(tmp_path):
    written = PhaseResult(status="passed", metrics={"acc": 0.9}, notes="fine")
    write_result(tmp_path, written)
    assert read_result(tmp_path) == written


def test_missing_result_is_contract_error(tmp_path):
    with pytest.raises(ContractError, match="wrote no result.json"):
        read_result(tmp_path)


def test_invalid_status_rejected(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"status": "mostly passed"}))
    with pytest.raises(ContractError):
        read_result(tmp_path)


def test_prose_metric_rejected(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps({"status": "passed", "metrics": {"acc": "roughly 0.91"}})
    )
    with pytest.raises(ContractError):
        read_result(tmp_path)


def test_malformed_json_rejected(tmp_path):
    (tmp_path / "result.json").write_text("{not json")
    with pytest.raises(ContractError):
        read_result(tmp_path)


def test_extra_fields_rejected(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps({"status": "passed", "surprise": 1})
    )
    with pytest.raises(ContractError):
        read_result(tmp_path)


def test_produces_checked_at_producer(tmp_path):
    write_result(tmp_path, PhaseResult(status="passed"))
    with pytest.raises(ContractError, match="declared outputs"):
        check_produces(tmp_path, ["eval_results.json"])

    (tmp_path / "eval_results.json").write_text("{}")
    check_produces(tmp_path, ["eval_results.json"])  # no raise
