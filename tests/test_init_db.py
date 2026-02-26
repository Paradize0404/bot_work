"""
test_init_db.py — smoke-тест миграций и валидация моделей.

Гарантирует, что:
1. create_tables() + все миграции проходят на чистой БД без ошибок.
2. После вставки граничных данных повторный прогон миграций не падает.
3. Numeric-колонки вмещают документированный диапазон значений.
"""

import importlib
import os

import pytest
from sqlalchemy import Numeric, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

# ---------------------------------------------------------------------------
#  Engine для CI-базы (не пользуемся db.engine — он читает config в import-time)
#  NullPool — каждый тест получает свой event-loop (scope=function),
#  поэтому нельзя переиспользовать asyncpg-соединения между тестами.
# ---------------------------------------------------------------------------
_TEST_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)
if _TEST_DB_URL.startswith("postgresql://"):
    _TEST_DB_URL = _TEST_DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _TEST_DB_URL.startswith("postgres://"):
    _TEST_DB_URL = _TEST_DB_URL.replace("postgres://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_TEST_DB_URL, echo=False, poolclass=NullPool)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------
def _all_models():
    """Возвращает все ORM-классы (из db.models, db.ft_models, models.ocr)."""
    from db.models import Base

    # force-import all model modules so Base.registry sees every table
    import db.ft_models  # noqa: F401
    import models.ocr  # noqa: F401

    return [
        mapper.class_ for mapper in Base.registry.mappers if hasattr(mapper, "class_")
    ]


def _numeric_columns():
    """Yield (model_cls, column_name, precision, scale) for every Numeric column."""
    for model in _all_models():
        table = model.__table__
        for col in table.columns:
            col_type = col.type
            if isinstance(col_type, Numeric) and col_type.precision is not None:
                yield model, col.name, col_type.precision, col_type.scale or 0


# ---------------------------------------------------------------------------
#  Boundary (max value) expectations for known columns.
#  key = (table_name, column_name) → max absolute value that can appear.
# ---------------------------------------------------------------------------
_BOUNDARY_VALUES: dict[tuple[str, str], float] = {
    # confidence_score: 0–100
    ("ocr_document", "confidence_score"): 100.0,
    ("ocr_item", "confidence_score"): 100.0,
    # money columns: 12-digit total (99_999_999.9999)
    ("ocr_document", "total_amount"): 99_999_999.0,
    ("ocr_document", "total_vat"): 99_999_999.0,
    ("ocr_item", "qty"): 99_999_999.0,
    ("ocr_item", "price"): 99_999_999.0,
    ("ocr_item", "sum"): 99_999_999.0,
    # salary
    ("salary_history", "mot_pct"): 9999.0,
}


# ===================================================================
#  TESTS
# ===================================================================


class TestInitDbMigrations:
    """create_tables() must succeed on a clean DB (and be idempotent)."""

    @pytest.fixture(autouse=True)
    async def _setup_teardown(self):
        """Drop all tables before & after each test for isolation."""
        from db.models import Base
        import db.ft_models  # noqa: F401
        import models.ocr  # noqa: F401

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        yield
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def test_create_tables_clean_db(self):
        """Миграции проходят на абсолютно чистой БД."""
        from db.models import Base
        import db.ft_models  # noqa: F401
        import models.ocr  # noqa: F401

        # create_all equivalent
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # run migrations
        # We need to call the migration SQL list directly, using our test engine
        # because init_db.create_tables imports `engine` from db.engine (prod config)
        await self._run_migrations()

    async def test_create_tables_idempotent(self):
        """Повторный прогон create+миграция не падает."""
        from db.models import Base
        import db.ft_models  # noqa: F401
        import models.ocr  # noqa: F401

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._run_migrations()

        # second run
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._run_migrations()

    async def test_migrations_with_boundary_data(self):
        """
        Вставляет граничные данные в REAL-колонки, потом запускает ALTER TYPE
        миграции — overflow не должен возникнуть.
        """
        from db.models import Base
        import db.ft_models  # noqa: F401
        import models.ocr  # noqa: F401

        # 1. Create tables with REAL columns (simulate old schema)
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 2. Force columns to REAL (undo the Numeric from model definition)
        async with _engine.begin() as conn:
            for (tbl, col), max_val in _BOUNDARY_VALUES.items():
                # Check table exists
                r = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = :t"
                    ),
                    {"t": tbl},
                )
                if not r.fetchone():
                    continue
                # Check column exists
                r = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": tbl, "c": col},
                )
                if not r.fetchone():
                    continue
                await conn.execute(
                    text(f'ALTER TABLE "{tbl}" ALTER COLUMN "{col}" TYPE REAL')
                )

        # 3. Insert boundary data
        async with _engine.begin() as conn:
            # ocr_document with confidence_score = 100
            await conn.execute(
                text(
                    "INSERT INTO ocr_document "
                    "(id, telegram_id, doc_type, confidence_score,"
                    " status, category, currency, page_count, is_multistage, created_at) "
                    "VALUES ('test-overflow-1', '12345', 'upd', 100.0,"
                    " 'recognized', 'goods', 'RUB', 1, false, NOW())"
                )
            )
            # ocr_item with confidence_score = 100
            await conn.execute(
                text(
                    "INSERT INTO ocr_item "
                    "(id, document_id, raw_name, confidence_score, mapping_status, is_auto_corrected) "
                    "VALUES ('test-item-1', 'test-overflow-1', 'test', 100.0, 'pending', false)"
                )
            )

        # 4. Run migrations — must NOT raise
        await self._run_migrations()

        # 5. Verify data survived
        async with _Session() as s:
            r = await s.execute(
                text(
                    "SELECT confidence_score FROM ocr_document WHERE id = 'test-overflow-1'"
                )
            )
            val = r.scalar()
            assert val is not None
            assert float(val) == pytest.approx(100.0, abs=0.01)

    # ── helper: replay migration SQL from init_db using test engine ──

    async def _run_migrations(self):
        """Выполнить все миграции из init_db._MIGRATIONS на тестовой БД."""
        import db.init_db as init_mod

        # Reload to pick up any changes
        importlib.reload(init_mod)

        from db.init_db import create_tables

        # Monkey-patch BOTH db.engine.engine AND db.init_db.engine
        # (create_tables resolves `engine` from db.init_db module globals)
        import db.engine as eng_mod

        orig_eng = eng_mod.engine
        orig_init_eng = init_mod.engine
        eng_mod.engine = _engine
        init_mod.engine = _engine
        try:
            await create_tables()
        finally:
            eng_mod.engine = orig_eng
            init_mod.engine = orig_init_eng


