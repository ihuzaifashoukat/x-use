"""Queue-side MCP tools. Placeholder: real tools land with the queue task."""
import logging
from typing import Any, Dict

from .executor import Ctx, ToolError

logger = logging.getLogger(__name__)


def build_executor(ctx: Ctx):
    """Bridge the queue runner's ExecutorFn to the MCP browser executors."""

    async def _execute(item) -> Dict[str, Any]:
        raise ToolError(f"Queue execution is not available yet ('{item.action}').")

    return _execute


def register_queue_tools(server, ctx: Ctx) -> None:
    """No-op placeholder; replaced by the queue tools task."""
