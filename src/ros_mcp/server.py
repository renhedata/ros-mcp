"""MCP stdio entry point for the RouterOS gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import cast

from mcp.server.fastmcp import Context, FastMCP

from ros_mcp.gateway import Gateway, create_gateway_from_env
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


def main() -> None:
    """Run the gateway using MCP's stdio transport."""
    create_server().run(transport="stdio")
