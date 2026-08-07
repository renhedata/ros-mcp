# ROS-MCP

[![CI](https://github.com/asharca/ros-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/asharca/ros-mcp/actions/workflows/ci.yml)
[![Container](https://github.com/asharca/ros-mcp/actions/workflows/container.yml/badge.svg)](https://github.com/asharca/ros-mcp/actions/workflows/container.yml)

ROS-MCP is a small MCP gateway for managing multiple MikroTik RouterOS devices
over SSH. It exposes exactly two tools and keeps device credentials, SSH session
reuse, retries, timeouts, and output limits behind the gateway.

Despite the project name, `ROS` means MikroTik RouterOS, not Robot Operating
System.

## MCP tools

### `device_list`

Returns the configured, non-sensitive device inventory. It performs no network
I/O and never returns passwords, private keys, environment-variable names, or
host-key fingerprints.

### `command_execute`

```text
command_execute(device: string, command: string, dry_run: boolean = true)
```

- `device` is always required. There is no implicit default-device fallback.
- `command` is one RouterOS CLI line, limited to 8 KiB.
- `dry_run=true` validates locally and does not open an SSH connection.
- A result with `status=unknown` means the command may have executed and must
  not be retried automatically.

The gateway does not implement RouterOS Safe Mode and does not claim that a dry
run validates RouterOS syntax or effects.

## Configuration

Choose exactly one device-registry source:

- `ROS_DEVICES_JSON`, containing the JSON object directly; or
- `ros-mcp --config PATH`, where `PATH` is a UTF-8 JSON file.

The two sources use the same schema. Do not set `ROS_DEVICES_JSON` when using
`--config`; the server rejects ambiguous configuration sources. Each device
references its own credential environment variable.

```json
{
  "main": {
    "display_name": "Main Router",
    "description": "Internet edge",
    "host": "192.168.88.1",
    "port": 22,
    "username": "ai-mgmt",
    "enabled": true,
    "tags": ["production", "edge"],
    "connect_timeout_seconds": 15,
    "command_timeout_seconds": 25,
    "keepalive_seconds": 30,
    "idle_ttl_seconds": 300,
    "output_limit_bytes": 1048576,
    "auth": {
      "type": "password",
      "password_env": "ROS_MAIN_PASSWORD"
    },
    "host_key": {
      "fingerprint_sha256": "SHA256:REPLACE_WITH_43_BASE64_CHARACTERS"
    }
  },
  "office": {
    "display_name": "Office Router",
    "host": "10.0.0.1",
    "port": 22,
    "username": "ai-mgmt",
    "auth": {
      "type": "private_key",
      "private_key_env": "ROS_OFFICE_PRIVATE_KEY",
      "passphrase_env": "ROS_OFFICE_KEY_PASSPHRASE"
    },
    "host_key": {
      "fingerprint_sha256": "SHA256:REPLACE_WITH_43_BASE64_CHARACTERS"
    }
  }
}
```

`devices.example.json` contains the same sanitized configuration as a starting
point. Keep actual device inventories outside version control.

Set the referenced secrets separately, using the MCP client's secret store or
the process environment:

```text
ROS_MAIN_PASSWORD=<main RouterOS SSH password>
ROS_OFFICE_PRIVATE_KEY=<complete PEM/OpenSSH private-key text>
ROS_OFFICE_KEY_PASSPHRASE=<optional key passphrase>
```

Device IDs must match `^[a-z][a-z0-9_-]{0,63}$`. Configuration is validated and
resolved once at startup, so restart the server after changing a configuration
file. Missing secrets, unknown fields, duplicate JSON keys, invalid IDs, and
malformed SHA-256 fingerprints prevent startup.

The host-key fingerprint is mandatory. Unknown keys are never accepted through
TOFU or Paramiko's `AutoAddPolicy`.

## Run locally

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required for local
development.

```bash
uv sync --all-groups
export ROS_DEVICES_JSON='{"main":{"host":"192.168.88.1","username":"ai-mgmt","auth":{"type":"password","password_env":"ROS_MAIN_PASSWORD"},"host_key":{"fingerprint_sha256":"SHA256:REPLACE_WITH_43_BASE64_CHARACTERS"}}}'
export ROS_MAIN_PASSWORD='replace-me'
uv run ros-mcp
```

To deploy from a file instead, put the JSON object above in a UTF-8 file and
pass its path. The file does not contain passwords, private keys, or
passphrases; it only names their environment variables.

```bash
export ROS_MAIN_PASSWORD='replace-me'
uv run ros-mcp --config /absolute/path/to/devices.json
```

The process speaks MCP over stdio. Application code does not write ordinary
messages to stdout because stdout belongs to the protocol.

## Docker

Published images are available from GHCR:

```bash
docker pull ghcr.io/asharca/ros-mcp:latest
```

`main` publishes `latest`, `main`, and `sha-<commit>` tags. Git tags such as
`v1.2.3` additionally publish `1.2.3` and `1.2`.

To build locally:

```bash
docker build -t ros-mcp:local .
docker run --rm -i \
  --read-only \
  --tmpfs /tmp:rw,size=16m \
  -e ROS_DEVICES_JSON \
  -e ROS_MAIN_PASSWORD \
  ros-mcp:local
```

The image runs as a non-root user, exposes no port, and starts the stdio MCP
server directly. Its root filesystem is safe to run read-only.

### Docker with a configuration file

Mount the configuration file read-only and pass the container path to
`--config` after the image name. The secret variable is forwarded from the
Docker client process; add every variable referenced by the file.

```bash
docker run --rm -i \
  --read-only \
  --tmpfs /tmp:rw,size=16m \
  --mount type=bind,src="/absolute/path/to/devices.json",dst=/config/devices.json,readonly \
  -e ROS_MAIN_PASSWORD \
  ghcr.io/asharca/ros-mcp:latest \
  --config /config/devices.json
```

Use an absolute source path that is visible to the Docker daemon. With Docker
Desktop, share the source directory; with a remote daemon, use a file or secret
mount managed on that daemon rather than a path from the MCP client's machine.
The mounted file must be readable by the image's UID/GID `10001`. Do not use
`-t` or `-d`: the MCP transport needs attached stdin and stdout. Do not disable
networking because the gateway needs outbound SSH access to RouterOS devices.

## Deploy as an MCP server

ROS-MCP uses the stdio transport. Configure your MCP client to start one process
per server instance and keep its stdin and stdout connected to the client. Client
configuration formats differ, but clients that use an `mcpServers` object can
use the following Docker command shape. Replace the placeholder secret with the
client's secret-management mechanism rather than committing it to a config file.

```json
{
  "mcpServers": {
    "ros-mcp": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=16m",
        "--mount",
        "type=bind,src=/absolute/path/to/devices.json,dst=/config/devices.json,readonly",
        "-e",
        "ROS_MAIN_PASSWORD",
        "ghcr.io/asharca/ros-mcp:latest",
        "--config",
        "/config/devices.json"
      ],
      "env": {
        "ROS_MAIN_PASSWORD": "<secret from your secret store>"
      }
    }
  }
}
```

The `env` entry supplies the secret to the Docker client, and `-e
ROS_MAIN_PASSWORD` forwards it into the container. Add an `env` entry and `-e`
argument for every credential named by the device file. Verify host-key
fingerprints through a trusted out-of-band channel, use a least-privileged
RouterOS account, and keep manual approval enabled for `command_execute` in the
MCP client.

## ToolPlane

1. Publish the image to a registry reachable by ToolPlane's Docker daemon.
2. In a workspace, choose `MCP > Add custom MCP > Docker`.
3. For environment configuration, enter the image reference and leave Start
   Command empty. Add `ROS_DEVICES_JSON` and all referenced secret variables on
   the deployment's Variables page, then restart it.
4. For file configuration, use ToolPlane's file or secret-mount facility to
   place the file at (for example) `/config/devices.json`, then set Start Command
   to `--config /config/devices.json`. Add the referenced secret variables as
   deployment variables and restart it.
5. Leave `Disconnect from network` disabled so the container can reach RouterOS
   SSH addresses.

ToolPlane currently has a 30-second call timeout. Configure
`command_timeout_seconds` to about 25 seconds there, or raise ToolPlane's timeout
above the gateway's command timeout.

## Execution behavior

- One reusable SSH session is maintained per device.
- Commands for the same device are serialized in a bounded FIFO.
- Different devices can execute concurrently.
- A stale session may reconnect once before command dispatch.
- A command is never replayed after dispatch.
- stdout and stderr are drained concurrently and retained up to the configured
  per-stream byte limit.
- All SSH clients are explicitly closed during MCP server shutdown.

## Logging

ROS-MCP has no audit-log Module, log-query tool, command history, or persistent
request/output storage. Expected operational failures are returned as structured
tool results. The MCP runtime is configured to emit only error-level diagnostics
to stderr.

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```
