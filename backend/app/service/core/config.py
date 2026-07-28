from typing import List, Optional, Union

from pydantic import AnyHttpUrl, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LLM_FIRST_TOKEN_TIMEOUT: int = 30
    LLM_STREAM_TIMEOUT: int = 60
    AGENT_TIMEOUT_SECONDS: int = 1800
    SUB_AGENT_TIMEOUT_SECONDS: int = 600
    TOOL_TIMEOUT_SECONDS: int = 60



settings = Settings()

