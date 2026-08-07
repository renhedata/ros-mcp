"""Core RouterOS gateway exposed through the two MCP tools."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from pathlib import Path

from .device_registry import DeviceNotFoundError, DeviceRegistry
from .models import (
    DEVICE_ID_PATTERN,
    CommandError,
    CommandResult,
    CommandStatus,
    DeviceListResult,
    ExecutionState,
)
from .ssh import ParamikoSSHAdapter, SSHAdapterError, SSHCommandPort, SSHExecution

MAX_COMMAND_BYTES = 8 * 1024
_DEVICE_ID_RE = re.compile(DEVICE_ID_PATTERN)


class Gateway:
    """Hide device selection, validation, SSH lifecycle, and result mapping."""

    def __init__(self, registry: DeviceRegistry, ssh: SSHCommandPort) -> None:
        self._registry = registry
        self._ssh = ssh

    def device_list(self) -> DeviceListResult:
        """Return configured devices without performing network I/O."""

        return self._registry.list_devices()

    async def command_execute(
        self,
        device: str,
        command: str,
        dry_run: bool = True,
    ) -> CommandResult:
        """Validate and optionally execute one RouterOS command on one device."""

        started = time.perf_counter_ns()

        request_error = _validate_request(device, command)
        if request_error is not None:
            return _failed_before_dispatch(device, request_error, started)

        try:
            resolved = self._registry.get(device)
        except DeviceNotFoundError:
            return _failed_before_dispatch(
                device,
                CommandError(
                    code="DEVICE_NOT_FOUND",
                    message=f"Device {device!r} is not configured",
                ),
                started,
            )

        if not resolved.enabled:
            return _failed_before_dispatch(
                device,
                CommandError(
                    code="DEVICE_DISABLED",
                    message=f"Device {device!r} is disabled",
                ),
                started,
            )

        if dry_run:
            return CommandResult(
                device=device,
                status=CommandStatus.DRY_RUN,
                execution_state=ExecutionState.NOT_STARTED,
                duration_ms=_elapsed_ms(started),
            )

        try:
            execution = await self._ssh.execute(resolved, command)
        except SSHAdapterError as exc:
            return _failed_before_dispatch(
                device,
                CommandError(
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    may_have_executed=False,
                ),
                started,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failed_before_dispatch(
                device,
                CommandError(
                    code="INTERNAL_ERROR",
                    message="SSH execution failed before an outcome was available",
                ),
                started,
            )

        return _map_execution(device, execution, started)

    async def aclose(self) -> None:
        """Close all reusable SSH sessions."""

        await self._ssh.aclose()


def create_gateway_from_env(
    environ: Mapping[str, str] | None = None,
) -> Gateway:
    """Build the production Gateway from an environment snapshot."""

    return Gateway(
        registry=DeviceRegistry.from_env(environ),
        ssh=ParamikoSSHAdapter(),
    )


def create_gateway_from_file(
    path: str | Path,
    environ: Mapping[str, str] | None = None,
) -> Gateway:
    """Build the production Gateway from a device registry file and environment secrets."""

    return Gateway(
        registry=DeviceRegistry.from_file(path, environ),
        ssh=ParamikoSSHAdapter(),
    )


def _validate_request(device: object, command: object) -> CommandError | None:
    if not isinstance(device, str) or _DEVICE_ID_RE.fullmatch(device) is None:
        return CommandError(
            code="INVALID_DEVICE",
            message=f"device must match {DEVICE_ID_PATTERN}",
        )
    if not isinstance(command, str):
        return CommandError(code="INVALID_COMMAND", message="command must be a string")
    if not command.strip():
        return CommandError(code="INVALID_COMMAND", message="command cannot be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in command):
        return CommandError(
            code="INVALID_COMMAND",
            message="command must be a single line without control characters",
        )
    try:
        command_size = len(command.encode("utf-8"))
    except UnicodeEncodeError:
        return CommandError(code="INVALID_COMMAND", message="command must be valid UTF-8")
    if command_size > MAX_COMMAND_BYTES:
        return CommandError(
            code="INVALID_COMMAND",
            message=f"command exceeds the {MAX_COMMAND_BYTES}-byte limit",
        )
    return None


def _failed_before_dispatch(
    device: object,
    error: CommandError,
    started: int,
) -> CommandResult:
    return CommandResult(
        device=device if isinstance(device, str) else str(device),
        status=CommandStatus.FAILED,
        execution_state=ExecutionState.NOT_STARTED,
        duration_ms=_elapsed_ms(started),
        error=error,
    )


def _map_execution(
    device: str,
    execution: SSHExecution,
    started: int,
) -> CommandResult:
    common = {
        "device": device,
        "exit_code": execution.exit_code,
        "stdout": execution.stdout,
        "stderr": execution.stderr,
        "stdout_truncated": execution.stdout_truncated,
        "stderr_truncated": execution.stderr_truncated,
        "duration_ms": _elapsed_ms(started),
    }

    if execution.status == "succeeded":
        return CommandResult(
            **common,
            status=CommandStatus.SUCCEEDED,
            execution_state=ExecutionState.COMPLETED,
        )

    if execution.status == "failed":
        return CommandResult(
            **common,
            status=CommandStatus.FAILED,
            execution_state=ExecutionState.COMPLETED,
            error=CommandError(
                code=execution.error_code or "REMOTE_COMMAND_FAILED",
                message=execution.error_message or "Remote command exited with a non-zero status",
            ),
        )

    return CommandResult(
        **common,
        status=CommandStatus.UNKNOWN,
        execution_state=ExecutionState.UNKNOWN,
        error=CommandError(
            code=execution.error_code or "EXECUTION_OUTCOME_UNKNOWN",
            message=execution.error_message or "Command execution outcome is unknown",
            retryable=False,
            may_have_executed=True,
        ),
    )


def _elapsed_ms(started: int) -> int:
    return max(0, (time.perf_counter_ns() - started) // 1_000_000)
