from __future__ import annotations

import asyncio
import io
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from ros_mcp.ssh import ParamikoSSHAdapter, SSHAdapterError, _load_private_key

FINGERPRINT = "SHA256:test-fingerprint"


@dataclass(slots=True)
class Device:
    device: str
    host: str = "192.0.2.1"
    port: int = 22
    username: str = "ai-mgmt"
    auth_type: str = "password"
    password: str | None = "secret"
    private_key: str | None = None
    passphrase: str | None = None
    fingerprint_sha256: str = FINGERPRINT
    connect_timeout_seconds: float = 0.2
    command_timeout_seconds: float = 0.5
    keepalive_seconds: int = 30
    idle_ttl_seconds: int = 300
    output_limit_bytes: int = 1024


class FakeKey:
    def __init__(self, fingerprint: str = FINGERPRINT) -> None:
        self.fingerprint = fingerprint


class FakeChannel:
    def __init__(
        self,
        *,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        exit_code: int = 0,
        delay: float = 0.0,
        exec_error: Exception | None = None,
        tracker: CommandTracker | None = None,
    ) -> None:
        self.stdout = deque(stdout or [])
        self.stderr = deque(stderr or [])
        self.exit_code = exit_code
        self.delay = delay
        self.exec_error = exec_error
        self.tracker = tracker
        self.started_at: float | None = None
        self.closed = False
        self.command: str | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def exec_command(self, command: str) -> None:
        self.command = command
        self.started_at = time.monotonic()
        if self.tracker is not None:
            self.tracker.enter()
        if self.exec_error is not None:
            raise self.exec_error

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, size: int) -> bytes:
        del size
        return self.stdout.popleft()

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, size: int) -> bytes:
        del size
        return self.stderr.popleft()

    def exit_status_ready(self) -> bool:
        assert self.started_at is not None
        return time.monotonic() - self.started_at >= self.delay

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        if not self.closed and self.tracker is not None and self.started_at is not None:
            self.tracker.leave()
        self.closed = True


class CommandTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()

    def leave(self) -> None:
        with self._lock:
            self.active -= 1


class FakeTransport:
    def __init__(
        self,
        channels: list[FakeChannel] | None = None,
        *,
        fingerprint: str = FINGERPRINT,
        open_error: Exception | None = None,
    ) -> None:
        self.channels = deque(channels or [])
        self.fingerprint = fingerprint
        self.open_error = open_error
        self.active = True
        self.keepalive: int | None = None
        self.open_calls = 0

    def is_active(self) -> bool:
        return self.active

    def get_remote_server_key(self) -> FakeKey:
        return FakeKey(self.fingerprint)

    def set_keepalive(self, interval: int) -> None:
        self.keepalive = interval

    def open_session(self, timeout: float) -> FakeChannel:
        del timeout
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error
        return self.channels.popleft()


class FakeClient:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.policy: Any | None = None
        self.connect_calls = 0
        self.closed = False

    def set_missing_host_key_policy(self, policy: Any) -> None:
        self.policy = policy

    def connect(self, **kwargs: Any) -> None:
        self.connect_calls += 1
        self.connect_kwargs = kwargs
        assert self.policy is not None
        self.policy.missing_host_key(
            self,
            kwargs["hostname"],
            self.transport.get_remote_server_key(),
        )

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.closed = True
        self.transport.active = False


class ClientFactory:
    def __init__(self, clients: list[FakeClient]) -> None:
        self.clients = deque(clients)
        self.created: list[FakeClient] = []

    def __call__(self) -> FakeClient:
        client = self.clients.popleft()
        self.created.append(client)
        return client


@pytest.mark.asyncio
async def test_same_device_is_serialized_in_fifo_order() -> None:
    tracker = CommandTracker()
    channels = [
        FakeChannel(delay=0.05, tracker=tracker),
        FakeChannel(delay=0.01, tracker=tracker),
    ]
    client = FakeClient(FakeTransport(channels))
    adapter = ParamikoSSHAdapter(
        client_factory=ClientFactory([client]),
        poll_interval_seconds=0.001,
    )

    first = asyncio.create_task(adapter.execute(Device("main"), "first"))
    await asyncio.to_thread(tracker.entered.wait, 0.2)
    second = asyncio.create_task(adapter.execute(Device("main"), "second"))
    results = await asyncio.gather(first, second)

    assert [result.status for result in results] == ["succeeded", "succeeded"]
    assert [channel.command for channel in channels] == ["first", "second"]
    assert tracker.max_active == 1
    assert client.connect_calls == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_different_devices_execute_in_parallel() -> None:
    tracker = CommandTracker()
    first_client = FakeClient(FakeTransport([FakeChannel(delay=0.05, tracker=tracker)]))
    second_client = FakeClient(FakeTransport([FakeChannel(delay=0.05, tracker=tracker)]))
    adapter = ParamikoSSHAdapter(
        client_factory=ClientFactory([first_client, second_client]),
        max_workers=2,
        poll_interval_seconds=0.001,
    )

    await asyncio.gather(
        adapter.execute(Device("main"), "one"),
        adapter.execute(Device("office"), "two"),
    )

    assert tracker.max_active == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_per_device_waiting_queue_is_bounded() -> None:
    tracker = CommandTracker()
    client = FakeClient(
        FakeTransport(
            [
                FakeChannel(delay=0.05, tracker=tracker),
                FakeChannel(tracker=tracker),
            ]
        )
    )
    adapter = ParamikoSSHAdapter(
        client_factory=ClientFactory([client]),
        per_device_queue_size=1,
        poll_interval_seconds=0.001,
    )

    first = asyncio.create_task(adapter.execute(Device("main"), "first"))
    await asyncio.to_thread(tracker.entered.wait, 0.2)
    second = asyncio.create_task(adapter.execute(Device("main"), "second"))
    await asyncio.sleep(0)

    with pytest.raises(SSHAdapterError) as raised:
        await adapter.execute(Device("main"), "third")

    assert raised.value.code == "DEVICE_QUEUE_FULL"
    await asyncio.gather(first, second)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_fingerprint_mismatch_is_rejected_without_retry() -> None:
    client = FakeClient(FakeTransport([], fingerprint="SHA256:wrong"))
    factory = ClientFactory([client])
    adapter = ParamikoSSHAdapter(client_factory=factory)

    with pytest.raises(SSHAdapterError) as raised:
        await adapter.execute(Device("main"), "command")

    assert raised.value.code == "SSH_HOST_KEY_MISMATCH"
    assert raised.value.may_have_executed is False
    assert len(factory.created) == 1
    assert client.closed
    await adapter.aclose()


