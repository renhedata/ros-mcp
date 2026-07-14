"""Asynchronous, multi-device SSH command execution over Paramiko."""

from __future__ import annotations

import asyncio
import hmac
import io
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import paramiko

if TYPE_CHECKING:
    from ros_mcp.models import ResolvedDevice


ExecutionStatus = Literal["succeeded", "failed", "unknown"]
ExecutionState = Literal["completed", "unknown"]


@dataclass(frozen=True, slots=True)
class SSHExecution:
    """Result of a command whose dispatch outcome is known or explicitly unknown."""

    status: ExecutionStatus
    execution_state: ExecutionState
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    error_code: str | None = None
    error_message: str | None = None


class SSHAdapterError(RuntimeError):
    """A failure known to have happened before a command was dispatched."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.may_have_executed = False


class SSHCommandPort(Protocol):
    async def execute(self, device: ResolvedDevice, command: str) -> SSHExecution: ...

    async def aclose(self) -> None: ...


class _FingerprintMismatch(paramiko.SSHException):
    pass


class _PrivateKeyLoadError(paramiko.SSHException):
    pass


class _FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    """Accept exactly one configured SHA256 host-key fingerprint."""

    def __init__(self, expected: str) -> None:
        self._expected = expected

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        del client, hostname
        _verify_fingerprint(key, self._expected)


@dataclass(slots=True)
class _ByteCollector:
    limit: int
    buffer: bytearray = field(default_factory=bytearray)
    total: int = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self.buffer)
        if remaining > 0:
            self.buffer.extend(chunk[:remaining])

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        return self.buffer.decode("utf-8", errors="replace")


@dataclass(slots=True)
class _DeviceState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0
    client: Any | None = None
    last_used_at: float = 0.0


@dataclass(slots=True)
class _AttemptFailure(Exception):
    cause: Exception
    dispatched: bool
    stdout: _ByteCollector
    stderr: _ByteCollector


ClientFactory = Callable[[], Any]


class ParamikoSSHAdapter:
    """Reuse one SSH session per device while preserving per-device FIFO order."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = paramiko.SSHClient,
        max_workers: int = 8,
        max_pending_jobs: int = 16,
        per_device_queue_size: int = 32,
        poll_interval_seconds: float = 0.01,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_pending_jobs < 0:
            raise ValueError("max_pending_jobs cannot be negative")
        if per_device_queue_size < 0:
            raise ValueError("per_device_queue_size cannot be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._client_factory = client_factory
        self._per_device_queue_size = per_device_queue_size
        self._poll_interval_seconds = poll_interval_seconds
        self._states: dict[str, _DeviceState] = {}
        self._state_guard = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._all_requests_done = asyncio.Event()
        self._all_requests_done.set()
        self._accepted_requests = 0
        self._closing = False
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ros-mcp-ssh",
        )
        # ThreadPoolExecutor's internal queue is unbounded. This gate bounds submitted work.
        self._worker_slots = asyncio.Semaphore(max_workers + max_pending_jobs)

    async def execute(self, device: ResolvedDevice, command: str) -> SSHExecution:
        device_id = str(device.device)
        state, counted_waiter = await self._accept_request(device_id)
        acquired = False
        try:
            await state.lock.acquire()
            acquired = True
            if counted_waiter:
                state.waiting -= 1
                counted_waiter = False
            return await self._run_blocking(self._execute_with_retry, state, device, command)
        finally:
            if counted_waiter:
                state.waiting -= 1
            if acquired:
                state.lock.release()
            await self._finish_request()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            async with self._state_guard:
                self._closing = True

            await self._all_requests_done.wait()
            clients = [state.client for state in self._states.values() if state.client is not None]
            for state in self._states.values():
                state.client = None
                state.last_used_at = 0.0
            await self._run_blocking(self._close_clients, clients)
            self._closed = True
            await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    async def _accept_request(self, device_id: str) -> tuple[_DeviceState, bool]:
        async with self._state_guard:
            if self._closing or self._closed:
                raise SSHAdapterError("SSH_ADAPTER_CLOSED", "SSH executor is closed")
            state = self._states.setdefault(device_id, _DeviceState())
            queued = state.lock.locked() or state.waiting > 0
            if queued and state.waiting >= self._per_device_queue_size:
                raise SSHAdapterError(
                    "DEVICE_QUEUE_FULL",
                    f"SSH command queue is full for device {device_id!r}",
                    retryable=True,
                )
            if queued:
                state.waiting += 1
            self._accepted_requests += 1
            self._all_requests_done.clear()
            return state, queued

    async def _finish_request(self) -> None:
        async with self._state_guard:
            self._accepted_requests -= 1
            if self._accepted_requests == 0:
                self._all_requests_done.set()

    async def _run_blocking(self, function: Callable[..., Any], *args: Any) -> Any:
        async with self._worker_slots:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._executor, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Do not release a device lock while its Paramiko operation is still running.
                await asyncio.shield(future)
                raise

    def _execute_with_retry(
        self,
        state: _DeviceState,
        device: ResolvedDevice,
        command: str,
    ) -> SSHExecution:
        for attempt in range(2):
            try:
                return self._execute_once(state, device, command)
            except _AttemptFailure as failure:
                self._drop_client(state)
                if failure.dispatched:
                    return self._unknown_execution(failure)
                if attempt == 0 and _is_retryable_pre_dispatch(failure.cause):
                    continue
                raise _public_pre_dispatch_error(failure.cause) from failure.cause
        raise AssertionError("unreachable")

    def _execute_once(
        self,
        state: _DeviceState,
        device: ResolvedDevice,
        command: str,
    ) -> SSHExecution:
        limit = int(device.output_limit_bytes)
        if limit < 0:
            raise SSHAdapterError(
                "INVALID_DEVICE_CONFIGURATION",
                "output_limit_bytes cannot be negative",
            )
        stdout = _ByteCollector(limit)
        stderr = _ByteCollector(limit)
        channel: Any | None = None
        dispatched = False
        try:
            client = self._ensure_client(state, device)
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise ConnectionError("SSH transport is not active")

            deadline = time.monotonic() + float(device.command_timeout_seconds)
            channel = transport.open_session(timeout=_remaining(deadline))
            channel.settimeout(_remaining(deadline))
            # From this point forward, the server may have received the command request.
            dispatched = True
            channel.exec_command(command)
            exit_code = self._drain_channel(
                channel,
                transport,
                stdout,
                stderr,
                deadline,
            )
            state.last_used_at = time.monotonic()
            return SSHExecution(
                status="succeeded" if exit_code == 0 else "failed",
                execution_state="completed",
                exit_code=exit_code,
                stdout=stdout.text(),
                stderr=stderr.text(),
                stdout_total_bytes=stdout.total,
                stderr_total_bytes=stderr.total,
                stdout_truncated=stdout.truncated,
                stderr_truncated=stderr.truncated,
            )
        except Exception as exc:
            raise _AttemptFailure(exc, dispatched, stdout, stderr) from exc
        finally:
            _safe_close_channel(channel)

    def _ensure_client(self, state: _DeviceState, device: ResolvedDevice) -> Any:
        client = state.client
        if client is not None:
            idle_ttl = int(device.idle_ttl_seconds)
            idle_expired = idle_ttl >= 0 and time.monotonic() - state.last_used_at >= idle_ttl
            transport = client.get_transport()
            if idle_expired or transport is None or not transport.is_active():
                self._drop_client(state)
                client = None
        if client is None:
            client = self._connect(device)
            state.client = client
            state.last_used_at = time.monotonic()
        return client

    def _connect(self, device: ResolvedDevice) -> Any:
        fingerprint = str(device.fingerprint_sha256)
        if not fingerprint.startswith("SHA256:"):
            raise SSHAdapterError(
                "INVALID_DEVICE_CONFIGURATION",
                "fingerprint_sha256 must be an OpenSSH SHA256 fingerprint",
            )

        client = self._client_factory()
        try:
            client.set_missing_host_key_policy(_FingerprintPolicy(fingerprint))
            auth_type = str(device.auth_type)
            connect_kwargs: dict[str, Any] = {
                "hostname": str(device.host),
                "port": int(device.port),
                "username": str(device.username),
                "timeout": float(device.connect_timeout_seconds),
                "banner_timeout": float(device.connect_timeout_seconds),
                "auth_timeout": float(device.connect_timeout_seconds),
                "channel_timeout": float(device.command_timeout_seconds),
                "allow_agent": False,
                "look_for_keys": False,
            }
            if auth_type == "password":
                password = _secret_value(device.password)
                if password is None:
                    raise SSHAdapterError(
                        "INVALID_DEVICE_CONFIGURATION",
                        "password authentication requires a password",
                    )
                connect_kwargs["password"] = password
            elif auth_type == "private_key":
                private_key = _secret_value(device.private_key)
                if private_key is None:
                    raise SSHAdapterError(
                        "INVALID_DEVICE_CONFIGURATION",
                        "private-key authentication requires private_key",
                    )
                connect_kwargs["pkey"] = _load_private_key(
                    private_key,
                    _secret_value(device.passphrase),
                )
            else:
                raise SSHAdapterError(
                    "INVALID_DEVICE_CONFIGURATION",
                    f"unsupported SSH auth type {auth_type!r}",
                )

            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise ConnectionError("SSH transport did not become active")
            _verify_fingerprint(transport.get_remote_server_key(), fingerprint)
            keepalive = int(device.keepalive_seconds)
            if keepalive > 0:
                transport.set_keepalive(keepalive)
            return client
        except Exception:
            _safe_close_client(client)
            raise

    def _drain_channel(
        self,
        channel: Any,
        transport: Any,
        stdout: _ByteCollector,
        stderr: _ByteCollector,
        deadline: float,
    ) -> int:
        while True:
            if channel.recv_ready():
                stdout.add(channel.recv(32 * 1024))
            if channel.recv_stderr_ready():
                stderr.add(channel.recv_stderr(32 * 1024))

            if channel.exit_status_ready():
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    exit_code = int(channel.recv_exit_status())
                    if exit_code == -1 and not transport.is_active():
                        raise ConnectionError("SSH transport closed before confirming exit status")
                    return exit_code
            elif not transport.is_active():
                raise ConnectionError("SSH transport closed before command completion")

            remaining = _remaining(deadline)
            time.sleep(min(self._poll_interval_seconds, remaining))

    def _unknown_execution(self, failure: _AttemptFailure) -> SSHExecution:
        timed_out = isinstance(failure.cause, (TimeoutError, socket.timeout))
        return SSHExecution(
            status="unknown",
            execution_state="unknown",
            exit_code=None,
            stdout=failure.stdout.text(),
            stderr=failure.stderr.text(),
            stdout_total_bytes=failure.stdout.total,
            stderr_total_bytes=failure.stderr.total,
            stdout_truncated=failure.stdout.truncated,
            stderr_truncated=failure.stderr.truncated,
            error_code="COMMAND_TIMEOUT" if timed_out else "EXECUTION_OUTCOME_UNKNOWN",
            error_message=(
                "Command timed out after dispatch; execution outcome is unknown"
                if timed_out
                else "SSH failed after command dispatch; execution outcome is unknown"
            ),
        )

    @staticmethod
    def _close_clients(clients: list[Any]) -> None:
        for client in clients:
            _safe_close_client(client)

    @staticmethod
    def _drop_client(state: _DeviceState) -> None:
        client = state.client
        state.client = None
        state.last_used_at = 0.0
        _safe_close_client(client)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("SSH command timed out")
    return remaining


