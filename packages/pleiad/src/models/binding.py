import hashlib
import json
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pleiad.contracts import JsonValue, thaw_json_object


def credential_field(name: str) -> bool:
    """Recognize credential fields without matching token limits or schema keywords."""
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized in {"key", "ak", "auth", "cookie", "setcookie"} or normalized.endswith(
        ("key", "keyid", "token", "password", "passwd", "secret", "authorization", "credential", "credentials")
    )


def credential_values(value: JsonValue, *, sensitive: bool = False) -> tuple[str, ...]:
    """Find configured secrets so aliases can also be excluded from public data."""
    if isinstance(value, dict):
        return tuple(
            secret
            for key, item in value.items()
            for secret in credential_values(item, sensitive=sensitive or credential_field(key))
        )
    if isinstance(value, list):
        return tuple(secret for item in value for secret in credential_values(item, sensitive=sensitive))
    return (value,) if sensitive and isinstance(value, str) and value and value != "[REDACTED]" else ()


def nonsecret_value(value: JsonValue, secrets: tuple[str, ...]) -> JsonValue:
    """Remove credential fields and mask known aliases in fingerprint inputs."""
    if isinstance(value, dict):
        return {key: nonsecret_value(item, secrets) for key, item in value.items() if not credential_field(key)}
    if isinstance(value, list):
        return [nonsecret_value(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def normalized_base_url(base_url: str | None) -> str | None:
    """Normalize routing information while excluding URL credentials from the digest."""
    if base_url is None:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model base_url requires an absolute HTTP or HTTPS URL")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not credential_field(key) and key.casefold() not in {"sig", "signature", "x-amz-signature"}
    ]
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.removesuffix("/"), urlencode(query), ""))


def binding_fingerprint(
    *,
    protocol: str,
    provider: str,
    base_url: str | None,
    model: str,
    provider_options: Mapping[str, JsonValue],
    adapter_version: str = "1",
    headers: Mapping[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    """Hash non-secret model binding settings used by checkpoint validation.

    Args:
        protocol: Exact wire protocol identifier.
        provider: Provider adapter identifier.
        base_url: Bound endpoint, with credentials supplied separately.
        model: Configured model name.
        provider_options: Non-secret options affecting model requests.
        adapter_version: Adapter contract version for recovery compatibility.
        headers: Optional headers, with credential-bearing headers excluded.
        secrets: Known credentials to mask when repeated in non-secret fields.

    Returns:
        A deterministic SHA-256 fingerprint.

    Raises:
        ValueError: If a setting cannot be serialized or the endpoint is invalid.
    """
    options = thaw_json_object(provider_options)
    header_values: JsonValue = {} if headers is None else {key.lower(): value for key, value in headers.items()}
    credentials = (*secrets, *credential_values(options), *credential_values(header_values))
    if base_url is not None:
        endpoint = urlsplit(base_url)
        credentials = (
            *credentials,
            *(value for value in (endpoint.username, endpoint.password) if value),
            *(value for key, value in parse_qsl(endpoint.query) if credential_field(key)),
        )
    payload = {
        "protocol": protocol,
        "provider": provider,
        "base_url": nonsecret_value(normalized_base_url(base_url), credentials),
        "model": model,
        "provider_options": nonsecret_value(options, credentials),
        "headers": nonsecret_value(header_values, credentials),
        "adapter_version": adapter_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
