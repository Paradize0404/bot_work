"""
Тесты: гостевая авторизация (use_cases/auth.py — guest registration).

Запуск: pytest tests/test_guest_auth.py -v
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════
# 1. register_guest — создание гостевого пользователя
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_register_guest_creates_new():
    """register_guest создаёт нового гостя и записывает в кеш."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # гость не существует
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("use_cases.auth.async_session_factory", return_value=mock_ctx_manager),
        patch(
            "use_cases.auth.uctx.set_context", new_callable=AsyncMock
        ) as mock_set_ctx,
    ):
        from use_cases.auth import register_guest

        await register_guest(telegram_id=999, full_name="Иванов Иван Иванович")

    # Проверяем, что add был вызван (новый гость)
    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()
    # Проверяем, что контекст записан
    mock_set_ctx.assert_awaited_once()
    call_kwargs = mock_set_ctx.call_args.kwargs
    assert call_kwargs["telegram_id"] == 999
    assert call_kwargs["employee_id"] == "guest"
    assert call_kwargs["employee_name"] == "Иванов Иван Иванович"
    assert call_kwargs["first_name"] == "Иванов"
    assert call_kwargs["role_name"] == "Гость"


@pytest.mark.asyncio
async def test_register_guest_updates_existing():
    """register_guest обновляет имя если гость уже существует."""
    existing_guest = MagicMock()
    existing_guest.full_name = "Старое Имя"

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_guest
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("use_cases.auth.async_session_factory", return_value=mock_ctx_manager),
        patch("use_cases.auth.uctx.set_context", new_callable=AsyncMock),
    ):
        from use_cases.auth import register_guest

        await register_guest(telegram_id=999, full_name="Новое Имя")

    # Имя обновлено
    assert existing_guest.full_name == "Новое Имя"
    # add не вызывается для существующего гостя
    mock_session.add.assert_not_called()


# ═══════════════════════════════════════════════════════
# 2. check_auth_status — проверка гостей
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_check_auth_status_guest_authorized():
    """check_auth_status возвращает AUTHORIZED для гостя с department_id."""
    guest = MagicMock()
    guest.full_name = "Инвестор Иванов"
    guest.department_id = "aaaaaaaa-0000-0000-0000-000000000001"

    with (
        patch(
            "use_cases.auth.uctx.get_user_context",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "use_cases.auth.get_guest_user",
            new_callable=AsyncMock,
            return_value=guest,
        ),
        patch("use_cases.auth.uctx.set_context", new_callable=AsyncMock),
    ):
        from use_cases.auth import check_auth_status, AuthStatus

        result = await check_auth_status(telegram_id=999)

    assert result.status == AuthStatus.AUTHORIZED
    assert result.first_name == "Инвестор"


@pytest.mark.asyncio
async def test_check_auth_status_guest_needs_department():
    """check_auth_status возвращает NEEDS_DEPARTMENT для гостя без department_id."""
    guest = MagicMock()
    guest.full_name = "Инвестор Петров"
    guest.department_id = None

    with (
        patch(
            "use_cases.auth.uctx.get_user_context",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "use_cases.auth.get_guest_user",
            new_callable=AsyncMock,
            return_value=guest,
        ),
        patch("use_cases.auth.uctx.set_context", new_callable=AsyncMock),
    ):
        from use_cases.auth import check_auth_status, AuthStatus

        result = await check_auth_status(telegram_id=999)

    assert result.status == AuthStatus.NEEDS_DEPARTMENT


@pytest.mark.asyncio
async def test_check_auth_status_not_authorized():
    """check_auth_status возвращает NOT_AUTHORIZED если нет ни сотрудника, ни гостя."""
    with (
        patch(
            "use_cases.auth.uctx.get_user_context",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "use_cases.auth.get_guest_user",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        from use_cases.auth import check_auth_status, AuthStatus

        result = await check_auth_status(telegram_id=999)

    assert result.status == AuthStatus.NOT_AUTHORIZED


# ═══════════════════════════════════════════════════════
# 3. get_employees_with_telegram — включает гостей
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_employees_with_telegram_includes_guests():
    """get_employees_with_telegram возвращает и сотрудников, и гостей."""
    mock_emp = MagicMock()
    mock_emp.id = "11111111-0000-0000-0000-000000000001"
    mock_emp.name = "Сотрудник Иванов"
    mock_emp.last_name = "Иванов"
    mock_emp.first_name = "Сотрудник"
    mock_emp.telegram_id = 100

    mock_guest = MagicMock()
    mock_guest.pk = 1
    mock_guest.full_name = "Инвестор Петров"
    mock_guest.telegram_id = 200

    mock_session = AsyncMock()

    emp_result = MagicMock()
    emp_result.scalars.return_value.all.return_value = [mock_emp]

    guest_result = MagicMock()
    guest_result.scalars.return_value.all.return_value = [mock_guest]

    mock_session.execute = AsyncMock(side_effect=[emp_result, guest_result])

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

    with patch("use_cases.admin.async_session_factory", return_value=mock_ctx_manager):
        from use_cases.admin import get_employees_with_telegram

        items = await get_employees_with_telegram()

    assert len(items) == 2
    assert items[0]["telegram_id"] == 100
    assert items[0]["name"] == "Сотрудник Иванов"
    assert items[1]["telegram_id"] == 200
    assert "(гость)" in items[1]["name"]
