from __future__ import annotations

import json
from pathlib import Path

import pytest

from ros_mcp.device_registry import DeviceNotFoundError, DeviceRegistry, DeviceRegistryError
from ros_mcp.models import AuthType, DeviceConfig

FINGERPRINT_A = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FINGERPRINT_B = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA"


def password_device(password_env: str, **overrides: object) -> dict[str, object]:
    device: dict[str, object] = {
        "host": "192.0.2.1",
        "username": "ai-mgmt",
        "auth": {"type": "password", "password_env": password_env},
        "host_key": {"fingerprint_sha256": FINGERPRINT_A},
    }
    device.update(overrides)
    return device


def inline_password_device(password: str, **overrides: object) -> dict[str, object]:
    device: dict[str, object] = {
        "host": "192.0.2.1",
        "username": "ai-mgmt",
        "auth": {"type": "password", "password": password},
        "host_key": {"fingerprint_sha256": FINGERPRINT_A},
    }
    device.update(overrides)
    return device


def environment(devices: dict[str, object], **secrets: str) -> dict[str, str]:
    return {"ROS_DEVICES_JSON": json.dumps(devices), **secrets}


def write_config_file(tmp_path: Path, devices: dict[str, object]) -> Path:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(devices), encoding="utf-8")
    return path


def test_loads_multiple_devices_with_independent_passwords() -> None:
    registry = DeviceRegistry.from_env(
        environment(
            {
                "main": password_device(
                    "MAIN_PASSWORD",
                    display_name="Main Router",
                    tags=["production", "edge"],
                ),
                "office": password_device(
                    "OFFICE_PASSWORD",
                    host="198.51.100.8",
                    host_key={"fingerprint_sha256": FINGERPRINT_B},
                ),
            },
            MAIN_PASSWORD="main-secret",
            OFFICE_PASSWORD="office-secret",
        )
    )

    assert registry.device_ids == ("main", "office")
    assert registry.get("main").password is not None
    assert registry.get("office").password is not None
    assert registry.get("main").password.get_secret_value() == "main-secret"
    assert registry.get("office").password.get_secret_value() == "office-secret"
    assert registry.get("main").display_name == "Main Router"
    assert registry.get("office").display_name == "office"


def test_loads_devices_from_a_configuration_file(tmp_path: Path) -> None:
    config_path = write_config_file(
        tmp_path,
        {
            "main": password_device("MAIN_PASSWORD", display_name="Main Router"),
            "office": password_device(
                "OFFICE_PASSWORD",
                host="198.51.100.8",
                host_key={"fingerprint_sha256": FINGERPRINT_B},
            ),
        },
    )

    registry = DeviceRegistry.from_file(
        config_path,
        {
            "MAIN_PASSWORD": "main-secret",
            "OFFICE_PASSWORD": "office-secret",
        },
    )

    assert registry.device_ids == ("main", "office")
    assert registry.get("main").password is not None
    assert registry.get("main").password.get_secret_value() == "main-secret"
    assert registry.get("office").password is not None
    assert registry.get("office").password.get_secret_value() == "office-secret"


def test_loads_an_inline_password_from_a_configuration_file(tmp_path: Path) -> None:
    secret = "  inline secret!@#  "
    config_path = write_config_file(
        tmp_path,
        {"main": inline_password_device(secret)},
    )

    registry = DeviceRegistry.from_file(config_path, {})

    assert registry.get("main").password is not None
    assert registry.get("main").password.get_secret_value() == secret


def test_loads_an_inline_password_from_json_environment() -> None:
    registry = DeviceRegistry.from_env(
        environment({"main": inline_password_device("inline-secret")})
    )

    assert registry.get("main").password is not None
    assert registry.get("main").password.get_secret_value() == "inline-secret"


def test_resolves_complete_private_key_and_optional_passphrase() -> None:
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nkey-data\n-----END OPENSSH PRIVATE KEY-----"
    registry = DeviceRegistry.from_env(
        environment(
            {
                "main": {
                    "host": "192.0.2.1",
                    "username": "ai-mgmt",
                    "auth": {
                        "type": "private_key",
                        "private_key_env": "MAIN_PRIVATE_KEY",
                        "passphrase_env": "MAIN_KEY_PASSPHRASE",
                    },
                    "host_key": {"fingerprint_sha256": FINGERPRINT_A},
                }
            },
            MAIN_PRIVATE_KEY=private_key,
            MAIN_KEY_PASSPHRASE="key-secret",
        )
    )

    resolved = registry.get("main")
    assert resolved.auth_type is AuthType.PRIVATE_KEY
    assert resolved.private_key is not None
    assert resolved.private_key.get_secret_value() == private_key
    assert resolved.passphrase is not None
    assert resolved.passphrase.get_secret_value() == "key-secret"


def test_public_summary_and_resolved_dump_do_not_leak_sensitive_data() -> None:
    fingerprint = FINGERPRINT_A
    registry = DeviceRegistry.from_env(
        environment(
            {"main": password_device("MAIN_PASSWORD")},
            MAIN_PASSWORD="highly-sensitive-password",
        )
    )

    summary_json = registry.list_devices().model_dump_json()
    resolved_json = registry.get("main").model_dump_json()
    combined = summary_json + resolved_json

    assert "highly-sensitive-password" not in combined
    assert "MAIN_PASSWORD" not in combined
    assert fingerprint not in combined
    assert "password" in summary_json
    assert "192.0.2.1" in summary_json


