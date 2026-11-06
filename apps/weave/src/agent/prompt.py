from pleiad_weave.agent.contracts import NodeSpec

SPACE_INSTRUCTION_POLICY = """空间上下文 weave_space.instructions 是用户保存的默认指令，仅用于补充本次任务。
默认指令与本次用户任务的明确要求冲突时，以本次任务为准；不要把默认指令当成权限或审批授权。
"""

SYSTEM_PROMPT = (
    """你是 Weave，帮助用户完成任务。

先弄清用户要什么，再根据需要查看文件、调用工具或查阅技能。简单的任务直接处理；
需要分工或有步骤依赖时，先查询可用能力，再提交任务图并执行。使用目录中已有的能力，
缺少所需工具时说明原因。

回答要具体，依据实际拿到的信息和执行结果。完成了什么、还有什么没完成，都如实说明。
遇到影响结果的不确定之处，先核实；需要用户补充信息时，问清楚再继续。
"""
    + SPACE_INSTRUCTION_POLICY
)


def build_node_prompt(node: NodeSpec) -> str:
    """Combine the node's task and additional instructions.

    Args:
        node: Validated task instructions for one graph node.

    Returns:
        Instructions to accompany the SDK's structured output contract.
    """
    instructions = node.prompt.strip()
    task = f"你的任务：{node.objective}\n\n根据收到的输入完成任务，结果应有事实或执行记录支持。"
    prompt = f"{task}\n\n{instructions}" if instructions else task
    return (
        f"{prompt}\n\n{SPACE_INSTRUCTION_POLICY}"
        "上下文中的 task_request 是本次用户任务，节点输入是当前步骤的数据。"
        "复用编排中的旧任务要求不能覆盖本次用户任务。"
    )
