"""
Тесты: блокировка пользователей (use_cases/blocked_users.py).

Запуск: pytest tests/test_blocked_users.py -v
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════
# 1. is_blocked
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_is_blocked_true():
    """is_blocked возвращает True если пользователь в кеше."""
    import use_cases.blocked_users as mod

    # Сбрасываем кеш
    mod._invalidate_cache()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [(111,), (222,)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.is_blocked(111)

    assert result is True


@pytest.mark.asyncio
async def test_is_blocked_false():
    """is_blocked возвращает False если пользователя нет в кеше."""
    import use_cases.blocked_users as mod

    mod._invalidate_cache()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [(111,)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.is_blocked(999)

    assert result is False


# ═══════════════════════════════════════════════════════
# 2. block_user
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_block_user_new():
    """block_user создаёт новую запись и возвращает True."""
    import use_cases.blocked_users as mod

    mod._invalidate_cache()

    mock_session = AsyncMock()
    # execute для проверки existing → None
    mock_existing_result = MagicMock()
    mock_existing_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_existing_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.block_user(555, user_name="Test User", blocked_by=1)

    assert result is True
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_block_user_already_blocked():
    """block_user возвращает False если пользователь уже заблокирован."""
    import use_cases.blocked_users as mod

    mod._invalidate_cache()

    mock_session = AsyncMock()
    mock_existing_result = MagicMock()
    mock_existing_result.scalar_one_or_none.return_value = MagicMock()  # exists
    mock_session.execute = AsyncMock(return_value=mock_existing_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.block_user(555, user_name="Test", blocked_by=1)

    assert result is False


# ═══════════════════════════════════════════════════════
# 3. unblock_user
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unblock_user_exists():
    """unblock_user удаляет запись и возвращает True."""
    import use_cases.blocked_users as mod

    mod._invalidate_cache()

    mock_session = AsyncMock()
    mock_del_result = MagicMock()
    mock_del_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=mock_del_result)
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.unblock_user(555)

    assert result is True


@pytest.mark.asyncio
async def test_unblock_user_not_found():
    """unblock_user возвращает False если не был заблокирован."""
    import use_cases.blocked_users as mod

    mod._invalidate_cache()

    mock_session = AsyncMock()
    mock_del_result = MagicMock()
    mock_del_result.rowcount = 0
    mock_session.execute = AsyncMock(return_value=mock_del_result)
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.unblock_user(999)

    assert result is False


# ═══════════════════════════════════════════════════════
# 4. get_all_blocked
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_all_blocked():
    """get_all_blocked возвращает список заблокированных."""
    import use_cases.blocked_users as mod

    mock_blocked = MagicMock()
    mock_blocked.telegram_id = 111
    mock_blocked.user_name = "John"
    mock_blocked.blocked_at = None

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_blocked]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.blocked_users.async_session_factory",
        return_value=mock_ctx,
    ):
        result = await mod.get_all_blocked()

    assert len(result) == 1
    assert result[0]["telegram_id"] == 111
    assert result[0]["user_name"] == "John"


# ═══════════════════════════════════════════════════════
# 5. Модель и интеграция
# ═══════════════════════════════════════════════════════


def test_blocked_user_model_exists():
    """Модель BlockedUser существует в db.models."""
    from db.models import BlockedUser

    assert BlockedUser.__tablename__ == "blocked_user"


def test_block_check_middleware_exists():
    """BlockCheckMiddleware импортируется из global_commands."""
    from bot.global_commands import BlockCheckMiddleware

    assert BlockCheckMiddleware is not None


def test_block_button_in_settings():
    """Кнопка '🚫 Заблокированные' есть в настройках."""
    from bot.permission_map import TEXT_PERMISSIONS

    assert "🚫 Заблокированные" in TEXT_PERMISSIONS


def test_block_button_in_nav():
    """Кнопка '🚫 Заблокированные' есть в NAV_BUTTONS."""
    from bot.global_commands import NAV_BUTTONS

    assert "🚫 Заблокированные" in NAV_BUTTONS
