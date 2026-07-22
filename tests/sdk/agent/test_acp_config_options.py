"""Tests for the ACP ``configOptions`` relay (gap G8).

Covers the SDK boundary that surfaces a live ACP session's advertised
``configOptions`` onto ``ConversationInfo.config_options`` and lets a client
change them via ``session/set_config_option``:

* ``ACPConfigOption.from_protocol`` / ``ACPConfigOptionChoice`` normalization,
* ``_extract_session_config_options`` (absent vs empty vs present),
* the ``ACPAgent.config_options`` property, and
* ``ACPAgent.set_acp_config_option`` (the live round-trip + error mapping).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp.exceptions import RequestError as ACPRequestError

from openhands.sdk.agent.acp_agent import ACPAgent, _extract_session_config_options
from openhands.sdk.agent.acp_models import ACPConfigOption, ACPConfigOptionChoice


def _make_agent(**kwargs: Any) -> ACPAgent:
    return ACPAgent(acp_command=["echo", "test"], **kwargs)


def _agent_conn(agent: ACPAgent) -> Any:
    assert agent._conn is not None
    return cast(Any, agent._conn)


def _select(*, id="mode", current_value=None, options=(), name=None, category=None):
    return SimpleNamespace(
        id=id,
        type="select",
        name=name,
        description=None,
        category=category,
        current_value=current_value,
        options=list(options),
    )


def _boolean(*, id="thinking", current_value=None, name=None):
    return SimpleNamespace(
        id=id,
        type="boolean",
        name=name,
        description=None,
        category=None,
        current_value=current_value,
    )


def _choice(value, name=None, description=None):
    return SimpleNamespace(value=value, name=name, description=description)


# ---------------------------------------------------------------------------
# ACPConfigOption.from_protocol / ACPConfigOptionChoice
# ---------------------------------------------------------------------------


class TestACPConfigOptionFromProtocol:
    def test_normalizes_select(self):
        opt = ACPConfigOption.from_protocol(
            _select(
                id="reasoning_effort",
                name="Reasoning effort",
                current_value="high",
                options=[_choice("low", "Low"), _choice("high", "High", "Slow")],
            )
        )
        assert opt is not None
        assert opt.id == "reasoning_effort"
        assert opt.type == "select"
        assert opt.current_value == "high"
        assert opt.choices == [
            ACPConfigOptionChoice(value="low", name="Low"),
            ACPConfigOptionChoice(value="high", name="High", description="Slow"),
        ]

    def test_normalizes_boolean_by_type(self):
        opt = ACPConfigOption.from_protocol(_boolean(current_value=True))
        assert opt is not None
        assert opt.type == "boolean"
        assert opt.current_value is True
        assert opt.choices == []

    def test_infers_boolean_from_bool_current_value_without_type(self):
        # boolean options carry no ``type`` discriminator on the wire; infer it.
        raw = SimpleNamespace(id="plan_mode", current_value=False)
        opt = ACPConfigOption.from_protocol(raw)
        assert opt is not None
        assert opt.type == "boolean"
        assert opt.current_value is False

    def test_unwraps_rootmodel(self):
        wrapped = SimpleNamespace(
            root=_select(current_value="x", options=[_choice("x")])
        )
        opt = ACPConfigOption.from_protocol(wrapped)
        assert opt is not None
        assert opt.id == "mode"

    def test_flattens_groups_and_records_label(self):
        group = SimpleNamespace(
            name="Fast", options=[_choice("haiku", "Haiku"), _choice("sonnet")]
        )
        opt = ACPConfigOption.from_protocol(
            _select(id="model", current_value="haiku", options=[group])
        )
        assert opt is not None
        assert [c.value for c in opt.choices] == ["haiku", "sonnet"]
        assert all(c.group == "Fast" for c in opt.choices)

    def test_drops_entry_without_usable_id(self):
        assert ACPConfigOption.from_protocol(_select(id="")) is None
        assert ACPConfigOption.from_protocol(SimpleNamespace(id=42)) is None

    def test_drops_unrenderable_type(self):
        raw = SimpleNamespace(id="mystery", type="slider", current_value="x")
        assert ACPConfigOption.from_protocol(raw) is None

    def test_skips_choices_without_value(self):
        opt = ACPConfigOption.from_protocol(
            _select(
                current_value="ok", options=[_choice(42), _choice("ok"), _choice("")]
            )
        )
        assert opt is not None
        assert [c.value for c in opt.choices] == ["ok"]


# ---------------------------------------------------------------------------
# _extract_session_config_options: absent vs empty vs present
# ---------------------------------------------------------------------------


class TestExtractSessionConfigOptions:
    def test_none_response(self):
        assert _extract_session_config_options(None) is None

    def test_absent_field_is_none(self):
        # A response with no ``config_options`` at all -> None (preserve last-known).
        assert _extract_session_config_options(SimpleNamespace(models=None)) is None

    def test_empty_field_is_empty_list(self):
        # The server reported the field but has no options -> [] (clear).
        resp = SimpleNamespace(config_options=[])
        assert _extract_session_config_options(resp) == []

    def test_present_options_normalized(self):
        resp = SimpleNamespace(
            config_options=[
                _select(
                    id="reasoning_effort",
                    current_value="high",
                    options=[_choice("low"), _choice("high")],
                ),
                _boolean(id="thinking", current_value=True),
            ]
        )
        opts = _extract_session_config_options(resp)
        assert opts is not None
        assert [o.id for o in opts] == ["reasoning_effort", "thinking"]
        assert opts[0].type == "select"
        assert opts[1].type == "boolean"

    def test_model_select_included_verbatim(self):
        # The relay is honest/complete: the model select is present (the shell
        # skips id=="model" itself to avoid a duplicate control).
        resp = SimpleNamespace(
            config_options=[
                _select(id="model", current_value="x", options=[_choice("x")])
            ]
        )
        opts = _extract_session_config_options(resp)
        assert opts is not None
        assert [o.id for o in opts] == ["model"]

    def test_unrenderable_entries_dropped(self):
        resp = SimpleNamespace(
            config_options=[
                SimpleNamespace(id="", type="select"),  # no id -> dropped
                _boolean(id="ok", current_value=False),
            ]
        )
        opts = _extract_session_config_options(resp)
        assert opts is not None
        assert [o.id for o in opts] == ["ok"]


# ---------------------------------------------------------------------------
# ACPAgent.config_options property
# ---------------------------------------------------------------------------


class TestConfigOptionsProperty:
    def test_defaults_to_empty(self):
        assert _make_agent().config_options == []

    def test_reflects_private_attr(self):
        agent = _make_agent()
        opts = [ACPConfigOption(id="thinking", type="boolean", current_value=True)]
        agent._config_options = opts
        assert agent.config_options == opts

    def test_returns_a_copy(self):
        agent = _make_agent()
        agent._config_options = [ACPConfigOption(id="a", type="boolean")]
        got = agent.config_options
        got.append(ACPConfigOption(id="injected", type="boolean"))
        assert [o.id for o in agent.config_options] == ["a"]


# ---------------------------------------------------------------------------
# ACPAgent.set_acp_config_option: live round-trip + error mapping
# ---------------------------------------------------------------------------


class TestSetACPConfigOption:
    @staticmethod
    def _wire(agent: ACPAgent) -> ACPAgent:
        conn = MagicMock()
        conn.set_config_option = AsyncMock(
            return_value=SimpleNamespace(
                config_options=[
                    _select(
                        id="reasoning_effort",
                        current_value="high",
                        options=[_choice("low"), _choice("high")],
                    )
                ]
            )
        )
        agent._conn = conn
        agent._session_id = "sess-1"
        agent._agent_name = "opencode-acp"
        executor = MagicMock()

        def _run(coro: Any, timeout: Any = None) -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        executor.run_async = MagicMock(side_effect=_run)
        agent._executor = executor
        return agent

    def test_sets_select_and_refreshes_options(self):
        agent = self._wire(_make_agent())
        agent.set_acp_config_option("reasoning_effort", "high")
        _agent_conn(agent).set_config_option.assert_awaited_once_with(
            config_id="reasoning_effort", value="high", session_id="sess-1"
        )
        # Surfaced state refreshed from the authoritative response.
        assert [o.id for o in agent.config_options] == ["reasoning_effort"]
        assert agent.config_options[0].current_value == "high"

    def test_sets_boolean_value(self):
        agent = self._wire(_make_agent())
        agent.set_acp_config_option("thinking", True)
        _agent_conn(agent).set_config_option.assert_awaited_once_with(
            config_id="thinking", value=True, session_id="sess-1"
        )

    def test_rejects_empty_config_id(self):
        agent = self._wire(_make_agent())
        with pytest.raises(ValueError, match="non-empty"):
            agent.set_acp_config_option("   ", "x")
        _agent_conn(agent).set_config_option.assert_not_called()

    def test_raises_before_session_initialized(self):
        agent = _make_agent()  # no _conn / _session_id / _executor
        with pytest.raises(RuntimeError, match="not initialized"):
            agent.set_acp_config_option("mode", "x")

    def test_client_rejection_becomes_value_error(self):
        # A -32602 invalid-params (unknown option / bad value) surfaces as a
        # ValueError so the agent-server route maps it to a 400.
        agent = self._wire(_make_agent())
        _agent_conn(agent).set_config_option.side_effect = ACPRequestError(
            code=-32602, message="Invalid params"
        )
        with pytest.raises(ValueError, match="rejected set_config_option"):
            agent.set_acp_config_option("mode", "bogus")

    def test_server_internal_error_propagates(self):
        # -32603 is a retriable server-internal failure -> propagate as-is (5xx).
        agent = self._wire(_make_agent())
        _agent_conn(agent).set_config_option.side_effect = ACPRequestError(
            code=-32603, message="internal error"
        )
        with pytest.raises(ACPRequestError):
            agent.set_acp_config_option("mode", "x")

    def test_passes_timeout_to_run_async(self):
        agent = self._wire(_make_agent(acp_prompt_timeout=42.0))
        agent.set_acp_config_option("mode", "x")
        _, kwargs = agent._executor.run_async.call_args
        assert kwargs["timeout"] == 42.0