def _verify_fingerprint(key: Any, expected: str) -> None:
    actual = getattr(key, "fingerprint", None)
    if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
        raise _FingerprintMismatch("SSH host-key fingerprint does not match configuration")


def _secret_value(value: Any | None) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter is not None else value)


def _load_private_key(private_key: str, passphrase: str | None) -> paramiko.PKey:
    errors: list[Exception] = []
    key_types = (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey)
    for key_type in key_types:
        try:
            return key_type.from_private_key(io.StringIO(private_key), password=passphrase)
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(exc)
    message = "private key is invalid, unsupported, or has a wrong passphrase"
    raise _PrivateKeyLoadError(message) from (errors[-1] if errors else None)


def _is_retryable_pre_dispatch(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            SSHAdapterError,
            _FingerprintMismatch,
            _PrivateKeyLoadError,
            paramiko.AuthenticationException,
            paramiko.BadHostKeyException,
            paramiko.PasswordRequiredException,
        ),
    ):
        return False
    return isinstance(exc, (OSError, TimeoutError, paramiko.SSHException))


def _public_pre_dispatch_error(exc: Exception) -> SSHAdapterError:
    if isinstance(exc, SSHAdapterError):
        return exc
    if isinstance(exc, (_FingerprintMismatch, paramiko.BadHostKeyException)):
        return SSHAdapterError(
            "SSH_HOST_KEY_MISMATCH",
            "SSH host-key fingerprint does not match configuration",
        )
    if isinstance(exc, (_PrivateKeyLoadError, paramiko.PasswordRequiredException)):
        return SSHAdapterError("SSH_PRIVATE_KEY_INVALID", str(exc))
    if isinstance(exc, paramiko.AuthenticationException):
        return SSHAdapterError("SSH_AUTH_FAILED", "SSH authentication failed")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SSHAdapterError(
            "SSH_CONNECT_TIMEOUT",
            "SSH connection or channel setup timed out",
            retryable=True,
        )
    return SSHAdapterError(
        "SSH_CONNECT_FAILED",
        "SSH connection or channel setup failed",
        retryable=True,
    )


def _safe_close_channel(channel: Any | None) -> None:
    if channel is not None:
        try:
            channel.close()
        except Exception:
            pass


def _safe_close_client(client: Any | None) -> None:
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
