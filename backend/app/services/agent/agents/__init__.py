"""Lazy agent package exports."""

__all__ = [
    "BaseAgent",
    "AgentConfig",
    "AgentResult",
    "TaskHandoff",
    "OrchestratorAgent",
    "ReconAgent",
    "AnalysisAgent",
    "ScanAgent",
    "TriageAgent",
    "FindingAgent",
    "VerificationAgent",
]


def __getattr__(name: str):
    if name in {"BaseAgent", "AgentConfig", "AgentResult", "TaskHandoff"}:
        from .base import AgentConfig, AgentResult, BaseAgent, TaskHandoff

        mapping = {
            "BaseAgent": BaseAgent,
            "AgentConfig": AgentConfig,
            "AgentResult": AgentResult,
            "TaskHandoff": TaskHandoff,
        }
        return mapping[name]
    if name == "OrchestratorAgent":
        from .orchestrator import OrchestratorAgent

        return OrchestratorAgent
    if name == "ReconAgent":
        from .recon import ReconAgent

        return ReconAgent
    if name == "AnalysisAgent":
        from .analysis import AnalysisAgent

        return AnalysisAgent
    if name == "ScanAgent":
        from .scan import ScanAgent

        return ScanAgent
    if name == "TriageAgent":
        from .triage import TriageAgent

        return TriageAgent
    if name == "FindingAgent":
        from .finding import FindingAgent

        return FindingAgent
    if name == "VerificationAgent":
        from .verification import VerificationAgent

        return VerificationAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")