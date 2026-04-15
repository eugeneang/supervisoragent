import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from commands.ping import ping_command


@pytest.mark.asyncio
async def test_ping_replies_pong():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await ping_command(update, context)

    update.message.reply_text.assert_called_once_with("pong")
