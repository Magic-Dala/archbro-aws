from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from archbro.backend.core.contracts import AgentDecision, ProjectContext, ProjectEvent


class GoalConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class GoalDraft(BaseModel):
    suggested_project_name: str = "Untitled Project"
    goal: str = ""
    assistant_message: str
    ready: bool = False
    missing_information: list[str] = Field(default_factory=list)


class ModelProvider(ABC):
    name: str
    model_id: str

    async def draft_goal(
        self,
        *,
        messages: list[GoalConversationMessage],
        current_goal: str = "",
    ) -> GoalDraft:
        """Merge Ask conversation into the current Goal draft without destructive replacement."""
        raise NotImplementedError(f"{type(self).__name__} does not support goal drafting")

    @abstractmethod
    async def generate(
        self,
        *,
        event: ProjectEvent,
        context: ProjectContext,
        system_prompt: str,
    ) -> AgentDecision:
        raise NotImplementedError

    async def generate_with_external_tools(
        self,
        *,
        event: ProjectEvent,
        context: ProjectContext,
        system_prompt: str,
        external_tools: Any,
    ) -> AgentDecision:
        """Optional tool-aware entry point; providers remain backward compatible."""

        return await self.generate(
            event=event,
            context=context,
            system_prompt=system_prompt,
        )
