"""MCP stdio entry point for the RouterOS gateway."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import cast

from mcp.server.fastmcp import Context, FastMCP

from ros_mcp.gateway import Gateway, create_gateway_from_env, create_gateway_from_file
from ros_mcp.models import CommandResult, DeviceListResult

GatewayFactory = Callable[[], Gateway]


def create_server(
    gateway_factory: GatewayFactory = create_gateway_from_env,
) -> FastMCP[Gateway]:
    """Create the MCP server without opening stdio or connecting to a device."""

    @asynccontextmanager
    async def lifespan(_: FastMCP[Gateway]) -> AsyncIterator[Gateway]:
        gateway = gateway_factory()
        try:
            yield gateway
        finally:
            await gateway.aclose()

    server = FastMCP[Gateway](
        "ros-mcp",
        instructions="Manage configured MikroTik RouterOS devices over SSH.",
        lifespan=lifespan,
        log_level="ERROR",
    )

    @server.tool(structured_output=True)
    def device_list(*, ctx: Context) -> DeviceListResult:
        """List configured RouterOS devices without connecting to them or exposing credentials."""
        gateway = cast(Gateway, ctx.request_context.lifespan_context)
        return gateway.device_list()

    @server.tool(structured_output=True)
    async def command_execute(
        device: str,
        command: str,
        dry_run: bool = True,
        *,
        ctx: Context,
    ) -> CommandResult:
        """Execute a command on one device.

        Commands may be non-idempotent. When the result reports an unknown outcome,
        the command may already have run and must not be retried automatically.
        """
        gateway = cast(Gateway, ctx.request_context.lifespan_context)
        return await gateway.command_execute(
            device=device,
            command=command,
            dry_run=dry_run,
        )

    return server


def main(argv: Sequence[str] | None = None) -> None:
    """Run the gateway using MCP's stdio transport."""

    parser = argparse.ArgumentParser(description="Run the RouterOS MCP gateway over stdio.")
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="read the device registry JSON from PATH instead of ROS_DEVICES_JSON",
    )
    arguments = parser.parse_args(argv)

    gateway_factory: GatewayFactory = create_gateway_from_env
    if arguments.config is not None:
        gateway_factory = partial(create_gateway_from_file, arguments.config)

    create_server(gateway_factory).run(transport="stdio")
