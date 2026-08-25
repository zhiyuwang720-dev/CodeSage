"""流块 → 消息的增量组装器:原始块按流序增量拼装成完整块。

agent 循环逐块记录原始 ``assistant/chunk`` 事件(重放保真),同时
把同一块流喂给本组装器 —— 它是唯一规范的组装算法:流结束后读
``blocks()`` / ``message()`` / ``usage`` / ``finish``,取消截断
时读 ``interrupted_blocks()``。

容错设计:容忍只有 delta 的协议(没有 block-start/end);对已被
``block-end`` 关闭的索引再来的 delta 一律忽略(畸形流)—— 不端
的适配器不能靠乱发块撑爆内存或破坏已完成的块。``block-end``
携带权威块,第一次关闭胜出。
"""

from __future__ import annotations

__all__ = ["BlockAssembler"]


class BlockAssembler:
    """增量把原始流块组装成完整内容块与最终 assistant 消息。"""

    def __init__(self) -> None:
        #: 索引 → 部分块;``order`` 记录索引的到达顺序
        self._partials: dict[int, dict] = {}
        self._order: list[int] = []
        self._usage: dict | None = None
        self._finish: dict | None = None
        self._replay_state: dict | None = None

    def push(self, chunk: dict) -> None:
        """按流序喂入一个原始块。"""
        chunk_type = chunk.get("type")
        if chunk_type == "block-start":
            index = chunk["index"]
            if index not in self._partials:
                self._order.append(index)
                self._partials[index] = {
                    "blockType": chunk["blockType"],
                    "text": "",
                    "toolCallArguments": "",
                }
            return
        if chunk_type in ("text-delta", "reasoning-delta"):
            partial = self._ensure(chunk["index"], "text" if chunk_type == "text-delta" else "reasoning")
            if partial.get("block") is not None:
                return  # 已被 block-end 关闭;忽略掉队块
            partial["text"] += chunk.get("text", "")
            return
        if chunk_type == "tool-call-delta":
            partial = self._ensure(chunk["index"], "tool-call")
            if partial.get("block") is not None:
                return
            partial["toolCallId"] = chunk.get("id")
            if chunk.get("name") is not None:
                partial["toolCallName"] = chunk["name"]
            partial["toolCallArguments"] += chunk.get("argumentsDelta", "")
            return
        if chunk_type == "block-end":
            block = chunk["block"]
            partial = self._ensure(chunk["index"], block["type"])
            if partial.get("block") is not None:
                return  # 首次关闭胜出;重复关闭的掉队块忽略
            partial["block"] = block
            return
        if chunk_type == "usage":
            self._usage = chunk.get("usage")
            return
        if chunk_type == "finish":
            self._finish = chunk.get("reason")
            self._replay_state = chunk.get("replayState")
            return
        # 未知块类型:忽略(delta-only 协议之外的宽容)

    def _ensure(self, index: int, block_type: str) -> dict:
        partial = self._partials.get(index)
        if partial is None:
            partial = {"blockType": block_type, "text": "", "toolCallArguments": ""}
            self._partials[index] = partial
            self._order.append(index)
        return partial

    def _assemble(self, partial: dict, index: int) -> dict:
        if partial.get("block") is not None:
            return partial["block"]
        block_type = partial["blockType"]
        if block_type == "text":
            return {"type": "text", "text": partial["text"]}
        if block_type == "reasoning":
            return {"type": "reasoning", "text": partial["text"]}
        if block_type == "tool-call":
            return {
                "type": "tool-call",
                "id": partial.get("toolCallId") or f"call-{index}",
                "name": partial.get("toolCallName") or "",
                "arguments": partial["toolCallArguments"],
            }
        raise ValueError(f'cannot assemble incomplete block of type "{block_type}"')

    def _must_get(self, index: int) -> dict:
        partial = self._partials.get(index)
        if partial is None:
            raise RuntimeError(f"BlockAssembler invariant violated: no partial for index {index}")
        return partial

    def _assembled(self) -> tuple[list, dict | None]:
        """所有已见块的一次保留/丢弃决策:max-token 截断丢弃不可安全执行的工具调用。

        已发块与重放元数据都从这一份结果派生,两者不可能分歧。
        """
        all_blocks = [self._assemble(self._must_get(i), i) for i in self._order]
        kept = None
        if self.finish.get("kind") == "max-tokens":
            kept = [block["type"] != "tool-call" for block in all_blocks]
        blocks = all_blocks if kept is None else [b for b, k in zip(all_blocks, kept) if k]
        envelope = self._replay_state
        if envelope is None or envelope.get("blocks") is None:
            return blocks, envelope
        if len(envelope["blocks"]) != len(all_blocks):
            return blocks, None  # 条目与已发块不对齐:丢弃重放元数据
        if kept is None or len(blocks) == len(all_blocks):
            return blocks, envelope
        return blocks, {
            "response": envelope["response"],
            "blocks": [b for b, k in zip(envelope["blocks"], kept) if k],
        }

    def blocks(self) -> list:
        """按流序组装所有已见块。

        max-token 截断会丢弃无法安全执行的工具调用;未关闭的开放
        块从其累积 delta 组装(从未被 block-end 关闭的未知类型抛错)。
        """
        return self._assembled()[0]

    def interrupted_blocks(self) -> list:
        """中断流可安全定稿的前缀:已关闭与开放的 text/reasoning 块。

        工具调用被省略 —— 中断发生在派发之前,保留它需要伪造结果;
        开放的未知块同样省略。全部为空时返回空列表。
        """
        kept: list[dict] = []
        for index in self._order:
            partial = self._must_get(index)
            block_type = partial["block"].get("type") if partial.get("block") is not None else partial.get("blockType")
            if block_type not in ("text", "reasoning"):
                continue
            block = self._assemble(partial, index)
            if block.get("text", "").strip() != "":
                kept.append(block)
        return kept

    @property
    def usage(self) -> dict | None:
        """usage 块带来的用量;未到达时为 None。"""
        return self._usage

    @property
    def finish(self) -> dict:
        """finish 块带来的结束原因;流无结束块时为 ``{kind: 'stop'}``。"""
        return self._finish if self._finish is not None else {"kind": "stop"}

    @property
    def replay_state(self) -> dict | None:
        """终末 finish 块的每块重放元数据(与 blocks() 同步裁剪)。

        包络条目与已发块不对齐时返回 None(无法安全对齐)。
        """
        return self._assembled()[1]

    def message(self, source: dict | None = None) -> dict:
        """组装后的 assistant 消息:对 blocks() 加 role 与身份。

        source 缺省为插件归属(dsh-llm/assembler)—— 调用方(agent
        循环)总是显式传模型来源。
        """
        from .messages import create_message

        return create_message({
            "role": "assistant",
            "content": self.blocks(),
            "source": source if source is not None else {"kind": "plugin", "plugin": "dsh-llm/assembler"},
        })
