from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from fastapi import WebSocket
from pydantic import ValidationError

from openhands.agent_server.event_router import search_conversation_events
from openhands.agent_server.sockets import _send_event
from openhands.sdk.event import SystemPromptEvent
from openhands.sdk.llm import TextContent
from openhands.sdk.mcp.config import dump_mcp_config
from openhands.sdk.settings.model import (
    AGENT_SETTINGS_SCHEMA_VERSION,
    OpenHandsAgentSettings,
    validate_agent_settings,
)
from openhands.sdk.tool.builtins import FinishTool


class _FakeEventService:
    async def search_events(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "kind": "SystemPromptEvent",
                    "id": "system_event",
                    "parent_id": "parent_event",
                    "system_prompt": {"type": "text", "text": "system"},
                    "tools": [
                        {"kind": "FinishTool"},
                        {"kind": "VisionInspectTool"},
                    ],
                }
            ],
            "next_page_id": None,
        }


class _CaptureWebSocket:
    application_state = object()
    client_state = object()

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


def verify_settings_migration() -> dict[str, Any]:
    settings = validate_agent_settings(
        {
            "schema_version": 4,
            "agent_kind": "openhands",
            "llm": {"model": "openhands/gpt-5.5"},
            "mcp_config": {
                "mcpServers": {
                    "superhuman-mail": {
                        "url": "https://mcp.mail.superhuman.com/mcp",
                        "transport": "http",
                        "auth": {
                            "strategy": "oauth2",
                            "state": {
                                "tokens": {"access_token": "token-value"},
                                "client_info": {"client_id": "client-id"},
                                "token_expires_at": 1234567890.0,
                            },
                        },
                    }
                }
            },
        }
    )
    assert isinstance(settings, OpenHandsAgentSettings)
    assert settings.schema_version == AGENT_SETTINGS_SCHEMA_VERSION == 5

    dumped_servers = dump_mcp_config(settings.mcp_config)
    assert set(dumped_servers) == {"superhuman-mail"}
    server = dumped_servers["superhuman-mail"]
    assert server["auth"]["strategy"] == "oauth2"
    assert server["auth"]["state"]["tokens"]["access_token"] == "token-value"
    assert server["auth"]["state"]["client_info"]["client_id"] == "client-id"

    return {
        "schema_version": settings.schema_version,
        "mcp_server_names": sorted(dumped_servers),
        "has_mcpServers_wrapper": "mcpServers" in dumped_servers,
        "oauth_strategy": server["auth"]["strategy"],
        "oauth_access_token": server["auth"]["state"]["tokens"]["access_token"],
        "oauth_client_id": server["auth"]["state"]["client_info"]["client_id"],
    }


def verify_current_wrapper_rejection() -> dict[str, Any]:
    try:
        validate_agent_settings(
            {
                "schema_version": AGENT_SETTINGS_SCHEMA_VERSION,
                "agent_kind": "openhands",
                "mcp_config": {
                    "mcpServers": {"weather": {"url": "https://mcp.example.com/mcp"}}
                },
            }
        )
    except ValidationError as exc:
        return {
            "rejected": True,
            "error_count": len(exc.errors()),
            "first_error_type": exc.errors()[0]["type"],
        }

    raise AssertionError("current-version mcpServers wrapper unexpectedly validated")


async def verify_event_transport() -> dict[str, Any]:
    search_response = await search_conversation_events(
        event_service=_FakeEventService()
    )
    search_payload = json.loads(search_response.body)
    search_item = search_payload["items"][0]
    assert search_item["parent_id"] == "parent_event"
    assert [tool["kind"] for tool in search_item["tools"]] == [
        "FinishTool",
        "VisionInspectTool",
    ]

    websocket = _CaptureWebSocket()
    await _send_event(
        SystemPromptEvent(
            id="websocket_system_event",
            parent_id="websocket_parent",
            system_prompt=TextContent(text="system"),
            tools=list(FinishTool.create()),
        ),
        cast(WebSocket, websocket),
    )
    websocket_item = websocket.payloads[0]
    assert websocket_item["parent_id"] == "websocket_parent"

    return {
        "search_parent_id": search_item["parent_id"],
        "search_tool_kinds": [tool["kind"] for tool in search_item["tools"]],
        "websocket_parent_id": websocket_item["parent_id"],
        "websocket_tool_kinds": [tool["kind"] for tool in websocket_item["tools"]],
    }


async def main() -> None:
    result = {
        "settings_migration": verify_settings_migration(),
        "current_wrapper_rejection": verify_current_wrapper_rejection(),
        "event_transport": await verify_event_transport(),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
