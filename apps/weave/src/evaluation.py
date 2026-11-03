import asyncio
import hashlib
import json
import time
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, Self

import typer
from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject
from pleiad.errors.base import PleiadError
from pleiad.events.contracts import Event
from pleiad.events.runtime import ToolStartedEvent
from pleiad.memory.short_memory import ShortMemory
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.observability.tracing import Observability
from pleiad.runtime.config import RunConfig
from pleiad.runtime.execution.runner import Runner
from pleiad.runtime.lifecycle.state import RunStatus, RunUsage
from pleiad.runtime.policy import HostPolicy
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.loader import SkillLoader
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from pydantic import Field, TypeAdapter, field_validator, model_validator

from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.models import create_model
from pleiad_weave.infrastructure.sandbox.provisioning import default_policy

JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)
evaluation_app = typer.Typer(help="Validate and run explicit real-provider SDK task evaluations")


class EvaluationCase(ContractModel):
    """A maintained task fixture and exact JSON oracle, not a production trace."""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    objective: str = Field(min_length=1)
    files: dict[str, str] = Field(min_length=1)
    input: JsonObject = Field(default_factory=dict)
    expected: JsonObject

    @model_validator(mode="after")
    def validate_json_values(self) -> Self:
        """Reject non-finite JSON data before a task can start model work."""
        canonical_json(self.input)
        canonical_json(self.expected)
        return self

    @field_validator("files")
    @classmethod
    def validate_paths(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep every fixture inside its isolated workspace."""
        for path in value:
            if (
                PurePosixPath(path).is_absolute()
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or "\\" in path
                or "\x00" in path
            ):
                raise ValueError("fixture paths must be normalized relative POSIX paths")
        return value

    def fingerprint(self) -> str:
        """Hash the complete task, fixtures, input, and oracle for honest comparisons."""
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


class EvaluationSuite(ContractModel):
    """A versioned collection of uniquely identified task fixtures."""

    version: Literal[1] = 1
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        """Reject ambiguous case identities."""
        ids = tuple(case.case_id for case in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


class CaseResult(ContractModel):
    """Record one actual task outcome without substituting unknown measurements."""

    case_id: str
    case_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["passed", "failed", "error"]
    run_id: str | None = None
    output: str | None = None
    error: str | None = None
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    usage: RunUsage | None = None
    attempted_tools: tuple[str, ...] = ()


class EvaluationReport(ContractModel):
    """Persist a real-provider run or a clearly labeled harness-test run."""

    version: Literal[1] = 1
    mode: Literal["real_provider", "harness_test"]
    provider: str
    model: str
    suite_cases: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(min_length=1)
    scope: Literal["full", "failed_cases"] = "full"
    results: tuple[CaseResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_results(self) -> Self:
        """Reject duplicate scores and incomplete reports labeled as full-suite runs."""
        ids = tuple(result.case_id for result in self.results)
        if len(ids) != len(set(ids)):
            raise ValueError("Evaluation result case IDs must be unique")
        if self.scope == "full" and set(ids) != self.suite_cases.keys():
            raise ValueError("A full-suite report must contain every suite case")
        if any(self.suite_cases.get(result.case_id) != result.case_fingerprint for result in self.results):
            raise ValueError("Evaluation results must match their complete suite manifest")
        return self


class EvaluationComparison(ContractModel):
    """Compare the same task definitions without treating missing cases as passes."""

    regressions: tuple[str, ...]
    recoveries: tuple[str, ...]
    still_failing: tuple[str, ...]


def canonical_json(value: object) -> str:
    """Serialize exact JSON deterministically for scoring and fixture identities.

    Args:
        value: A JSON-compatible task, observation, or oracle.

    Returns:
        Canonical JSON with stable key ordering and no non-finite numbers.

    Raises:
        TypeError: If a value cannot be serialized as JSON.
        ValueError: If a number is not finite.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def score_output(case: EvaluationCase, output: str) -> bool:
    """Check the full JSON answer against the maintained oracle without an LLM judge.

    Args:
        case: Maintained task with its expected complete JSON answer.
        output: Actual final model text, without repairs or code-fence extraction.

    Returns:
        Whether the complete canonical JSON answer equals the oracle.
    """
    try:
        actual = JSON_OBJECT.validate_json(output)
    except ValueError:
        return False
    return canonical_json(actual) == canonical_json(case.expected)


def failure_cases(suite: EvaluationSuite, report: EvaluationReport) -> tuple[EvaluationCase, ...]:
    """Select failed cases only when their task definitions still match.

    Args:
        suite: Complete current task definitions.
        report: Previous actual outcomes, with the complete suite manifest.

    Returns:
        Failed and errored tasks in the previous report's order.

    Raises:
        ValueError: If any fixture, oracle, or task definition changed.
    """
    cases = {case.case_id: case for case in suite.cases}
    if {case_id: case.fingerprint() for case_id, case in cases.items()} != report.suite_cases:
        raise ValueError("Task definition changed or disappeared in the suite")
    selected: list[EvaluationCase] = []
    for result in report.results:
        case = cases.get(result.case_id)
        if case is None or case.fingerprint() != result.case_fingerprint:
            raise ValueError(f"Task definition changed or disappeared: {result.case_id}")
        if result.status != "passed":
            selected.append(case)
    return tuple(selected)


def compare_reports(baseline: EvaluationReport, candidate: EvaluationReport) -> EvaluationComparison:
    """Reject partial or changed-task comparisons and identify outcome regressions.

    Args:
        baseline: Full-suite outcomes from an actual provider run.
        candidate: Full-suite outcomes using the same maintained task definitions.

    Returns:
        Regressed, recovered, and still-failing task identities.

    Raises:
        ValueError: If either report is partial, synthetic, or from another suite.
    """
    if baseline.mode != "real_provider" or candidate.mode != "real_provider":
        raise ValueError("Only real-provider reports can establish or compare a quality baseline")
    if baseline.scope != "full" or candidate.scope != "full":
        raise ValueError("Partial failed-case reruns cannot establish or compare a full-suite baseline")
    if baseline.suite_cases != candidate.suite_cases:
        raise ValueError("The complete suite task definitions changed")
    before = {result.case_id: result for result in baseline.results}
    after = {result.case_id: result for result in candidate.results}
    if not before or before.keys() != after.keys():
        raise ValueError("Baseline and candidate must contain the same non-empty case set")
    for case_id in before:
        if before[case_id].case_fingerprint != after[case_id].case_fingerprint:
            raise ValueError(f"Task definition changed: {case_id}")
    return EvaluationComparison(
        regressions=tuple(key for key in before if before[key].status == "passed" and after[key].status != "passed"),
        recoveries=tuple(key for key in before if before[key].status != "passed" and after[key].status == "passed"),
        still_failing=tuple(key for key in before if before[key].status != "passed" and after[key].status != "passed"),
    )


async def evaluate_case(
    case: EvaluationCase,
    model: OpenAIModelClient | AnthropicModelClient,
    settings: Settings,
    observability: Observability | None = None,
) -> CaseResult:
    """Run an isolated read-only task through real SDK mechanisms.

    Args:
        case: Maintained task, isolated file fixtures, and oracle.
        model: Actual configured client; tests may replace only this external boundary.
        settings: Deployment configuration supplying the explicit Sandbox image policy.
        observability: Optional host-owned telemetry, including an existing Langfuse binding.

    Returns:
        A scored result. Provider and filesystem faults remain recorded as errors.
    """
    attempted: list[str] = []

    async def capture(event: Event) -> None:
        if isinstance(event, ToolStartedEvent):
            attempted.append(event.tool_code)

    started = time.monotonic()
    try:
        with TemporaryDirectory(prefix="pleiad-eval-") as directory:
            root = Path(directory)
            workspace, scratch = root / "workspace", root / "scratch"
            workspace.mkdir()
            scratch.mkdir()
            for path, content in case.files.items():
                target = workspace / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            registry = ToolRegistry()
            install_default_tools(
                registry,
                workspace=workspace.resolve(),
                scratch=scratch.resolve(),
                sandbox=Sandbox(OciSandbox(settings.sandbox_runtime, observability=observability)),
                sandbox_policy=default_policy(settings.sandbox_image),
            )
            runner = Runner(
                tools=registry,
                skills=SkillLoader(),
                tool_runtime=ToolRuntime(),
                host_policy=HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"file.read"}))),
                event_sink=capture,
                observability=observability,
            )
            agent = Agent(
                name="evaluation",
                model=model,
                prompt=(
                    "Solve the task using the supplied files. Return only the requested JSON object, without Markdown."
                ),
            )
            blocks = (
                (ContextBlock(kind="business_input", origin="evaluation", content=canonical_json(case.input)),)
                if case.input
                else ()
            )
            result = await runner.run(
                agent,
                case.objective,
                memory=ShortMemory.in_memory(case.case_id),
                config=RunConfig(),
                context_blocks=blocks,
            )
            unchanged = all((workspace / path).read_text(encoding="utf-8") == text for path, text in case.files.items())
            allowed = all(tool in {"read_file", "read_tool_result"} for tool in attempted)
            passed = (
                result.status is RunStatus.SUCCEEDED
                and score_output(case, result.final_output)
                and unchanged
                and allowed
            )
            reason = None
            if not allowed:
                reason = "unauthorized_tool_attempt"
            elif not unchanged:
                reason = "fixture_modified"
            elif result.status is not RunStatus.SUCCEEDED:
                reason = result.error.code if result.error is not None else f"run.{result.status.value}"
            elif not passed:
                reason = "answer_mismatch"
            return CaseResult(
                case_id=case.case_id,
                case_fingerprint=case.fingerprint(),
                status="passed" if passed else "failed",
                run_id=result.run_id,
                output=result.final_output,
                error=reason,
                elapsed_seconds=time.monotonic() - started,
                usage=result.usage,
                attempted_tools=tuple(attempted),
            )
    except (PleiadError, OSError, ValueError) as error:
        return CaseResult(
            case_id=case.case_id,
            case_fingerprint=case.fingerprint(),
            status="error",
            error=str(error),
            elapsed_seconds=time.monotonic() - started,
            attempted_tools=tuple(attempted),
        )


