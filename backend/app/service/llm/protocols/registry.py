"""Protocol registry and provider metadata."""

from ..types import LLMProvider


def canonical_endpoint_protocol(protocol: str) -> str:
    """Normalize endpoint protocol string."""
    return protocol.strip().lower()


def canonical_tool_message_format(fmt: str) -> str:
    """Normalize tool message format string."""
    if not fmt:
        return "auto"
    return fmt.strip().lower()


def get_provider_metadata(provider: LLMProvider) -> dict:
    """Get metadata for a provider. Returns empty dict for now."""
    return {}

