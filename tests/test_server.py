from __future__ import annotations

from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

import ros_mcp.server as server_module
from ros_mcp.models import (
    AuthType,
    CommandResult,
    CommandStatus,
    DeviceListResult,
    DeviceSummary,
    ExecutionState,
)
from ros_mcp.server import create_server


def make_device_list_result() -> DeviceListResult:
    return DeviceListResult(
        devices=(
            DeviceSummary(
                device="main",
                display_name="Main Router",
                description="Internet edge",
                host="192.168.88.1",
                port=22,
                username="ai-mgmt",
                auth_type=AuthType.PASSWORD,
                enabled=True,
                tags=("production", "edge"),
                connect_timeout_seconds=15,
                command_timeout_seconds=60,
                keepalive_seconds=30,
                idle_ttl_seconds=300,
                output_limit_bytes=1_048_576,
            ),
        ),
    )


class FakeGateway:
    def __init__(self) -> None:
        self.list_calls = 0
        self.command_calls: list[tuple[str, str, bool]] = []
        self.close_calls = 0

    def device_list(self) -> DeviceListResult:
        self.list_calls += 1
        return make_device_list_result()

    async def command_execute(
        self,
        device: str,
        command: str,
        dry_run: bool = True,
    ) -> CommandResult:
        self.command_calls.append((device, command, dry_run))
        if dry_run:
            return CommandResult(
                device=device,
                status=CommandStatus.DRY_RUN,
                execution_state=ExecutionState.NOT_STARTED,
                duration_ms=0,
            )
        return CommandResult(
            device=device,
            status=CommandStatus.SUCCEEDED,
            execution_state=ExecutionState.COMPLETED,
            exit_code=0,
            stdout="uptime: 1d",
            duration_ms=12,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


async def test_server_registers_only_the_two_public_tools() -> None:
    server = create_server(FakeGateway)

    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {"device_list", "command_execute"}

    device_list = tools["device_list"]
    assert device_list.inputSchema["properties"] == {}
    assert device_list.outputSchema == DeviceListResult.model_json_schema()

    command_execute = tools["command_execute"]
    assert command_execute.inputSchema["properties"] == {
        "device": {"title": "Device", "type": "string"},
        "command": {"title": "Command", "type": "string"},
        "dry_run": {"default": True, "title": "Dry Run", "type": "boolean"},
    }
    assert command_execute.inputSchema["required"] == ["device", "command"]
    assert command_execute.outputSchema == CommandResult.model_json_schema()
    assert "must not be retried automatically" in command_execute.description


async def test_tools_share_one_gateway_and_close_it_with_the_session() -> None:
    gateway = FakeGateway()
    factory_calls = 0

    def factory() -> FakeGateway:
        nonlocal factory_calls
        factory_calls += 1
        return gateway

    server = create_server(factory)  # type: ignore[arg-type]
    assert factory_calls == 0

    async with create_connected_server_and_client_session(server) as session:
        assert factory_calls == 1

        listed = await session.call_tool("device_list", {})
        dry_run = await session.call_tool(
            "command_execute",
            {"device": "main", "command": "/system resource print"},
        )
        executed = await session.call_tool(
            "command_execute",
            {
                "device": "main",
                "command": "/system resource print",
                "dry_run": False,
            },
        )

        assert listed.isError is False
        assert listed.structuredContent == make_device_list_result().model_dump(mode="json")
        assert dry_run.isError is False
        assert dry_run.structuredContent == CommandResult(
            device="main",
            status=CommandStatus.DRY_RUN,
            execution_state=ExecutionState.NOT_STARTED,
            duration_ms=0,
        ).model_dump(mode="json")
        assert executed.isError is False
        assert executed.structuredContent == CommandResult(
            device="main",
            status=CommandStatus.SUCCEEDED,
            execution_state=ExecutionState.COMPLETED,
            exit_code=0,
            stdout="uptime: 1d",
            duration_ms=12,
        ).model_dump(mode="json")

    assert factory_calls == 1
    assert gateway.list_calls == 1
    assert gateway.command_calls == [
        ("main", "/system resource print", True),
        ("main", "/system resource print", False),
    ]
    assert gateway.close_calls == 1


def test_main_runs_stdio_without_writing_to_stdout(monkeypatch: Any, capsys: Any) -> None:
    transports: list[str] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            transports.append(transport)

    monkeypatch.setattr(server_module, "create_server", FakeServer)

    server_module.main()

    assert transports == ["stdio"]
    assert capsys.readouterr().out == ""