class TestNumericPrecision:
    """Статическая проверка: Numeric(p,s) вмещает документированный диапазон."""

    def test_all_numeric_columns_have_enough_room(self):
        """
        Для каждой Numeric(p,s) колонки с известным _BOUNDARY_VALUES
        проверяем: max_int_digits = p - s >= digits_needed(max_value).
        """
        failures = []
        for model, col_name, prec, scale in _numeric_columns():
            tbl = model.__tablename__
            key = (tbl, col_name)
            if key not in _BOUNDARY_VALUES:
                continue

            max_val = _BOUNDARY_VALUES[key]
            int_digits_available = prec - scale  # slots for integer part
            int_digits_needed = len(str(int(abs(max_val))))

            if int_digits_available < int_digits_needed:
                failures.append(
                    f"{tbl}.{col_name}: Numeric({prec},{scale}) "
                    f"max integer part = {int_digits_available} digits, "
                    f"but need {int_digits_needed} for value {max_val}"
                )

        assert not failures, "Numeric precision overflow risk detected:\n" + "\n".join(
            failures
        )

    def test_confidence_score_holds_100(self):
        """confidence_score (0–100) НЕ должен быть Numeric(5,4) — макс 9.9999."""
        for model, col_name, prec, scale in _numeric_columns():
            if col_name != "confidence_score":
                continue
            int_part = prec - scale
            assert int_part >= 3, (
                f"{model.__tablename__}.confidence_score: "
                f"Numeric({prec},{scale}) can only hold {10**int_part - 1} max, "
                f"but values go up to 100"
            )
