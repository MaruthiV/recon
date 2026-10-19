from datetime import date

import pytest
from google.adk.models.base_llm import BaseLlm

import recon_agents.workflow as wf
from recon_agents.meter import Price, Pricing, UnpricedModel, Usage
from recon_agents.roles import ROLES

ON = date(2026, 10, 17)


def test_snapshot_loads_and_prices_cached_and_thoughts_once():
    p = Pricing.load("2026-10-17")
    assert p.snapshot == "2026-10-17"
    # 600 fresh in, 400 cached in, 100 out + 50 thoughts billed as out
    u = Usage(prompt=1000, cached=400, output=100, thoughts=50)
    assert p.cost_micros("gemini-3.8-flash", u, ON) == -(-(600 * 750_000 + 400 * 75_000 + 150 * 3_750_000) // 1_000_000)


def test_cost_rounds_up_never_down():
    p = Pricing("t", {"m": Price(input=1, output=1)})
    assert p.cost_micros("m", Usage(prompt=1), ON) == 1
    assert p.cost_micros("m", Usage(), ON) == 0


def test_unknown_model_is_an_error_not_free():
    with pytest.raises(UnpricedModel):
        Pricing.load("2026-10-17").cost_micros("gemini-9-ultra", Usage(prompt=1), ON)


def test_price_expires_with_the_schedule():
    with pytest.raises(UnpricedModel, match="expired"):
        Pricing.load("2026-10-17").cost_micros("gemini-3.8-flash", Usage(prompt=1), date(2027, 1, 1))


def test_workflow_version_tracks_prompts_and_models(monkeypatch):
    models: dict[str, str] = dict.fromkeys(ROLES, "gemini-3.8-flash")
    base = wf.workflow_version(models)
    assert base == wf.workflow_version(dict(models))
    assert base != wf.workflow_version(models | {ROLES[0]: "gemini-3.5-flash-lite"})
    original = wf.prompt
    monkeypatch.setattr(wf, "prompt", lambda role: original(role) + " ")
    assert base != wf.workflow_version(models)


def test_a_prompt_variant_from_outside_the_repo_changes_the_version(monkeypatch, tmp_path):
    models: dict[str, BaseLlm | str] = dict.fromkeys(ROLES, "gemini-3.8-flash")
    base = wf.workflow_version(models)
    for role in ROLES:
        (tmp_path / f"{role}.txt").write_text(wf.prompt(role) + "\nBe brief.")
    monkeypatch.setenv("RECON_PROMPTS_DIR", str(tmp_path))
    assert wf.workflow_version(models) != base
