"""Миграция квитанций периода: upgrade, состав ключа, downgrade на SQLite."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_group_coverage_migration(tmp_path):
    path = Path(__file__).parents[1] / "migrations/versions/0010_group_schedule_coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            schema = inspect(connection)
            assert schema.get_pk_constraint("group_schedule_coverage")["constrained_columns"] == ["group_id", "period_start"]
            assert {fk["referred_table"] for fk in schema.get_foreign_keys("group_schedule_coverage")} == {"study_groups", "import_runs"}
            module.downgrade()
            assert "group_schedule_coverage" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
