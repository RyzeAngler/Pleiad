from collections.abc import Mapping

from jsonschema import Draft202012Validator

from pleiad.contracts import JsonSchema


def validate_json_schema(schema: JsonSchema) -> None:
    """Validate a local JSON Schema without allowing external reference retrieval.

    Args:
        schema: Schema supplied by a host or workspace capability.

    Raises:
        ValueError: If a reference requires an external resource.
        jsonschema.SchemaError: If the schema is invalid.
    """
    reject_external_references(schema)
    Draft202012Validator.check_schema(schema)


def reject_external_references(value: object) -> None:
    """Reject references requiring network or filesystem retrieval.

    Args:
        value: Schema subtree to inspect.

    Raises:
        ValueError: If a reference is not local to this schema document.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str) and not item.startswith("#"):
                raise ValueError("JSON Schema must not resolve external or remote references")
            reject_external_references(item)
    elif isinstance(value, list):
        for item in value:
            reject_external_references(item)
