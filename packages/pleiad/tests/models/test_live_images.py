import asyncio
import hashlib
import io
import json
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import AfterValidator, ConfigDict, Field, SecretStr, ValidationError

from pleiad.agent.agent import Agent
from pleiad.context.budget import ApproximateTokenCounter
from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.images import ImageBlock, ImageReadScope, ImageReference, ResolvedImage
from pleiad.messages.message import TextBlock
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import (
    ImageEditRequest,
    ImageFormat,
    ImageGenerationRequest,
    ImageProvider,
    ImageResult,
)
from pleiad.models.images.errors import ImageOperationError
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.result import RunResult
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.skills.loader import SkillLoader
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime


def validate_key(value: SecretStr) -> SecretStr:
    """Require a nonblank file credential without exposing its value in errors."""
    if not value.get_secret_value().strip():
        raise ValueError("api_key must not be blank")
    return value


type LiveKey = Annotated[SecretStr, AfterValidator(validate_key)]


class VisionSettings(ContractModel):
    """Configure a real image-understanding model and an observable answer."""

    model_config = ConfigDict(hide_input_in_errors=True)
    provider: str = "openai"
    protocol: Literal["openai", "anthropic"] = "openai"
    model: str = Field(min_length=1)
    api_key: LiveKey = Field(repr=False)
    base_url: str | None = None
    provider_options: JsonObject = Field(default_factory=dict)
    prompt: str = Field(min_length=1)
    expected_text: str = Field(min_length=1)
    followup_prompt: str = "Repeat the exact visible text from the previous image. Return only that text."
    max_output_tokens: int = Field(default=2048, gt=0)


class ImageSettings(ContractModel):
    """Configure a native image provider independently of the vision model."""

    model_config = ConfigDict(hide_input_in_errors=True)
    provider: ImageProvider
    model: str = Field(min_length=1)
    api_key: LiveKey = Field(repr=False)
    base_url: str | None = None
    size: str = "auto"
    quality: Literal["auto", "low", "medium", "high"] = "auto"
    output_format: ImageFormat | None = None
    background: Literal["auto", "opaque", "transparent"] = "auto"
    reference_kind: Literal["general", "character"] = "general"
    provider_options: JsonObject = Field(default_factory=dict)
    generation_prompt: str = "A red ceramic mug on a plain white table, product photography."
    edit_prompt: str = "Keep the product unchanged and replace only the background with pale blue."


class LiveSettings(ContractModel):
    """Validate file configuration while redacting credentials from diagnostics."""

    model_config = ConfigDict(hide_input_in_errors=True)
    output_dir: Path
    image_path: Path | None = None
    mask_path: Path | None = None
    vision: VisionSettings | None = None
    images: ImageSettings | None = None


@pytest.fixture
def live_settings(pytestconfig: pytest.Config) -> LiveSettings:
    """Skip real requests unless the caller explicitly selects a configuration."""
    selected: object = pytestconfig.getoption("live_config")
    if selected is None:
        pytest.skip("Real requests require --live-config PATH")
    if not isinstance(selected, Path):
        raise TypeError("--live-config must be a file path")
    return load_settings(selected)


def load_settings(path: Path) -> LiveSettings:
    """Read TOML credentials and resolve asset paths from its file location."""
    path = path.expanduser().resolve(strict=True)
    settings = LiveSettings.model_validate(tomllib.loads(path.read_text()))
    updates: dict[str, Path] = {}
    for name in ("output_dir", "image_path", "mask_path"):
        value = getattr(settings, name)
        if isinstance(value, Path):
            updates[name] = (path.parent / value.expanduser()).resolve()
    return settings.model_copy(update=updates)


def load_asset(path: Path, *, mask: bool = False) -> ResolvedImage:
    """Decode a selected local asset and derive its real dimensions and digest."""
    data = path.read_bytes()
    if len(data) > 10_000_000:
        raise ValueError("Use a test image no larger than 10 MB")
    with Image.open(io.BytesIO(data)) as decoded:
        if decoded.width > 8192 or decoded.height > 8192:
            raise ValueError("Use a test image no larger than 8192 pixels per side")
        # Pillow exposes n_frames only for formats supporting multiple frames.
        if getattr(decoded, "n_frames", 1) != 1:
            raise ValueError("Use a still image for this smoke test")
        decoded.load()
        assert decoded.format is not None
        mime = Image.MIME[decoded.format]
        if mask and (mime != "image/png" or "A" not in decoded.getbands()):
            raise ValueError("The optional mask must be a PNG with an alpha channel")
        digest = hashlib.sha256(data).hexdigest()
        reference = ImageReference.model_validate(
            {
                "reference": f"asset:{digest}",
                "sha256": digest,
                "mime": mime,
                "width": decoded.width,
                "height": decoded.height,
            }
        )
    return ResolvedImage(reference, data)