@evaluation_app.command("validate")
def validate_suite(suite: Path) -> None:
    """Validate fixed tasks offline without creating a model or Sandbox process.

    Args:
        suite: JSON suite file to validate.
    """
    parsed = EvaluationSuite.model_validate_json(suite.read_text(encoding="utf-8"))
    typer.echo(f"Validated {len(parsed.cases)} maintained task fixtures; no model requests were made.")


@evaluation_app.command("run")
def run_evaluation(
    suite: Path,
    output: Path,
    real_provider: Annotated[bool, typer.Option("--real-provider")] = False,
    failures: Annotated[Path | None, typer.Option("--failures")] = None,
) -> None:
    """Explicitly call the configured provider and write real scored task outcomes.

    Args:
        suite: Complete maintained task definitions.
        output: Destination for the actual result report.
        real_provider: Explicit opt-in to configured remote model requests.
        failures: Optional previous report selecting only failed tasks to rerun.

    Raises:
        typer.BadParameter: If remote execution is not opted in or configured.
        typer.Exit: With status 1 if any executed task fails or errors.
    """
    if not real_provider:
        raise typer.BadParameter("Running tasks requires explicit --real-provider; use validate for an offline check")
    parsed = EvaluationSuite.model_validate_json(suite.read_text(encoding="utf-8"))
    selected = parsed.cases
    if failures is not None:
        selected = failure_cases(parsed, EvaluationReport.model_validate_json(failures.read_text(encoding="utf-8")))
    if not selected:
        raise typer.BadParameter("No failed cases to rerun")
    settings = Settings()
    if not settings.llm_api_key.get_secret_value():
        raise typer.BadParameter("The configured provider credential is missing")

    async def execute() -> EvaluationReport:
        model = create_model(settings)
        try:
            results = [await evaluate_case(case, model, settings) for case in selected]
            return EvaluationReport(
                mode="real_provider",
                provider=settings.llm_provider,
                model=settings.llm_model,
                suite_cases={case.case_id: case.fingerprint() for case in parsed.cases},
                scope="full" if failures is None else "failed_cases",
                results=tuple(results),
            )
        finally:
            await model.aclose()

    report = asyncio.run(execute())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))
    if any(result.status != "passed" for result in report.results):
        raise typer.Exit(1)


@evaluation_app.command("compare")
def compare_evaluations(baseline: Path, candidate: Path) -> None:
    """Compare reports only when every task and fixture identity still agrees.

    Args:
        baseline: Previous actual full-suite report.
        candidate: New actual full-suite report.

    Raises:
        ValueError: If task definitions or report scopes do not agree.
        typer.Exit: With status 1 if a task regressed.
    """
    result = compare_reports(
        EvaluationReport.model_validate_json(baseline.read_text(encoding="utf-8")),
        EvaluationReport.model_validate_json(candidate.read_text(encoding="utf-8")),
    )
    typer.echo(result.model_dump_json(indent=2))
    if result.regressions:
        raise typer.Exit(1)


if __name__ == "__main__":
    evaluation_app()
