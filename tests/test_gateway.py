from __future__ import annotations

import json

import pytest

from ros_mcp.device_registry import DeviceRegistry
from ros_mcp.gateway import MAX_COMMAND_BYTES, Gateway
from ros_mcp.models import CommandStatus, ExecutionState, ResolvedDevice
from ros_mcp.ssh import SSHAdapterError, SSHExecution

FINGERPRINT = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def registry(*, enabled: bool = True) -> DeviceRegistry:
    devices = {
        "main": {
            "display_name": "Main Router",
            "host": "192.0.2.1",
            "username": "ai-mgmt",
            "enabled": enabled,
            "auth": {"type": "password", "password_env": "MAIN_PASSWORD"},
            "host_key": {"fingerprint_sha256": FINGERPRINT},
        }
    }
    return DeviceRegistry.from_env(
        {"ROS_DEVICES_JSON": json.dumps(devices), "MAIN_PASSWORD": "secret"}
    )


def execution(
    *,
    status: str = "succeeded",
    state: str = "completed",
    exit_code: int | None = 0,
    error_code: str | None = None,
    error_message: str | None = None,
) -> SSHExecution:
    return SSHExecution(
        status=status,  # type: ignore[arg-type]
        execution_state=state,  # type: ignore[arg-type]
        exit_code=exit_code,
        stdout="output",
        stderr="error-output" if exit_code else "",
        stdout_total_bytes=6,
        stderr_total_bytes=12 if exit_code else 0,
        stdout_truncated=False,
        stderr_truncated=False,
        error_code=error_code,
        error_message=error_message,
    )


class FakeSSH:
    def __init__(
        self,
        result: SSHExecution | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or execution()
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.closed = 0

    async def execute(self, device: ResolvedDevice, command: str) -> SSHExecution:
        self.calls.append((str(device.device), command))
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed += 1


def test_device_list_is_local_and_contains_no_secret() -> None:
    ssh = FakeSSH()
    gateway = Gateway(registry(), ssh)

    result = gateway.device_list()

    assert [device.device for device in result.devices] == ["main"]
    assert result.devices[0].display_name == "Main Router"
    assert "secret" not in result.model_dump_json()
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_dry_run_validates_without_using_ssh() -> None:
    ssh = FakeSSH()
    gateway = Gateway(registry(), ssh)

    result = await gateway.command_execute("main", "/system resource print")

    assert result.status is CommandStatus.DRY_RUN
    assert result.execution_state is ExecutionState.NOT_STARTED
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_success_and_remote_failure_are_mapped() -> None:
    success_ssh = FakeSSH(execution())
    success = await Gateway(registry(), success_ssh).command_execute(
        "main", "/system resource print", dry_run=False
    )
    failed = await Gateway(
        registry(), FakeSSH(execution(status="failed", exit_code=1))
    ).command_execute("main", "/bad command", dry_run=False)

    assert success.status is CommandStatus.SUCCEEDED
    assert success.exit_code == 0
    assert success_ssh.calls == [("main", "/system resource print")]
    assert failed.status is CommandStatus.FAILED
    assert failed.execution_state is ExecutionState.COMPLETED
    assert failed.error is not None
    assert failed.error.code == "REMOTE_COMMAND_FAILED"


@pytest.mark.asyncio
async def test_unknown_outcome_is_never_retryable() -> None:
    ssh = FakeSSH(
        execution(
            status="unknown",
            state="unknown",
            exit_code=None,
            error_code="COMMAND_TIMEOUT",
            error_message="Command timed out after dispatch; execution outcome is unknown",
        )
    )

    result = await Gateway(registry(), ssh).command_execute("main", "/system reboot", dry_run=False)

    assert result.status is CommandStatus.UNKNOWN
    assert result.execution_state is ExecutionState.UNKNOWN
    assert result.error is not None
    assert result.error.may_have_executed is True
    assert result.error.retryable is False
    assert len(ssh.calls) == 1


@pytest.mark.asyncio
async def test_predispatch_ssh_error_is_structured() -> None:
    ssh = FakeSSH(error=SSHAdapterError("SSH_AUTH_FAILED", "SSH authentication failed"))

    result = await Gateway(registry(), ssh).command_execute(
        "main", "/system resource print", dry_run=False
    )

    assert result.status is CommandStatus.FAILED
    assert result.execution_state is ExecutionState.NOT_STARTED
    assert result.error is not None
    assert result.error.code == "SSH_AUTH_FAILED"
    assert result.error.may_have_executed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device", "command", "code"),
    [
        ("missing", "/system resource print", "DEVICE_NOT_FOUND"),
        ("Main", "/system resource print", "INVALID_DEVICE"),
        ("main", "", "INVALID_COMMAND"),
        ("main", "   ", "INVALID_COMMAND"),
        ("main", "one\ntwo", "INVALID_COMMAND"),
        ("main", "one\x00two", "INVALID_COMMAND"),
        ("main", "x" * (MAX_COMMAND_BYTES + 1), "INVALID_COMMAND"),
    ],
)
async def test_invalid_requests_never_use_ssh(
    device: str,
    command: str,
    code: str,
) -> None:
    ssh = FakeSSH()

    result = await Gateway(registry(), ssh).command_execute(device, command, dry_run=False)

    assert result.status is CommandStatus.FAILED
    assert result.execution_state is ExecutionState.NOT_STARTED
    assert result.error is not None
    assert result.error.code == code
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_disabled_device_and_close() -> None:
    ssh = FakeSSH()
    gateway = Gateway(registry(enabled=False), ssh)

    result = await gateway.command_execute("main", "/system resource print", dry_run=False)
    await gateway.aclose()

    assert result.error is not None
    assert result.error.code == "DEVICE_DISABLED"
    assert ssh.calls == []
    assert ssh.closed == 1
