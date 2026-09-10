"""An agent that does nothing at all.

It submits immediately without touching the machine, which makes it the control
for the rest of the pipeline: anything that still "succeeds" here is being scored
by something other than the agent's work, and any crash is ours rather than the
model's. Costs no model calls.
"""

import uuid

from inspect_ai.agent import Agent, AgentState
from inspect_ai.model import ChatMessageAssistant
from inspect_ai.tool import Tool, ToolCall

from control_arena import control_agent


@control_agent
def osworld_empty_policy() -> Agent:
    async def impl(state: AgentState, tools: list[Tool]) -> AgentState:
        call_id = "submit_" + str(uuid.uuid4())
        state.messages.append(
            ChatMessageAssistant(
                content="",
                tool_calls=[
                    ToolCall(
                        id=call_id, function="submit", arguments={"status": "done"}
                    )
                ],
            )
        )
        return state

    return impl
