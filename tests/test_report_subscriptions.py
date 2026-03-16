"""
Тесты: подписки на отчёты дня (use_cases/report_subscriptions.py).

Запуск: pytest tests/test_report_subscriptions.py -v
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════
# Фикстуры
# ═══════════════════════════════════════════════════════

DEPT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
DEPT_ID_2 = "bbbbbbbb-0000-0000-0000-000000000002"


# ═══════════════════════════════════════════════════════
# 1. get_subscribers_for_department
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_subscribers_for_department():
    """get_subscribers_for_department возвращает telegram_id подписчиков."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [(100,), (200,)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import get_subscribers_for_department

        result = await get_subscribers_for_department(DEPT_ID)

    assert result == [100, 200]


@pytest.mark.asyncio
async def test_get_subscribers_empty():
    """Нет подписчиков — пустой список."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import get_subscribers_for_department

        result = await get_subscribers_for_department(DEPT_ID)

    assert result == []


# ═══════════════════════════════════════════════════════
# 2. add_subscription
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_subscription_new():
    """add_subscription создаёт новую подписку и возвращает True."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # подписки нет
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import add_subscription

        result = await add_subscription(
            telegram_id=100,
            department_id=DEPT_ID,
            created_by=1,
        )

    assert result is True
    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_subscription_already_exists():
    """add_subscription возвращает False если подписка уже существует."""
    existing_sub = MagicMock()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_sub
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import add_subscription

        result = await add_subscription(
            telegram_id=100,
            department_id=DEPT_ID,
        )

    assert result is False
    mock_session.add.assert_not_called()


# ═══════════════════════════════════════════════════════
# 3. remove_subscription
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_remove_subscription_exists():
    """remove_subscription возвращает True если подписка удалена."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import remove_subscription

        result = await remove_subscription(telegram_id=100, department_id=DEPT_ID)

    assert result is True


@pytest.mark.asyncio
async def test_remove_subscription_not_found():
    """remove_subscription возвращает False если подписка не найдена."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 0
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "use_cases.report_subscriptions.async_session_factory",
        return_value=mock_ctx,
    ):
        from use_cases.report_subscriptions import remove_subscription

        result = await remove_subscription(telegram_id=100, department_id=DEPT_ID)

    assert result is False


# ═══════════════════════════════════════════════════════
# 4. DB model — ReportSubscription
# ═══════════════════════════════════════════════════════


def test_report_subscription_model_exists():
    """Модель ReportSubscription существует и имеет нужные поля."""
    from db.models import ReportSubscription

    assert ReportSubscription.__tablename__ == "report_subscription"
    assert hasattr(ReportSubscription, "telegram_id")
    assert hasattr(ReportSubscription, "department_id")
    assert hasattr(ReportSubscription, "created_by")


def test_guest_user_model_exists():
    """Модель GuestUser существует и имеет нужные поля."""
    from db.models import GuestUser

    assert GuestUser.__tablename__ == "guest_user"
    assert hasattr(GuestUser, "telegram_id")
    assert hasattr(GuestUser, "full_name")
    assert hasattr(GuestUser, "department_id")


# ═══════════════════════════════════════════════════════
# 5. Day report: recipients include subscriptions
# ═══════════════════════════════════════════════════════


def test_day_report_imports_report_subscriptions():
    """day_report_handlers импортирует модуль report_subscriptions."""
    import bot.day_report_handlers as drh

    assert hasattr(drh, "sub_uc")


def test_permission_map_has_day_report_receive():
    """permission_map содержит PERM_DAY_REPORT_RECEIVE."""
    from bot.permission_map import PERM_DAY_REPORT_RECEIVE

    assert PERM_DAY_REPORT_RECEIVE == "📋 Получатель отчёта дня"


def test_report_sub_button_in_settings():
    """Кнопка «📬 Подписки на отчёты» должна быть в TEXT_PERMISSIONS."""
    from bot.permission_map import TEXT_PERMISSIONS, PERM_SETTINGS

    assert "📬 Подписки на отчёты" in TEXT_PERMISSIONS
    assert TEXT_PERMISSIONS["📬 Подписки на отчёты"] == PERM_SETTINGS
