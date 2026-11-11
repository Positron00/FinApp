"""SQLAlchemy 2.1 is instrumented despite the instrumentation version gate (S6; NFR-09)."""

import asyncio

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pycommon import otel
from pycommon.db import create_engine
from sqlalchemy import text

from tests.integration.conftest import Pg

pytestmark = pytest.mark.integration


def test_db_queries_produce_client_spans(pg: Pg) -> None:
    """PAY-NFR-09: pycommon.otel.setup instruments the engine, so a query yields a DB span."""
    settings = pg.settings()
    engine = create_engine(settings)
    exporter = InMemorySpanExporter()
    telemetry = otel.setup(
        None, settings, engine=engine, redis_instrument=False, span_exporter=exporter
    )
    try:

        async def query() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()

        asyncio.run(query())
        systems = {
            (s.attributes or {}).get("db.system.name") or (s.attributes or {}).get("db.system")
            for s in exporter.get_finished_spans()
        }
        assert "postgresql" in systems
    finally:
        telemetry.shutdown()
