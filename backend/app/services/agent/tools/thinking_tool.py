"""
Think 工具 - 深度推理工具

让Agent进行深度思考和推理，用于：
- 分析复杂情况
- 规划下一步行动
- 评估发现的严重性
- 决定是否需要创建子Agent
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field

from .base import AgentTool, ToolResult

logger = logging.getLogger(__name__)


class ThinkInput(BaseModel):
    """Think工具输入参数"""
    thought: str = Field(
        ...,
        description="思考内容，可以是分析、规划、评估等"
    )
    category: Optional[str] = Field(
        default="general",
        description="思考类别: analysis(分析), planning(规划), evaluation(评估), decision(决策)"
    )


class ThinkTool(AgentTool):
    """
    Think 工具
    
    这是一个让Agent进行深度推理的工具。Agent可以用它来：
    - 分析复杂情况：当面对复杂的代码逻辑或不确定的漏洞线索时
    - 规划下一步行动：在执行具体操作之前先规划策略
    - 评估发现的严重性：发现可疑点后评估其真实性和影响
    - 决定是否需要分解任务：当任务变得复杂时分析是否需要创建子Agent
    
    Think工具的输出会被记录到Agent的对话历史中，帮助LLM保持思路的连贯性。
    """
    
    @property
    def name(self) -> str:
        return "think"
    
    @property
    def description(self) -> str:
        return """深度思考工具。用于：
1. 分析复杂的代码逻辑或安全问题
2. 规划下一步的分析策略
3. 评估发现的漏洞是否真实存在
4. 决定是否需要深入调查某个方向

使用此工具记录你的推理过程，这有助于保持分析的连贯性。

参数:
- thought: 你的思考内容
- category: 思考类别 (analysis/planning/evaluation/decision)"""
    
    @property
    def args_schema(self):
        return ThinkInput

    def is_read_only(self, **kwargs) -> bool:
        del kwargs
        return True
    
    async def _execute(
        self,
        thought: str,
        category: str = "general",
        **kwargs
    ) -> ToolResult:
        """
        执行思考
        
        实际上这个工具不执行任何操作，只是记录思考内容。
        但它的存在让Agent有一个"思考"的动作，有助于推理。
        """
        if not thought or not thought.strip():
            return ToolResult(
                success=False,
                error="思考内容不能为空",
            )
        
        thought = thought.strip()
        
        # 根据类别添加标记
        category_labels = {
            "analysis": "🔍 分析",
            "planning": "📋 规划",
            "evaluation": "⚖️ 评估",
            "decision": "🎯 决策",
            "general": "💭 思考",
        }
        
        label = category_labels.get(category, "💭 思考")
        
        logger.debug(f"Think tool called: [{label}] {thought[:100]}...")
        
        return ToolResult(
            success=True,
            data={
                "message": f"思考已记录 ({len(thought)} 字符)",
                "category": category,
                "label": label,
            },
            metadata={
                "thought": thought,
                "category": category,
                "char_count": len(thought),
            }
        )


class ReflectTool(AgentTool):
    """
    反思工具
    
    让Agent回顾和总结当前的分析进展
    """
    
    @property
    def name(self) -> str:
        return "reflect"
    
    @property
    def description(self) -> str:
        return """反思工具。用于回顾当前的分析进展：
1. 总结已经发现的问题
2. 评估当前分析的覆盖度
3. 识别可能遗漏的方向
4. 决定是否需要调整策略

参数:
- summary: 当前进展总结
- findings_so_far: 目前发现的问题数量
- coverage: 分析覆盖度评估 (low/medium/high)
- next_steps: 建议的下一步行动"""
    
    @property
    def args_schema(self):
        return None

    def is_read_only(self, **kwargs) -> bool:
        del kwargs
        return True
    
    async def _execute(
        self,
        summary: str = "",
        findings_so_far: int = 0,
        coverage: str = "medium",
        next_steps: str = "",
        **kwargs
    ) -> ToolResult:
        """执行反思"""
        reflection = {
            "summary": summary,
            "findings_count": findings_so_far,
            "coverage": coverage,
            "next_steps": next_steps,
        }
        
        return ToolResult(
            success=True,
            data={
                "message": "反思已记录",
                "reflection": reflection,
            },
            metadata=reflection,
        )
