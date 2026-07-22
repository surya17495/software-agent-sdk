"""Tests for ``_compose_conversation_info`` lifting ACP ``configOptions`` (G8).

Parallels the ``current_model_id`` / ``available_models`` lifting tests: the
ACP session's advertised ``configOptions`` live in an ``ACPAgent`` PrivateAttr
(``AgentBase`` is frozen), so the agent-server lifts them off the live agent
(falling back to persisted ``agent_state``) onto
``ConversationInfo.config_options`` for the shell's dynamic pickers.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import SecretStr

from openhands.agent_server.conversation_service import _compose_conversation_info
from openhands.agent_server.models import ConversationInfo, StoredConversation
from openhands.agent_server.utils import utc_now
from openhands.sdk import LLM, Agent, Tool
from openhands.sdk.agent.acp_agent import ACPAgent
from openhands.sdk.agent.acp_models import ACPConfigOption, ACPConfigOptionChoice
from openhands.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.workspace import LocalWorkspace


def _make_state(agent) -> ConversationState:
    workspace = LocalWorkspace(working_dir="/tmp/test")
    return ConversationState(
        id=uuid4(),
        agent=agent,
        workspace=workspace,
        execution_status=ConversationExecutionStatus.IDLE,
        confirmation_policy=NeverConfirm(),
    )


def _make_stored(state: ConversationState) -> StoredConversation:
    workspace = LocalWorkspace(working_dir=state.workspace.working_dir)
    return StoredConversation(
        id=state.id,
        agent=state.agent,
        workspace=workspace,
        title="Test",
        metrics=None,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _sample_options() -> list[ACPConfigOption]:
    return [
        ACPConfigOption(
            id="reasoning_effort",
            name="Reasoning effort",
            type="select",
            current_value="high",
            choices=[
                ACPConfigOptionChoice(value="low", name="Low"),
                ACPConfigOptionChoice(value="high", name="High"),
            ],
        ),
        ACPConfigOption(id="thinking", type="boolean", current_value=True),
    ]


def test_config_options_lifted_from_acp_agent():
    agent = ACPAgent(acp_command=["echo", "test"])
    agent._config_options = _sample_options()
    state = _make_state(agent)
    stored = _make_stored(state)

    info = _compose_conversation_info(stored, state)

    assert isinstance(info, ConversationInfo)
    assert info.config_options == _sample_options()


def test_config_options_empty_when_server_omits_them():
    agent = ACPAgent(acp_command=["echo", "test"])
    state = _make_state(agent)
    stored = _make_stored(state)

    info = _compose_conversation_info(stored, state)

    assert info.config_options == []


def test_config_options_empty_for_native_openhands_agent():
    agent = Agent(
        llm=LLM(model="gpt-4o", api_key=SecretStr("test-key"), usage_id="test-llm"),
        tools=[Tool(name="TerminalTool")],
    )
    state = _make_state(agent)
    stored = _make_stored(state)

    info = _compose_conversation_info(stored, state)

    assert info.config_options == []


def test_config_options_read_from_persisted_agent_state():
    # Cold read: PrivateAttr empty, options coerced back from agent_state dicts.
    agent = ACPAgent(acp_command=["echo", "test"])
    state = _make_state(agent)
    state.agent_state = {
        "acp_config_options": [
            {
                "id": "reasoning_effort",
                "name": "Reasoning effort",
                "type": "select",
                "current_value": "high",
                "choices": [{"value": "low"}, {"value": "high"}],
            }
        ]
    }
    stored = _make_stored(state)

    info = _compose_conversation_info(stored, state)

    assert [o.id for o in info.config_options] == ["reasoning_effort"]
    assert info.config_options[0].current_value == "high"


def test_live_agent_options_take_precedence_over_persisted_state():
    agent = ACPAgent(acp_command=["echo", "test"])
    agent._config_options = [ACPConfigOption(id="live", type="boolean")]
    state = _make_state(agent)
    state.agent_state = {
        "acp_config_options": [{"id": "stale", "type": "boolean"}],
    }
    stored = _make_stored(state)

    info = _compose_conversation_info(stored, state)

    assert [o.id for o in info.config_options] == ["live"]