def test_inline_password_does_not_leak_from_registry_outputs(tmp_path: Path) -> None:
    secret = "highly-sensitive-inline-password"
    device = inline_password_device(secret)
    config_json = DeviceConfig.model_validate(device).model_dump_json()
    registry = DeviceRegistry.from_file(
        write_config_file(tmp_path, {"main": device}),
        {},
    )

    combined = registry.list_devices().model_dump_json() + registry.get("main").model_dump_json()

    assert secret not in config_json
    assert secret not in combined
    assert FINGERPRINT_A not in combined


@pytest.mark.parametrize(
    ("devices", "secrets"),
    [
        ({}, {}),
        ({"Main": password_device("PASS")}, {"PASS": "secret"}),
        ({"1main": password_device("PASS")}, {"PASS": "secret"}),
        (
            {"main": password_device("PASS", unexpected=True)},
            {"PASS": "secret"},
        ),
        (
            {
                "main": password_device(
                    "PASS",
                    auth={
                        "type": "password",
                        "password_env": "PASS",
                        "unexpected": True,
                    },
                )
            },
            {"PASS": "secret"},
        ),
        (
            {
                "main": password_device(
                    "PASS",
                    host_key={
                        "fingerprint_sha256": FINGERPRINT_A,
                        "unexpected": True,
                    },
                )
            },
            {"PASS": "secret"},
        ),
        (
            {
                "main": password_device(
                    "PASS",
                    host_key={"fingerprint_sha256": "SHA256:not-a-digest"},
                )
            },
            {"PASS": "secret"},
        ),
    ],
)
def test_rejects_invalid_or_unknown_configuration(
    devices: dict[str, object],
    secrets: dict[str, str],
) -> None:
    with pytest.raises(DeviceRegistryError):
        DeviceRegistry.from_env(environment(devices, **secrets))


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"ROS_DEVICES_JSON": ""},
        {"ROS_DEVICES_JSON": "[]"},
        {"ROS_DEVICES_JSON": "not json"},
    ],
)
def test_rejects_missing_empty_or_non_object_registry(environ: dict[str, str]) -> None:
    with pytest.raises(DeviceRegistryError):
        DeviceRegistry.from_env(environ)


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "password"},
        {"type": "password", "password": "   "},
        {"type": "password", "password": "inline-secret", "password_env": "PASS"},
        {"type": "unsupported", "password": "inline-secret"},
    ],
)
def test_rejects_invalid_inline_password_configuration(auth: dict[str, str]) -> None:
    device = inline_password_device("placeholder", auth=auth)

    with pytest.raises(DeviceRegistryError) as raised:
        DeviceRegistry.from_env(environment({"main": device}))

    assert "inline-secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_json_with_an_inline_password_does_not_retain_its_contents() -> None:
    secret = "super-secret-in-invalid-json"
    raw = f'{{"main":{{"auth":{{"type":"password","password":"{secret}"}}}}}} trailing'

    with pytest.raises(DeviceRegistryError) as raised:
        DeviceRegistry.from_env({"ROS_DEVICES_JSON": raw})

    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_utf8_configuration_file_does_not_retain_its_contents(tmp_path: Path) -> None:
    secret = b"super-secret-in-invalid-utf8"
    path = tmp_path / "invalid-utf8.json"
    path.write_bytes(b'{"password":"' + secret + b'"}\xff')

    with pytest.raises(DeviceRegistryError) as raised:
        DeviceRegistry.from_file(path, {})

    assert secret.decode() not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_rejects_unreadable_invalid_or_empty_configuration_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not json", encoding="utf-8")
    empty_path = tmp_path / "empty.json"
    empty_path.write_text("", encoding="utf-8")
    non_utf8_path = tmp_path / "non-utf8.json"
    non_utf8_path.write_bytes(b"\xff")

    for path in (missing_path, invalid_path, empty_path, non_utf8_path):
        with pytest.raises(DeviceRegistryError):
            DeviceRegistry.from_file(path)


def test_rejects_both_configuration_sources(tmp_path: Path) -> None:
    config_path = write_config_file(tmp_path, {"main": password_device("PASS")})

    with pytest.raises(DeviceRegistryError, match="ROS_DEVICES_JSON"):
        DeviceRegistry.from_file(
            config_path,
            environment({"main": password_device("PASS")}, PASS="secret"),
        )


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_rejects_missing_or_empty_secret(secret: str | None) -> None:
    environ = environment({"main": password_device("PASS")})
    if secret is not None:
        environ["PASS"] = secret

    with pytest.raises(DeviceRegistryError):
        DeviceRegistry.from_env(environ)


def test_rejects_duplicate_device_key_before_json_value_is_lost(tmp_path: Path) -> None:
    raw = (
        '{"main":{"host":"192.0.2.1","username":"u",'
        '"auth":{"type":"password","password_env":"PASS"},'
        f'"host_key":{{"fingerprint_sha256":"{FINGERPRINT_A}"}}}},'
        '"main":{"host":"192.0.2.2","username":"u",'
        '"auth":{"type":"password","password_env":"PASS"},'
        f'"host_key":{{"fingerprint_sha256":"{FINGERPRINT_A}"}}}}}}'
    )

    with pytest.raises(DeviceRegistryError, match="duplicate JSON key"):
        DeviceRegistry.from_env({"ROS_DEVICES_JSON": raw, "PASS": "secret"})

    config_path = tmp_path / "duplicate-keys.json"
    config_path.write_text(raw, encoding="utf-8")
    with pytest.raises(DeviceRegistryError, match="duplicate JSON key"):
        DeviceRegistry.from_file(config_path, {"PASS": "secret"})


def test_unknown_device_has_a_specific_exception() -> None:
    registry = DeviceRegistry.from_env(
        environment(
            {"main": password_device("PASS")},
            PASS="secret",
        )
    )

    with pytest.raises(DeviceNotFoundError):
        registry.get("office")