@pytest.mark.asyncio
async def test_pre_dispatch_failure_reconnects_once() -> None:
    stale = FakeClient(FakeTransport(open_error=OSError("stale transport")))
    replacement_channel = FakeChannel()
    replacement = FakeClient(FakeTransport([replacement_channel]))
    factory = ClientFactory([stale, replacement])
    adapter = ParamikoSSHAdapter(client_factory=factory, poll_interval_seconds=0.001)

    result = await adapter.execute(Device("main"), "command")

    assert result.status == "succeeded"
    assert len(factory.created) == 2
    assert stale.closed
    assert replacement_channel.command == "command"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_post_dispatch_failure_is_unknown_and_is_not_retried() -> None:
    channel = FakeChannel(exec_error=OSError("connection lost"))
    client = FakeClient(FakeTransport([channel]))
    unused = FakeClient(FakeTransport([FakeChannel()]))
    factory = ClientFactory([client, unused])
    adapter = ParamikoSSHAdapter(client_factory=factory)

    result = await adapter.execute(Device("main"), "command")

    assert result.status == "unknown"
    assert result.execution_state == "unknown"
    assert result.error_code == "EXECUTION_OUTCOME_UNKNOWN"
    assert len(factory.created) == 1
    assert client.closed
    assert channel.closed
    await adapter.aclose()


@pytest.mark.asyncio
async def test_post_dispatch_timeout_is_unknown_and_is_not_retried() -> None:
    channel = FakeChannel(delay=0.1)
    client = FakeClient(FakeTransport([channel]))
    unused = FakeClient(FakeTransport([FakeChannel()]))
    factory = ClientFactory([client, unused])
    adapter = ParamikoSSHAdapter(client_factory=factory, poll_interval_seconds=0.001)

    result = await adapter.execute(Device("main", command_timeout_seconds=0.01), "command")

    assert result.status == "unknown"
    assert result.error_code == "COMMAND_TIMEOUT"
    assert len(factory.created) == 1
    assert client.closed
    await adapter.aclose()


@pytest.mark.asyncio
async def test_each_output_stream_is_bounded_and_reports_total_bytes() -> None:
    channel = FakeChannel(
        stdout=[b"1234", b"5678"],
        stderr=[b"abc", b"\xffdef"],
    )
    client = FakeClient(FakeTransport([channel]))
    adapter = ParamikoSSHAdapter(
        client_factory=ClientFactory([client]),
        poll_interval_seconds=0.001,
    )

    result = await adapter.execute(Device("main", output_limit_bytes=5), "command")

    assert result.stdout == "12345"
    assert result.stderr == "abc\ufffdd"
    assert result.stdout_total_bytes == 8
    assert result.stderr_total_bytes == 7
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_all_sessions_and_rejects_new_work() -> None:
    first_client = FakeClient(FakeTransport([FakeChannel()]))
    second_client = FakeClient(FakeTransport([FakeChannel()]))
    adapter = ParamikoSSHAdapter(
        client_factory=ClientFactory([first_client, second_client]),
        max_workers=2,
        poll_interval_seconds=0.001,
    )
    await asyncio.gather(
        adapter.execute(Device("main"), "one"),
        adapter.execute(Device("office"), "two"),
    )

    await adapter.aclose()
    await adapter.aclose()

    assert first_client.closed
    assert second_client.closed
    with pytest.raises(SSHAdapterError, match="closed"):
        await adapter.execute(Device("main"), "three")


def test_loads_rsa_ecdsa_and_ed25519_private_keys_from_memory() -> None:
    keys: list[tuple[str, type[paramiko.PKey]]] = []
    for key in (paramiko.RSAKey.generate(1024), paramiko.ECDSAKey.generate()):
        stream = io.StringIO()
        key.write_private_key(stream)
        keys.append((stream.getvalue(), type(key)))

    ed25519_key = ed25519.Ed25519PrivateKey.generate()
    ed25519_text = ed25519_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    keys.append((ed25519_text, paramiko.Ed25519Key))

    for private_key, expected_type in keys:
        assert isinstance(_load_private_key(private_key, None), expected_type)
