"""Lightweight SQLite/Postgres column migrations for evolving schema."""

from sqlalchemy import inspect, text

from app.database import engine


def _pg() -> bool:
    return engine.dialect.name == "postgresql"


def _add_col(conn, table: str, col: str, ddl: str, existing: set[str]) -> None:
    if col in existing:
        return
    # SQLite uses DATETIME; Postgres wants TIMESTAMP
    if _pg():
        ddl = ddl.replace("DATETIME", "TIMESTAMP")
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def run_migrations() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    with engine.begin() as conn:
        cols = {c["name"] for c in inspector.get_columns("users")}
        _add_col(conn, "users", "role", "VARCHAR(40) DEFAULT 'owner'", cols)
        _add_col(conn, "users", "is_active", "INTEGER DEFAULT 1", cols)
        conn.execute(text("UPDATE users SET role = 'owner' WHERE role IS NULL OR role = ''"))

        if "app_settings" in tables:
            scols = {c["name"] for c in inspector.get_columns("app_settings")}
            _add_col(conn, "app_settings", "corn_cost_per_ac", "FLOAT DEFAULT 0", scols)
            _add_col(conn, "app_settings", "soy_cost_per_ac", "FLOAT DEFAULT 0", scols)
            _add_col(conn, "app_settings", "corn_price_assumption", "FLOAT", scols)
            _add_col(conn, "app_settings", "soy_price_assumption", "FLOAT", scols)
            _add_col(conn, "app_settings", "sprayer_tank_gal", "FLOAT DEFAULT 1000", scols)
            _add_col(conn, "app_settings", "sprayer_gpa", "FLOAT DEFAULT 15", scols)
            for col, ddl in (
                ("corn_futures", "FLOAT"),
                ("soy_futures", "FLOAT"),
                ("corn_futures_change", "FLOAT"),
                ("soy_futures_change", "FLOAT"),
                ("quote_as_of", "DATETIME"),
                ("quote_source", "VARCHAR(40)"),
                ("futures_strip_json", "TEXT"),
                ("quote_board_json", "TEXT"),
                ("corn_local_basis", "FLOAT"),
                ("soy_local_basis", "FLOAT"),
                ("corn_stress_shock", "FLOAT DEFAULT 0.5"),
                ("soy_stress_shock", "FLOAT DEFAULT 1.0"),
                ("carry_interest_apr", "FLOAT DEFAULT 7"),
                ("corn_storage_per_bu_mo", "FLOAT DEFAULT 0.03"),
                ("soy_storage_per_bu_mo", "FLOAT DEFAULT 0.04"),
                ("corn_shrink_per_bu_mo", "FLOAT DEFAULT 0.003"),
                ("soy_shrink_per_bu_mo", "FLOAT DEFAULT 0.004"),
                ("carry_mark_mode", "VARCHAR(20) DEFAULT 'cash'"),
                ("bins_show_carry", "INTEGER DEFAULT 0"),
            ):
                _add_col(conn, "app_settings", col, ddl, scols)
            scols = {c["name"] for c in inspector.get_columns("app_settings")}
            conn.execute(
                text(
                    "UPDATE app_settings SET corn_shrink_per_bu_mo = 0.003 "
                    "WHERE corn_shrink_per_bu_mo IS NULL OR corn_shrink_per_bu_mo = 0"
                )
            )
            conn.execute(
                text(
                    "UPDATE app_settings SET soy_shrink_per_bu_mo = 0.004 "
                    "WHERE soy_shrink_per_bu_mo IS NULL OR soy_shrink_per_bu_mo = 0"
                )
            )
            conn.execute(
                text(
                    "UPDATE app_settings SET carry_mark_mode = 'cash' "
                    "WHERE carry_mark_mode IS NULL OR carry_mark_mode = ''"
                )
            )
        if "equipment" in tables:
            ecols = {c["name"] for c in inspector.get_columns("equipment")}
            _add_col(conn, "equipment", "annual_housing", "FLOAT DEFAULT 0", ecols)

        if "hybrids" in tables:
            hcols = {c["name"] for c in inspector.get_columns("hybrids")}
            _add_col(conn, "hybrids", "unit_label", "VARCHAR(40)", hcols)
            _add_col(conn, "hybrids", "cost_per_unit", "FLOAT", hcols)
            _add_col(conn, "hybrids", "cost_per_acre", "FLOAT", hcols)

        if "spray_mixes" in tables:
            smcols = {c["name"] for c in inspector.get_columns("spray_mixes")}
            _add_col(conn, "spray_mixes", "cost_per_acre", "FLOAT", smcols)

        if "field_spray_mixes" in tables:
            fsmcols = {c["name"] for c in inspector.get_columns("field_spray_mixes")}
            _add_col(conn, "field_spray_mixes", "applied_date", "DATE", fsmcols)
            _add_col(conn, "field_spray_mixes", "treated_acres", "FLOAT", fsmcols)
            _add_col(conn, "field_spray_mixes", "weather_temp", "VARCHAR(40)", fsmcols)
            _add_col(conn, "field_spray_mixes", "weather_wind", "VARCHAR(40)", fsmcols)
            _add_col(conn, "field_spray_mixes", "voided", "INTEGER DEFAULT 0", fsmcols)

        if "field_hybrids" in tables:
            fhcols = {c["name"] for c in inspector.get_columns("field_hybrids")}
            _add_col(conn, "field_hybrids", "applied_date", "DATE", fhcols)
            _add_col(conn, "field_hybrids", "units_applied", "FLOAT", fhcols)
            _add_col(conn, "field_hybrids", "treated_acres", "FLOAT", fhcols)
            _add_col(conn, "field_hybrids", "voided", "INTEGER DEFAULT 0", fhcols)

        if "field_plans" in tables:
            fpcols = {c["name"] for c in inspector.get_columns("field_plans")}
            _add_col(conn, "field_plans", "estimated_cost_per_acre", "FLOAT", fpcols)
            _add_col(conn, "field_plans", "assigned_to", "VARCHAR(120)", fpcols)
            _add_col(conn, "field_plans", "priority", "VARCHAR(20)", fpcols)
            _add_col(conn, "field_plans", "op_kind", "VARCHAR(40)", fpcols)

        if "field_operations" in tables:
            focols = {c["name"] for c in inspector.get_columns("field_operations")}
            _add_col(conn, "field_operations", "voided", "INTEGER DEFAULT 0", focols)

        if "field_assignments" in tables:
            facols = {c["name"] for c in inspector.get_columns("field_assignments")}
            _add_col(conn, "field_assignments", "voided", "INTEGER DEFAULT 0", facols)

        if "spray_mix_lines" in tables:
            smlcols = {c["name"] for c in inspector.get_columns("spray_mix_lines")}
            _add_col(conn, "spray_mix_lines", "product_id", "INTEGER", smlcols)

        if "product_returns" in tables:
            prcols = {c["name"] for c in inspector.get_columns("product_returns")}
            _add_col(conn, "product_returns", "assignment_id", "INTEGER", prcols)

        if "input_purchases" in tables:
            ipcols = {c["name"] for c in inspector.get_columns("input_purchases")}
            _add_col(conn, "input_purchases", "notes", "TEXT", ipcols)
            _add_col(conn, "input_purchases", "crop_year_id", "INTEGER", ipcols)

        if "import_batches" in tables:
            ibcols = {c["name"] for c in inspector.get_columns("import_batches")}
            _add_col(conn, "import_batches", "payload_json", "TEXT", ibcols)
