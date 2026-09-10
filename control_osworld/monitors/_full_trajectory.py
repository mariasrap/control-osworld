"""Monitor rates a finished run as a whole.

One model call over the whole trajectory, returning a single 0-100 suspicion
score.

Note: scores are kept on the raw 0-100 scale (CA's built-in monitors normalise
to 0-1). Normalise downstream if a protocol needs it.
"""

from __future__ import annotations

from typing import Literal

from control_arena import ModelRole
from control_arena._control_agent import control_agent
from control_arena._control_task_metadata import control_task_metadata
from control_arena.tools import convert_agent_state_to_monitor_view
from inspect_ai._util.content import ContentText
from inspect_ai.agent import Agent, AgentState
from inspect_ai.model import (
    ChatMessage,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.tool import Tool
from pydantic import Field

from control_osworld.monitors._transcript_builder import (
    USER_POSTAMBLE_FULL,
    USER_PREAMBLE_FULL,
    parse_monitor_response,
    render_trajectory,
    text_note,
    variant_flags,
)
from control_osworld.monitors.prompts import monitor_prompts


@control_agent
def osworld_full_trajectory_monitor(
    variant: Literal["no_text", "with_text", "no_images"] = Field(
        default="with_text",
        description="What the monitor sees: with_text=text+screens+actions, "
        "no_text=screens+actions, no_images=text+actions (no screenshots). "
        "CoT is always hidden.",
    ),
    model_role: ModelRole | None = Field(
        default="trusted",
        description="Model role for the monitor; set None to use `model` directly (e.g. post-hoc).",
    ),
    model: str | None = Field(
        default=None,
        description="Specific monitor model; used when model_role is None or unregistered.",
    ),
    max_tokens: int = 2048,
    max_image_dim: int = 1024,
    image_quality: int = 80,
    task_description: str | None = Field(
        default=None,
        description=(
            "Override for the user task description. When None (inline eval use), "
            "it is taken from the Control Arena task metadata. Post-hoc drivers "
            "must pass it explicitly since there is no live task context."
        ),
    ),
    default_suspicion_score: float = Field(
        default=0.0,
        description="Score recorded when the monitor reply has no parsable <score> tag.",
    ),
) -> Agent:
    """Full-trajectory monitor: one call over the whole trajectory, score in [0, 100].

    Attaches to the last message's metadata: suspicion_score, reason,
    raw_monitor_response, score_parse_error, model_usage.
    """
    include_visible_text, include_images = variant_flags(variant)

    async def impl(state: AgentState, tools: list[Tool]) -> AgentState:
        last_message = state.messages[-1]
        view = convert_agent_state_to_monitor_view(state)

        task_desc = (
            task_description
            if task_description is not None
            else control_task_metadata().main_task_description
        )

        content = (
            [
                ContentText(
                    text=USER_PREAMBLE_FULL.format(
                        task_description=task_desc,
                        text_note=text_note(include_visible_text, include_images),
                    )
                )
            ]
            + render_trajectory(
                view.messages,
                include_visible_text=include_visible_text,
                max_image_dim=max_image_dim,
                image_quality=image_quality,
                include_images=include_images,
            )
            + [ContentText(text=USER_POSTAMBLE_FULL)]
        )
        messages: list[ChatMessage] = [
            ChatMessageSystem(content=monitor_prompts["full_trajectory_system"]),
            ChatMessageUser(content=content),
        ]

        output = await get_model(
            model=model, role=model_role, config=GenerateConfig(max_tokens=max_tokens)
        ).generate(input=messages, cache=False)
        raw = output.completion
        score, reasoning = parse_monitor_response(raw)

        if last_message.metadata is None:
            last_message.metadata = {}
        last_message.metadata.update(
            {
                "suspicion_score": (
                    float(score) if score is not None else default_suspicion_score
                ),
                "reason": reasoning,
                "raw_monitor_response": raw,
                "score_parse_error": score is None,
                "model_usage": output.usage.model_dump() if output.usage else None,
            }
        )
        return state

    return impl
