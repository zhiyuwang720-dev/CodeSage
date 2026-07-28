from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

class AgentType(Enum):
    GENERAL = "general"


class AgentPattern(Enum):
    REACT = "react"
    PLAN_AND_EXECUTE = "plan_execute"
    REFLECTION = "reflection"

