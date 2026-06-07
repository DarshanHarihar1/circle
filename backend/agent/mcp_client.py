from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config import settings

FOOD_MCP_URL = f"{settings.SWIGGY_MCP_BASE_URL}/food"


async def call_swiggy_tool(tool_name: str, args: dict, access_token: str):
    async with streamablehttp_client(
        FOOD_MCP_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            return result.content
