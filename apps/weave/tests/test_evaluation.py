import asyncio
from pathlib import Path

import pytest
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.providers import openai
from pydantic import ValidationError
from typer.testing import CliRunner

from pleiad_weave.evaluation import (
    CaseResult,
    EvaluationCase,
    EvaluationReport,
    EvaluationSuite,
    compare_reports,
    evaluate_case,
    evaluation_app,
    failure_cases,
    score_output,
)
from pleiad_weave.infrastructure.config import Settings
from tests.test_execution import PINNED_IMAGE, ScriptedProvider


def task() -> EvaluationCase:
    return EvaluationCase(
        case_id="sum",
        objective="Read numbers.txt and return JSON with sum.",
        files={"numbers.txt": "1\n2\n"},
        expected={"sum": 3},
    )


@pytest.mark.parametrize("answer", ['{"sum": 4}', '{"sum": 3, "extra": true}', '```json\n{"sum": 3}\n```'])
def test_task_scoring_rejects_wrong_extra_or_unparseable_answers(answer: str) -> None:
    assert not score_output(task(), answer)
    assert score_output(task(), '{"sum":3}')


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "a\\b"])
def test_evaluation_fixtures_cannot_escape_their_isolated_workspace(path: str) -> None:
    with pytest.raises(ValidationError, match="fixture paths"):
        EvaluationCase(case_id="invalid", objective="task", files={path: "content"}, expected={})


def test_evaluation_rejects_non_finite_oracles_before_any_provider_work() -> None:
    with pytest.raises(ValidationError, match="Out of range float"):
        EvaluationCase(
            case_id="invalid-oracle", objective="task", files={"input.txt": "data"}, expected={"value": float("nan")}
        )


def test_baseline_comparison_reports_regressions_and_refuses_changed_or_missing_tasks() -> None:
    case = task()
    passed = CaseResult(case_id="sum", case_fingerprint=case.fingerprint(), status="passed", elapsed_seconds=1)
    baseline = EvaluationReport(
        mode="real_provider", provider="test", model="test", suite_cases={"sum": case.fingerprint()}, results=(passed,)
    )
    failed = passed.model_copy(update={"status": "failed"})
    candidate = baseline.model_copy(update={"results": (failed,)})
    assert compare_reports(baseline, candidate).regressions == ("sum",)
    assert failure_cases(EvaluationSuite(cases=(case,)), candidate) == (case,)
    changed = case.model_copy(update={"expected": {"sum": 4}})
    with pytest.raises(ValueError, match="changed"):
        failure_cases(EvaluationSuite(cases=(changed,)), candidate)
    with pytest.raises(ValueError, match="same non-empty"):
        compare_reports(baseline, candidate.model_copy(update={"results": ()}))
    with pytest.raises(ValueError, match="real-provider"):
        compare_reports(baseline, candidate.model_copy(update={"mode": "harness_test"}))
    with pytest.raises(ValidationError, match="unique"):
        EvaluationReport(
            mode="real_provider",
            provider="test",
            model="test",
            suite_cases={"sum": case.fingerprint()},
            results=(failed, passed),
        )
    with pytest.raises(ValueError, match="Partial"):
        compare_reports(baseline, candidate.model_copy(update={"scope": "failed_cases"}))
    with pytest.raises(ValidationError, match="every suite case"):
        EvaluationReport(
            mode="real_provider",
            provider="test",
            model="test",
            suite_cases={"sum": case.fingerprint(), "another": case.fingerprint()},
            results=(passed,),
        )


@pytest.mark.parametrize("unauthorized", [False, True], ids=["read-only", "command-denied"])
def test_evaluation_runs_real_components_and_fails_forbidden_tool_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unauthorized: bool
) -> None:
    provider = ScriptedProvider(
        (
            {"name": "command", "arguments": {"command": "echo forbidden"}}
            if unauthorized
            else {"name": "read_file", "arguments": {"scope": "workspace", "path": "numbers.txt"}},
            '{"sum":3}',
        ),
    )

    def binding(**options: object) -> ScriptedProvider:
        return provider

    monkeypatch.setattr(openai, "OpenAIProvider", binding)
    model = OpenAIModelClient(provider="openai", model="test", api_key="synthetic")
    settings = Settings(workspace=tmp_path, sandbox_image=PINNED_IMAGE)
    result = asyncio.run(evaluate_case(task(), model, settings))
    assert result.status == ("failed" if unauthorized else "passed")
    assert result.run_id is not None and result.usage is not None
    assert result.usage.input_tokens == 2 and result.usage.output_tokens == 2
    assert result.usage.model_calls == 2
    assert result.attempted_tools == (("command",) if unauthorized else ("read_file",))


def test_evaluation_records_unknown_usage_and_budget_exhaustion_without_fabricating_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        ({"name": "read_file", "arguments": {"scope": "workspace", "path": "numbers.txt"}},), report_usage=False
    )

    def binding(**options: object) -> ScriptedProvider:
        return provider

    monkeypatch.setattr(openai, "OpenAIProvider", binding)
    model = OpenAIModelClient(provider="openai", model="test", api_key="synthetic")
    result = asyncio.run(evaluate_case(task(), model, Settings(workspace=tmp_path, sandbox_image=PINNED_IMAGE)))
    assert result.status == "failed" and result.error == "run.limit_reached"
    assert result.usage is not None
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    assert result.usage.model_calls == 1


def test_evaluation_cli_validates_offline_and_requires_explicit_real_provider(tmp_path: Path) -> None:
    suite = tmp_path / "suite.json"
    suite.write_text(EvaluationSuite(cases=(task(),)).model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    validated = runner.invoke(evaluation_app, ["validate", str(suite)])
    assert validated.exit_code == 0
    assert "no model requests" in validated.output
    output = tmp_path / "report.json"
    refused = runner.invoke(evaluation_app, ["run", str(suite), str(output)])
    assert refused.exit_code != 0
    assert "--real-provider" in refused.output
    assert not output.exists()
