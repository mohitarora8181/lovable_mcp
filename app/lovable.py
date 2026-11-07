from contextlib import AsyncExitStack
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .oauth import LovableOAuth, LovableOAuthError


class LovableMcpError(Exception):
    pass


class LovableMcpClient:
    """Minimal adapter around Lovable's streamable-HTTP MCP endpoint."""

    def __init__(self, endpoint: str, oauth: LovableOAuth) -> None:
        self.endpoint = endpoint
        self.oauth = oauth

    async def send_message(self, project_id: str, message: str) -> str:
        return await self._call_project_tool(
            "send_message",
            {"project_id": project_id, "message": message},
            wait_for_message=True,
        )

    async def deploy_project(self, project_id: str) -> str:
        """Publish the shared Lovable project after a successful change."""
        return await self._call_project_tool("deploy_project", {"project_id": project_id})

    async def _call_project_tool(
        self,
        tool_name: str,
        arguments: dict[str, str],
        *,
        wait_for_message: bool = False,
    ) -> str:
        try:
            access_token = await self.oauth.access_token()
        except LovableOAuthError as exc:
            raise LovableMcpError(str(exc)) from exc

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(
                    streamablehttp_client(self.endpoint, headers=headers)
                )
                read_stream, write_stream, _ = streams
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                tool_result = await session.call_tool(tool_name, arguments)
                if tool_result.isError:
                    raise LovableMcpError(
                        _tool_text(tool_result) or f"Lovable rejected {tool_name}."
                    )
                if not wait_for_message:
                    return _tool_text(tool_result) or "Lovable accepted the request."
                message_id = _message_id(tool_result)
                if not message_id:
                    raise LovableMcpError(
                        "Lovable did not return a message ID after accepting the message."
                    )
                result = await session.call_tool(
                    "get_message",
                    {"project_id": arguments["project_id"], "message_id": message_id},
                )
        except Exception as exc:
            raise LovableMcpError(f"Lovable MCP request failed: {exc}") from exc

        if result.isError:
            raise LovableMcpError(_tool_text(result) or "Lovable could not retrieve the response.")
        return _tool_text(result) or "Lovable returned an empty response."


def _content_to_text(content: list[object]) -> str:
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _message_id(result: object) -> str | None:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        for key in ("message_id", "messageId", "id"):
            value = structured.get(key)
            if isinstance(value, str) and value:
                return value

    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            for key in ("message_id", "messageId", "id"):
                value = decoded.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _tool_text(result: object) -> str:
    text = _content_to_text(getattr(result, "content", []))
    if text:
        return text
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""
