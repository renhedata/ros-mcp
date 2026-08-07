"""Domain models for RouterOS devices and command execution results."""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

DEVICE_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

DeviceId = Annotated[
    str,
    StringConstraints(strict=True, pattern=DEVICE_ID_PATTERN),
]
EnvironmentVariableName = Annotated[
    str,
    StringConstraints(strict=True, pattern=ENV_NAME_PATTERN),
]
NonEmptyString = Annotated[str, StringConstraints(strict=True, min_length=1)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
PositiveSeconds = Annotated[int, Field(strict=True, gt=0)]
NonNegativeSeconds = Annotated[int, Field(strict=True, ge=0)]
PositiveByteCount = Annotated[int, Field(strict=True, gt=0)]
NonNegativeMilliseconds = Annotated[int, Field(strict=True, ge=0)]


class StrictModel(BaseModel):
    """Base for immutable models that reject unrecognised fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class AuthType(StrEnum):
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"


class PasswordAuthConfig(StrictModel):
    type: Literal["password"]
    password: SecretStr | None = Field(default=None, exclude=True, repr=False)
    password_env: EnvironmentVariableName | None = Field(default=None, repr=False)

    @field_validator("password")
    @classmethod
    def reject_empty_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("password must be non-blank")
        return value

    @model_validator(mode="after")
    def validate_password_source(self) -> Self:
        if (self.password is None) == (self.password_env is None):
            raise ValueError(
                "password authentication requires exactly one of password or password_env"
            )
        return self


class PrivateKeyAuthConfig(StrictModel):
    type: Literal["private_key"]
    private_key_env: EnvironmentVariableName = Field(repr=False)
    passphrase_env: EnvironmentVariableName | None = Field(default=None, repr=False)


AuthConfig = Annotated[
    PasswordAuthConfig | PrivateKeyAuthConfig,
    Field(discriminator="type"),
]


class HostKeyConfig(StrictModel):
    fingerprint_sha256: NonEmptyString = Field(repr=False)

    @field_validator("fingerprint_sha256")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        prefix = "SHA256:"
        if not value.startswith(prefix):
            raise ValueError("fingerprint must start with 'SHA256:'")

        encoded = value[len(prefix) :]
        if not encoded or "=" in encoded:
            raise ValueError("fingerprint must be unpadded base64")
        base64_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        if any(character not in base64_alphabet for character in encoded):
            raise ValueError("fingerprint must use standard base64")

        try:
            decoded = base64.b64decode(
                encoded + ("=" * (-len(encoded) % 4)),
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError("fingerprint must contain valid base64") from exc

        canonical = base64.b64encode(decoded).decode("ascii").rstrip("=")
        if len(decoded) != 32 or canonical != encoded:
            raise ValueError("fingerprint must encode a 32-byte SHA-256 digest")
        return value


class DeviceConfig(StrictModel):
    """Validated device configuration before secret references are resolved."""

    display_name: NonEmptyString | None = None
    description: str | None = None
    host: NonEmptyString
    port: Port = 22
    username: NonEmptyString
    enabled: StrictBool = True
    tags: tuple[NonEmptyString, ...] = ()
    connect_timeout_seconds: PositiveSeconds = 15
    command_timeout_seconds: PositiveSeconds = 60
    keepalive_seconds: NonNegativeSeconds = 30
    idle_ttl_seconds: NonNegativeSeconds = 300
    output_limit_bytes: PositiveByteCount = 1_048_576
    auth: AuthConfig
    host_key: HostKeyConfig

    @field_validator("display_name", "host", "username")
    @classmethod
    def reject_blank_or_padded_values(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("value must be non-blank and have no surrounding whitespace")
        return value

    @field_validator("description")
    @classmethod
    def reject_padded_description(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("description must have no surrounding whitespace")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not tag.strip() or tag != tag.strip() for tag in value):
            raise ValueError("tags must be non-blank and have no surrounding whitespace")
        if len(set(value)) != len(value):
            raise ValueError("tags must be unique")
        return value


class DeviceSummary(StrictModel):
    """Public, non-sensitive device data returned by ``device_list``."""

    device: DeviceId
    display_name: NonEmptyString
    description: str | None = None
    host: NonEmptyString
    port: Port
    username: NonEmptyString
    auth_type: AuthType
    enabled: StrictBool
    tags: tuple[str, ...] = ()
    connect_timeout_seconds: PositiveSeconds
    command_timeout_seconds: PositiveSeconds
    keepalive_seconds: NonNegativeSeconds
    idle_ttl_seconds: NonNegativeSeconds
    output_limit_bytes: PositiveByteCount


class DeviceListResult(StrictModel):
    devices: tuple[DeviceSummary, ...]


class ResolvedDevice(StrictModel):
    """Internal device model with environment references resolved in memory."""

    device: DeviceId
    display_name: NonEmptyString
    description: str | None = None
    host: NonEmptyString
    port: Port
    username: NonEmptyString
    auth_type: AuthType
    enabled: StrictBool
    tags: tuple[str, ...] = ()
    connect_timeout_seconds: PositiveSeconds
    command_timeout_seconds: PositiveSeconds
    keepalive_seconds: NonNegativeSeconds
    idle_ttl_seconds: NonNegativeSeconds
    output_limit_bytes: PositiveByteCount
    password: SecretStr | None = Field(default=None, exclude=True, repr=False)
    private_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    passphrase: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    fingerprint_sha256: str = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_auth_material(self) -> Self:
        if self.auth_type is AuthType.PASSWORD:
            if self.password is None:
                raise ValueError("password authentication requires a password")
            if self.private_key is not None or self.passphrase is not None:
                raise ValueError("password authentication cannot include private-key data")
        else:
            if self.private_key is None:
                raise ValueError("private-key authentication requires a private key")
            if self.password is not None:
                raise ValueError("private-key authentication cannot include a password")
        return self

    def to_summary(self) -> DeviceSummary:
        return DeviceSummary(
            device=self.device,
            display_name=self.display_name,
            description=self.description,
            host=self.host,
            port=self.port,
            username=self.username,
            auth_type=self.auth_type,
            enabled=self.enabled,
            tags=self.tags,
            connect_timeout_seconds=self.connect_timeout_seconds,
            command_timeout_seconds=self.command_timeout_seconds,
            keepalive_seconds=self.keepalive_seconds,
            idle_ttl_seconds=self.idle_ttl_seconds,
            output_limit_bytes=self.output_limit_bytes,
        )


class CommandStatus(StrEnum):
    DRY_RUN = "dry_run"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ExecutionState(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class CommandError(StrictModel):
    code: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]*$")]
    message: NonEmptyString
    retryable: StrictBool = False
    may_have_executed: StrictBool = False


class CommandResult(StrictModel):
    # Echo the caller's target even when it is not a valid configured DeviceId.
    device: Annotated[str, StringConstraints(strict=True)]
    status: CommandStatus
    execution_state: ExecutionState
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: StrictBool = False
    stderr_truncated: StrictBool = False
    duration_ms: NonNegativeMilliseconds
    error: CommandError | None = None

    @model_validator(mode="after")
    def validate_state_combination(self) -> Self:
        if self.execution_state is not ExecutionState.COMPLETED and self.exit_code is not None:
            raise ValueError("exit_code is only valid for completed execution")

        if self.status is CommandStatus.DRY_RUN:
            if self.execution_state is not ExecutionState.NOT_STARTED:
                raise ValueError("dry_run must have execution_state='not_started'")
            if self.exit_code is not None or self.error is not None:
                raise ValueError("dry_run cannot include an exit code or error")
        elif self.status is CommandStatus.SUCCEEDED:
            if self.execution_state is not ExecutionState.COMPLETED or self.exit_code != 0:
                raise ValueError("succeeded must be completed with exit_code=0")
            if self.error is not None:
                raise ValueError("succeeded cannot include an error")
        elif self.status is CommandStatus.FAILED:
            if self.execution_state is ExecutionState.UNKNOWN:
                raise ValueError("failed cannot have an unknown execution state")
            if self.execution_state is ExecutionState.NOT_STARTED:
                if self.exit_code is not None or self.error is None:
                    raise ValueError("a pre-dispatch failure requires an error and no exit code")
            elif self.exit_code is None or self.exit_code == 0:
                raise ValueError("a completed failure requires a non-zero exit code")
        else:
            if self.execution_state is not ExecutionState.UNKNOWN:
                raise ValueError("unknown status requires execution_state='unknown'")
            if self.exit_code is not None or self.error is None:
                raise ValueError("unknown execution requires an error and no exit code")
            if not self.error.may_have_executed:
                raise ValueError("unknown execution must set may_have_executed=true")
        return self
