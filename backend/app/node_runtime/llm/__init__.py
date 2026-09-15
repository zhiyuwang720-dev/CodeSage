"""模型边界模块：唯一 LiteLLM SDK 出口、配置快照与 usage 归一。

不允许在此模块之外出现 `litellm.acompletion` 调用或厂商协议分派。
"""

from .client import SDKModelClient, get_sdk_client, reset_sdk_client
from .config import (
    MODEL_BOUNDARY_VERSION,
    LLMConfig,
    LLMRequest,
    ModelCatalog,
    ModelConfigurationError,
    get_model_capabilities,
    get_provider_metadata,
)
from .errors import ModelBoundaryError
from .service import LLMService
from .types import LLMProvider, LLMResponse, LLMUsage
from .usage import NORMALIZATION_VERSION, normalize_usage

__all__ = [
    "LLMService",
    "LLMConfig",
    "LLMRequest",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "MODEL_BOUNDARY_VERSION",
    "ModelBoundaryError",
    "ModelCatalog",
    "ModelConfigurationError",
    "NORMALIZATION_VERSION",
    "SDKModelClient",
    "get_model_capabilities",
    "get_provider_metadata",
    "get_sdk_client",
    "normalize_usage",
    "reset_sdk_client",
]
