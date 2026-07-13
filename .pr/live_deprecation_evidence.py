from __future__ import annotations

import importlib
import json
import socket
import threading
import time
import warnings
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import uvicorn

from openhands.agent_server.api import create_app
from openhands.agent_server.mcp_router import MCPTestRequest, MCPTestSuccess
from openhands.agent_server.sub_agents_router import SubAgentInfo
from openhands.sdk.conversation.request import (
    StartACPConversationRequest,
    StartConversationRequest,
)
from openhands.sdk.profiles.resolver import AgentProfileDiagnostics
from openhands.sdk.subagent.schema import AgentDefinition
from openhands.sdk.workspace import LocalWorkspace


def capture[T](operation: Callable[[], T]) -> tuple[T, list[str]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = operation()
    return value, [str(item.message) for item in caught]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_live_openapi() -> tuple[dict[str, Any], dict[str, Any]]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Agent Server did not start")
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"http://127.0.0.1:{port}/openapi.json")
            response.raise_for_status()
            schema = response.json()
            metadata = {
                "status": response.status_code,
                "content_type": response.headers["content-type"].split(";", 1)[0],
                "path_count": len(schema["paths"]),
                "has_mcp_test_path": "/api/mcp/test" in schema["paths"],
            }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
    return schema, metadata


def live_schema_flags(
    openapi: dict[str, Any],
) -> dict[str, dict[str, bool | None]]:
    wanted = {
        "_RemoteMCPServerSpec": "api_key",
        "AgentProfileDiagnostics": "resolved_mcp_servers",
        "AgentDefinition": "mcp_servers",
        "SubAgentInfo": "mcp_servers",
        "MCPTestSuccess": "resolved_mcp_servers",
    }
    schemas = openapi["components"]["schemas"]
    return {
        prefix: {
            name: schema["properties"][field].get("deprecated")
            for name, schema in schemas.items()
            if name.startswith(prefix) and field in schema.get("properties", {})
        }
        for prefix, field in wanted.items()
    }


def deprecated_schema_flags() -> dict[str, bool | None]:
    mcp_schema = MCPTestRequest.model_json_schema()
    remote = mcp_schema["$defs"]["_RemoteMCPServerSpec"]
    return {
        "MCPTestRequest.server.api_key": remote["properties"]["api_key"].get(
            "deprecated"
        ),
        "AgentProfileDiagnostics.resolved_mcp_servers": (
            AgentProfileDiagnostics.model_json_schema()["properties"][
                "resolved_mcp_servers"
            ].get("deprecated")
        ),
        "AgentDefinition.mcp_servers": AgentDefinition.model_json_schema()[
            "properties"
        ]["mcp_servers"].get("deprecated"),
        "SubAgentInfo.mcp_servers": SubAgentInfo.model_json_schema()["properties"][
            "mcp_servers"
        ].get("deprecated"),
        "MCPTestSuccess.resolved_mcp_servers": MCPTestSuccess.model_json_schema()[
            "properties"
        ]["resolved_mcp_servers"].get("deprecated"),
    }


def main() -> None:
    live_openapi, backend_metadata = fetch_live_openapi()
    models = importlib.import_module("openhands.agent_server.models")
    info_alias, info_warnings = capture(lambda: getattr(models, "ACPConversationInfo"))
    page_alias, page_warnings = capture(lambda: getattr(models, "ACPConversationPage"))

    remote, remote_warnings = capture(
        lambda: MCPTestRequest.model_validate(
            {
                "server": {
                    "transport": "http",
                    "url": "https://example.invalid/mcp",
                    "api_key": "sanitized-legacy-token",
                },
                "timeout": 5.0,
            }
        )
    )
    diagnostics, diagnostics_warnings = capture(
        lambda: AgentProfileDiagnostics.model_validate(
            {
                "valid": True,
                "agent_kind": "openhands",
                "resolved_mcp_servers": ["fetch"],
            }
        )
    )
    agent_definition, definition_warnings = capture(
        lambda: AgentDefinition.model_validate(
            {
                "name": "legacy-mcp-agent",
                "mcp_servers": {
                    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}
                },
            }
        )
    )
    acp_request, request_warnings = capture(
        lambda: StartACPConversationRequest(
            agent_profile_id=uuid4(),
            workspace=LocalWorkspace(working_dir="/tmp/pr4004-deprecation"),
        )
    )

    _, supported_warnings = capture(
        lambda: (
            models.ConversationInfo,
            models.ConversationPage,
            MCPTestRequest.model_validate(
                {
                    "server": {
                        "transport": "http",
                        "url": "https://example.invalid/mcp",
                        "auth": {
                            "strategy": "bearer",
                            "value": "sanitized-supported-token",
                        },
                    },
                    "timeout": 5.0,
                }
            ),
            AgentProfileDiagnostics.model_validate(
                {
                    "valid": True,
                    "agent_kind": "openhands",
                    "resolved_mcp_config_keys": ["fetch"],
                }
            ),
            AgentDefinition.model_validate(
                {
                    "name": "supported-mcp-agent",
                    "mcp_config": {
                        "fetch": {
                            "command": "uvx",
                            "args": ["mcp-server-fetch"],
                        }
                    },
                }
            ),
            StartConversationRequest(
                agent_profile_id=uuid4(),
                workspace=LocalWorkspace(working_dir="/tmp/pr4004-supported"),
            ),
        )
    )

    auth = remote.resolved_server.auth
    assert auth is not None and auth.strategy == "bearer"
    assert diagnostics.__dict__.get("resolved_mcp_servers") == [
        "fetch"
    ] or diagnostics.resolved_mcp_config_keys == ["fetch"]
    assert agent_definition.mcp_config is not None
    assert info_alias is models.ConversationInfo
    assert page_alias is models.ConversationPage
    assert isinstance(acp_request, StartConversationRequest)

    warning_sets = {
        "ACPConversationInfo": info_warnings,
        "ACPConversationPage": page_warnings,
        "MCPTestRequest.server.api_key": remote_warnings,
        "AgentProfileDiagnostics.resolved_mcp_servers": diagnostics_warnings,
        "AgentDefinition.mcp_servers": definition_warnings,
        "StartACPConversationRequest": request_warnings,
        "supported_paths": supported_warnings,
    }
    print(
        json.dumps(
            {
                "warning_counts": {
                    name: len(messages) for name, messages in warning_sets.items()
                },
                "warning_messages": warning_sets,
                "schema_deprecated": deprecated_schema_flags(),
                "live_agent_server": backend_metadata,
                "live_openapi_deprecated": live_schema_flags(live_openapi),
                "compatibility": {
                    "info_alias_identity": True,
                    "page_alias_identity": True,
                    "legacy_api_key_strategy": auth.strategy,
                    "legacy_diagnostics_canonical": (
                        diagnostics.resolved_mcp_config_keys
                    ),
                    "legacy_diagnostics_alias": diagnostics.__dict__.get(
                        "resolved_mcp_servers"
                    ),
                    "legacy_agent_server_names": sorted(agent_definition.mcp_config),
                    "legacy_request_is_supported_request": True,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
