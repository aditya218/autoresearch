import pytest

from autoresearch.config import CampaignConfig, ConfigError, load_config

TOY = "toy_project/campaign.yaml"


def make(**workflow_overrides) -> dict:
    return {
        "name": "c",
        "goal": "g",
        "workflow": workflow_overrides
        or {
            "implement": {"agentic": True, "skills": ["s"]},
            "eval": {"after": "implement", "uses": "job"},
        },
    }


def test_toy_config_loads():
    cfg = load_config(TOY)
    assert cfg.name == "toy"
    assert cfg.phase_order() == ["implement", "smoke_test", "train", "analyze"]
    assert cfg.root_phase == "implement"
    assert cfg.workflow["smoke_test"].gate is True
    assert cfg.workflow["train"].repair.skill == "toy-repair"
    assert cfg.key_metrics["score"].from_phase == "train"


def test_phase_must_be_agentic_or_deterministic():
    with pytest.raises(Exception):
        CampaignConfig.model_validate(make(a={"agentic": True, "uses": "job"}))
    with pytest.raises(Exception):
        CampaignConfig.model_validate(make(a={}))


def test_unknown_predecessor_rejected():
    with pytest.raises(Exception):
        CampaignConfig.model_validate(
            make(a={"agentic": True}, b={"after": "nope", "uses": "job"})
        )


def test_cycle_rejected():
    with pytest.raises(Exception):
        CampaignConfig.model_validate(
            make(
                a={"after": "b", "uses": "job"},
                b={"after": "a", "uses": "job"},
            )
        )


def test_exactly_one_root_required():
    with pytest.raises(Exception):
        CampaignConfig.model_validate(
            make(a={"agentic": True}, b={"uses": "job"})
        )


def test_key_metric_must_come_from_deterministic_phase():
    raw = make()
    raw["key_metrics"] = {"acc": {"from": "implement"}}
    with pytest.raises(Exception, match="deterministic"):
        CampaignConfig.model_validate(raw)

    raw["key_metrics"] = {"acc": {"from": "eval"}}
    cfg = CampaignConfig.model_validate(raw)
    assert cfg.key_metrics["acc"].from_phase == "eval"


def test_key_metric_unknown_phase_rejected():
    raw = make()
    raw["key_metrics"] = {"acc": {"from": "ghost"}}
    with pytest.raises(Exception):
        CampaignConfig.model_validate(raw)


def test_bad_yaml_raises_config_error(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("just a string\n")
    with pytest.raises(ConfigError):
        load_config(path)
