"""Load and resolve the immutable RouterOS device registry."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import SecretStr, ValidationError

from .models import (
    DEVICE_ID_PATTERN,
    AuthType,
    DeviceConfig,
    DeviceListResult,
    PasswordAuthConfig,
    ResolvedDevice,
)

DEVICES_ENV_VAR = "ROS_DEVICES_JSON"
_DEVICE_ID_RE = re.compile(DEVICE_ID_PATTERN)


class DeviceRegistryError(ValueError):
    """Raised when device configuration cannot be loaded safely."""


class DeviceNotFoundError(KeyError):
    """Raised when a caller requests an unconfigured device id."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeviceRegistryError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> None:
    raise DeviceRegistryError(f"invalid JSON numeric constant: {value}")


class DeviceRegistry:
    """Immutable collection of resolved devices loaded at process startup."""

    __slots__ = ("_devices",)

    def __init__(self, devices: Mapping[str, ResolvedDevice]) -> None:
        if not devices:
            raise DeviceRegistryError("at least one device is required")
        self._devices = MappingProxyType(dict(devices))

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> DeviceRegistry:
        """Load from an explicit environment mapping, or a snapshot of ``os.environ``."""

        environment = dict(os.environ if environ is None else environ)
        raw_devices = environment.get(DEVICES_ENV_VAR)
        if raw_devices is None:
            raise DeviceRegistryError(f"required environment variable {DEVICES_ENV_VAR} is missing")
        if not isinstance(raw_devices, str) or not raw_devices.strip():
            raise DeviceRegistryError(f"required environment variable {DEVICES_ENV_VAR} is empty")

        return cls._from_json(raw_devices, environment, source=DEVICES_ENV_VAR)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> DeviceRegistry:
        """Load a device registry file and resolve its secret references from the environment."""

        environment = dict(os.environ if environ is None else environ)
        if DEVICES_ENV_VAR in environment:
            raise DeviceRegistryError(
                f"configuration file cannot be used when {DEVICES_ENV_VAR} is also set"
            )

        config_path = Path(path)
        read_error: DeviceRegistryError | None = None
        try:
            raw_devices = config_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            read_error = DeviceRegistryError(f"configuration file {config_path!s} must be UTF-8")
        except OSError:
            read_error = DeviceRegistryError(f"cannot read configuration file {config_path!s}")

        if read_error is not None:
            raise read_error

        if not raw_devices.strip():
            raise DeviceRegistryError(f"configuration file {config_path!s} is empty")

        return cls._from_json(
            raw_devices,
            environment,
            source=f"configuration file {config_path!s}",
        )

    @classmethod
    def _from_json(
        cls,
        raw_devices: str,
        environment: Mapping[str, str],
        *,
        source: str,
    ) -> DeviceRegistry:
        """Validate a JSON device registry and resolve its secret references."""

        parse_error: DeviceRegistryError | None = None
        try:
            parsed = json.loads(
                raw_devices,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except DeviceRegistryError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError):
            parse_error = DeviceRegistryError(f"{source} must contain valid JSON")

        if parse_error is not None:
            raise parse_error

        if not isinstance(parsed, dict):
            raise DeviceRegistryError(f"{source} must be a JSON object")
        if not parsed:
            raise DeviceRegistryError("at least one device is required")

        resolved: dict[str, ResolvedDevice] = {}
        for device_id, unvalidated_config in parsed.items():
            if not isinstance(device_id, str) or _DEVICE_ID_RE.fullmatch(device_id) is None:
                raise DeviceRegistryError(
                    f"invalid device id {device_id!r}; expected {DEVICE_ID_PATTERN}"
                )
            if not isinstance(unvalidated_config, dict):
                raise DeviceRegistryError(f"device {device_id!r} must be a JSON object")

            try:
                config = DeviceConfig.model_validate(unvalidated_config)
            except ValidationError as exc:
                locations = (
                    ", ".join(
                        ".".join(str(part) for part in error["loc"])
                        for error in exc.errors(include_input=False)
                    )
                    or "configuration"
                )
            else:
                resolved[device_id] = cls._resolve_device(device_id, config, environment)
                continue

            raise DeviceRegistryError(
                f"invalid configuration for device {device_id!r} at {locations}"
            )

        return cls(resolved)

    @staticmethod
    def _resolve_secret(
        environment: Mapping[str, str],
        variable_name: str,
        *,
        device_id: str,
        secret_kind: str,
    ) -> SecretStr:
        if variable_name not in environment:
            raise DeviceRegistryError(
                f"device {device_id!r} references a missing {secret_kind} environment variable"
            )
        value = environment[variable_name]
        if not isinstance(value, str) or not value.strip():
            raise DeviceRegistryError(f"device {device_id!r} has an empty {secret_kind}")
        return SecretStr(value)

    @classmethod
    def _resolve_device(
        cls,
        device_id: str,
        config: DeviceConfig,
        environment: Mapping[str, str],
    ) -> ResolvedDevice:
        password: SecretStr | None = None
        private_key: SecretStr | None = None
        passphrase: SecretStr | None = None

        if isinstance(config.auth, PasswordAuthConfig):
            auth_type = AuthType.PASSWORD
            if config.auth.password is not None:
                password = config.auth.password
            else:
                assert config.auth.password_env is not None
                password = cls._resolve_secret(
                    environment,
                    config.auth.password_env,
                    device_id=device_id,
                    secret_kind="password",
                )
        else:
            auth_type = AuthType.PRIVATE_KEY
            private_key = cls._resolve_secret(
                environment,
                config.auth.private_key_env,
                device_id=device_id,
                secret_kind="private key",
            )
            private_key_lines = private_key.get_secret_value().strip().splitlines()
            first_line = private_key_lines[0]
            if not (
                first_line.startswith("-----BEGIN ") and first_line.endswith("PRIVATE KEY-----")
            ):
                raise DeviceRegistryError(
                    f"device {device_id!r} private key must contain complete PEM/OpenSSH key text"
                )
            expected_end = first_line.replace("-----BEGIN ", "-----END ", 1)
            if private_key_lines[-1] != expected_end:
                raise DeviceRegistryError(
                    f"device {device_id!r} private key must contain complete PEM/OpenSSH key text"
                )
            if config.auth.passphrase_env is not None:
                passphrase = cls._resolve_secret(
                    environment,
                    config.auth.passphrase_env,
                    device_id=device_id,
                    secret_kind="private-key passphrase",
                )

        return ResolvedDevice(
            device=device_id,
            display_name=config.display_name or device_id,
            description=config.description,
            host=config.host,
            port=config.port,
            username=config.username,
            auth_type=auth_type,
            enabled=config.enabled,
            tags=config.tags,
            connect_timeout_seconds=config.connect_timeout_seconds,
            command_timeout_seconds=config.command_timeout_seconds,
            keepalive_seconds=config.keepalive_seconds,
            idle_ttl_seconds=config.idle_ttl_seconds,
            output_limit_bytes=config.output_limit_bytes,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
            fingerprint_sha256=config.host_key.fingerprint_sha256,
        )

    def get(self, device_id: str) -> ResolvedDevice:
        try:
            return self._devices[device_id]
        except KeyError as exc:
            raise DeviceNotFoundError(device_id) from exc

    def list_devices(self) -> DeviceListResult:
        return DeviceListResult(
            devices=tuple(device.to_summary() for device in self._devices.values())
        )

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(self._devices)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def __len__(self) -> int:
        return len(self._devices)