def new_output(settings: LiveSettings, scenario: str) -> Path:
    """Create a unique artifact directory so repeated paid tests remain distinct."""
    path = settings.output_dir / f"{scenario}-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    print(f"Artifacts: {path}")
    return path


def write_report(path: Path, name: str, payload: JsonObject) -> None:
    """Persist test facts without provider credentials or encoded image payloads."""
    (path / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def save_images(path: Path, result: ImageResult) -> None:
    """Decode and save actual provider outputs for manual visual acceptance."""
    outputs: list[JsonObject] = []
    for index, output in enumerate(result.images):
        with Image.open(io.BytesIO(output.data)) as decoded:
            decoded.load()
            assert decoded.format is not None
            assert Image.MIME[decoded.format] == output.mime
            suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[output.mime]
            name = f"output-{index + 1}.{suffix}"
            (path / name).write_bytes(output.data)
            outputs.append(
                {
                    "file": name,
                    "width": decoded.width,
                    "height": decoded.height,
                    "sha256": hashlib.sha256(output.data).hexdigest(),
                    "revised_prompt": output.revised_prompt,
                }
            )
    write_report(
        path,
        "result.json",
        {
            "operation_id": result.operation_id,
            "provider_request_id": result.provider_request_id,
            "model": result.model_name,
            "reported_model": result.reported_model_name,
            "usage": result.usage.model_dump(mode="json") if result.usage is not None else None,
            "images": list(outputs),
            "completed_at": result.completed_at.isoformat(),
            "visual_acceptance": "pending_manual_review",
        },
    )
    assert len(result.images) == 1


@pytest.mark.provider_e2e
def test_live_vision(live_settings: LiveSettings) -> None:
    settings = live_settings.vision
    assert settings is not None, "Add the [vision] section to the selected config"
    assert live_settings.image_path is not None, "Set image_path to a real image"
    asset = load_asset(live_settings.image_path)
    path = new_output(live_settings, "vision")
    session_id = path.name
    reads: list[ImageReadScope] = []

    async def resolver(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        if scope.session_id != session_id or image != asset.image:
            raise PermissionError("Image is outside this test Session")
        reads.append(scope)
        return asset

    async def scenario() -> None:
        constructor = OpenAIModelClient if settings.protocol == "openai" else AnthropicModelClient
        client = constructor(
            provider=settings.provider,
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            provider_options=settings.provider_options,
            timeout_seconds=180.0,
            max_retries=0,
        )
        runner = Runner(
            tools=ToolRegistry(),
            skills=SkillLoader(),
            tool_runtime=ToolRuntime(),
            image_resolver=resolver,
            token_counter=ApproximateTokenCounter(allow_unestimated_images=True),
        )
        agent = Agent(name="live-vision", model=client, prompt="Answer using the provided image.", tools=())
        try:
            for turn in (1, 2):
                session = Session(session_id, SqliteSessionStore(path / "session.sqlite3"))
                content: str | tuple[ImageBlock | TextBlock, ...] = (
                    (ImageBlock(image=asset.image), TextBlock(text=settings.prompt))
                    if turn == 1
                    else settings.followup_prompt
                )
                input_id = f"turn-{turn}"
                await session.enqueue(
                    InputEnvelope(input_id=input_id, kind="user", source="live-test", content=content)
                )
                handle = await runner.start(
                    agent,
                    session=session,
                    invocation_id=input_id,
                    input_ids=(input_id,),
                    input_mode="batch",
                    limits=RunLimits(max_model_steps=1, max_output_tokens=settings.max_output_tokens),
                )
                async with handle:
                    result = await handle.result()
                assert isinstance(result, RunResult)
                write_report(
                    path,
                    f"turn-{turn}.json",
                    {
                        "run_id": result.run_id,
                        "state": result.state.value,
                        "answer": result.final_output,
                        "usage": result.usage.model_dump(mode="json"),
                        "binding": client.binding_fingerprint,
                    },
                )
                print(f"Turn {turn}: {result.final_output}")
                assert result.state is RunStatus.SUCCEEDED
                assert settings.expected_text.casefold() in result.final_output.casefold()
            assert len(reads) == 2
            assert reads[0].run_id != reads[1].run_id
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.provider_e2e
@pytest.mark.parametrize("operation", ["generate", "edit"])
def test_live_images(live_settings: LiveSettings, operation: Literal["generate", "edit"]) -> None:
    settings = live_settings.images
    assert settings is not None, "Add the [images] section to the selected config"
    assets: list[ResolvedImage] = []
    if operation == "edit":
        assert live_settings.image_path is not None, "Set image_path to the base image"
        assets.append(load_asset(live_settings.image_path))
        if live_settings.mask_path is not None:
            assets.append(load_asset(live_settings.mask_path, mask=True))
    path = new_output(live_settings, operation)
    scope = ImageReadScope(session_id=path.name, run_id=path.name)
    if operation == "edit":
        request: ImageGenerationRequest = ImageEditRequest(
            operation_id=path.name,
            prompt=settings.edit_prompt,
            count=1,
            size=settings.size,
            quality=settings.quality,
            output_format=settings.output_format,
            background=settings.background,
            provider_options=settings.provider_options,
            reference_kind=settings.reference_kind,
            references=(assets[0].image,),
            base_reference=assets[0].image,
            mask=assets[1].image if len(assets) == 2 else None,
        )
    else:
        request = ImageGenerationRequest(
            operation_id=path.name,
            prompt=settings.generation_prompt,
            count=1,
            size=settings.size,
            quality=settings.quality,
            output_format=settings.output_format,
            background=settings.background,
            provider_options=settings.provider_options,
        )
    write_report(path, "request.json", request.model_dump(mode="json"))

    async def resolver(image: ImageReference, requested: ImageReadScope) -> ResolvedImage:
        if requested != scope:
            raise PermissionError("Invalid image-test read scope")
        for asset in assets:
            if asset.image == image:
                return asset
        raise PermissionError("Image is outside this edit operation")

    async def scenario() -> None:
        client = ImageClient(
            provider=settings.provider,
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout_seconds=300.0,
        )
        try:
            write_report(
                path,
                "submission.json",
                {
                    "operation_id": path.name,
                    "binding": client.binding_fingerprint,
                    "provider": settings.provider,
                    "model": settings.model,
                },
            )
            if isinstance(request, ImageEditRequest):
                result = await client.edit(request, resolver=resolver, scope=scope)
            else:
                result = await client.generate(request)
            await asyncio.to_thread(save_images, path, result)
        except ImageOperationError as error:
            write_report(
                path,
                "error.json",
                {
                    "operation_id": error.operation_id,
                    "code": error.code,
                    "provider_request_id": error.provider_request_id,
                },
            )
            raise
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.fixture
def local_config(tmp_path: Path) -> Path:
    """Create an offline configuration with independent example credentials."""
    path = tmp_path / "live.toml"
    path.write_text("""output_dir = "results"
image_path = "product.png"
mask_path = "mask.png"

[vision]
provider = "qwen"
model = "vision-model"
base_url = "https://vision.example/api/v1"
api_key = "fake-vision-key"
prompt = "Read the product code."
expected_text = "ABC123"

[images]
provider = "glm"
model = "glm-image"
base_url = "https://images.example/api/paas/v4"
api_key = "fake-image-key"
""")
    return path


def test_live_file_settings(local_config: Path) -> None:
    settings = load_settings(local_config)
    assert settings.vision is not None
    assert settings.images is not None
    assert settings.vision.api_key.get_secret_value() == "fake-vision-key"
    assert settings.images.api_key.get_secret_value() == "fake-image-key"
    assert settings.vision.base_url == "https://vision.example/api/v1"
    assert settings.images.base_url == "https://images.example/api/paas/v4"
    assert settings.output_dir == local_config.parent / "results"
    assert settings.image_path == local_config.parent / "product.png"
    assert settings.mask_path == local_config.parent / "mask.png"
    for key in ("fake-vision-key", "fake-image-key"):
        assert key not in repr(settings)
        assert key not in settings.model_dump_json()


@pytest.mark.parametrize("key", ["fake-vision-key", "fake-image-key"])
@pytest.mark.parametrize("replacement", ["", "   "])
def test_live_blank_key(local_config: Path, key: str, replacement: str) -> None:
    local_config.write_text(local_config.read_text().replace(key, replacement))
    with pytest.raises(ValidationError, match="api_key must not be blank"):
        load_settings(local_config)


@pytest.mark.parametrize("section,key", [("vision", "fake-vision-key"), ("images", "fake-image-key")])
def test_live_missing_key(local_config: Path, section: str, key: str) -> None:
    local_config.write_text(local_config.read_text().replace(f'api_key = "{key}"\n', ""))
    with pytest.raises(ValidationError) as caught:
        load_settings(local_config)
    assert f"{section}.api_key" in str(caught.value)
    assert "fake-vision-key" not in str(caught.value)
    assert "fake-image-key" not in str(caught.value)


def test_live_config_error_redacts(local_config: Path) -> None:
    local_config.write_text(local_config.read_text().replace('model = "glm-image"\n', ""))
    with pytest.raises(ValidationError) as caught:
        load_settings(local_config)
    assert "images.model" in str(caught.value)
    assert "fake-image-key" not in str(caught.value)
    assert "fake-vision-key" not in str(caught.value)
