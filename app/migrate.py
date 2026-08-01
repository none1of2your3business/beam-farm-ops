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
            _add_col(conn, "app_settings", "operation_rates_json", "TEXT", scols)
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
                ("corn_basis_shock", "FLOAT DEFAULT 0.20"),
                ("soy_basis_shock", "FLOAT DEFAULT 0.30"),
                ("carry_interest_apr", "FLOAT DEFAULT 7"),
                ("corn_storage_per_bu_mo", "FLOAT DEFAULT 0.03"),
                ("soy_storage_per_bu_mo", "FLOAT DEFAULT 0.04"),
                ("corn_shrink_per_bu_mo", "FLOAT DEFAULT 0.003"),
                ("soy_shrink_per_bu_mo", "FLOAT DEFAULT 0.004"),
                ("carry_mark_mode", "VARCHAR(20) DEFAULT 'cash'"),
                ("bins_show_carry", "INTEGER DEFAULT 0"),
                ("budget_defaults_json", "TEXT"),
                ("budget_travel_speed_mph", "FLOAT DEFAULT 30"),
                ("budget_travel_rate_per_hr", "FLOAT DEFAULT 150"),
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

        if "fields" in tables:
            fcols = {c["name"] for c in inspector.get_columns("fields")}
            _add_col(conn, "fields", "distance_miles", "FLOAT", fcols)

        if "fertilizer_products" in tables:
            fpcols = {c["name"] for c in inspector.get_columns("fertilizer_products")}
            _add_col(conn, "fertilizer_products", "tons_purchased", "FLOAT DEFAULT 0", fpcols)

        if "field_budgets" in tables:
            bcols = {c["name"] for c in inspector.get_columns("field_budgets")}
            for col, ddl in (
                ("seed_hybrid_id", "INTEGER"),
                ("seed_population", "FLOAT"),
                ("seed_bag_kernels", "FLOAT DEFAULT 80000"),
                ("seed_cost_per_bag", "FLOAT"),
            ):
                _add_col(conn, "field_budgets", col, ddl, bcols)

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

        if "field_plans" in tables:
            fpcols = {c["name"] for c in inspector.get_columns("field_plans")}
            _add_col(conn, "field_plans", "estimated_cost_per_acre", "FLOAT", fpcols)

        if "field_spray_mixes" in tables:
            fsmcols = {c["name"] for c in inspector.get_columns("field_spray_mixes")}
            _add_col(conn, "field_spray_mixes", "applied_date", "DATE", fsmcols)
            _add_col(conn, "field_spray_mixes", "invoice_id", "INTEGER", fsmcols)

        if "field_hybrids" in tables:
            fhcols = {c["name"] for c in inspector.get_columns("field_hybrids")}
            _add_col(conn, "field_hybrids", "applied_date", "DATE", fhcols)
            _add_col(conn, "field_hybrids", "acres", "FLOAT", fhcols)
            _add_col(conn, "field_hybrids", "units", "FLOAT", fhcols)
            _add_col(conn, "field_hybrids", "population", "FLOAT", fhcols)
            _add_col(conn, "field_hybrids", "client_name", "VARCHAR(160)", fhcols)
            _add_col(conn, "field_hybrids", "invoice_id", "INTEGER", fhcols)

        if "field_assignments" in tables:
            facols = {c["name"] for c in inspector.get_columns("field_assignments")}
            _add_col(conn, "field_assignments", "invoice_id", "INTEGER", facols)

        if "input_purchases" in tables:
            ipcols = {c["name"] for c in inspector.get_columns("input_purchases")}
            _add_col(conn, "input_purchases", "notes", "TEXT", ipcols)
            _add_col(conn, "input_purchases", "crop_year_id", "INTEGER", ipcols)

        if "import_batches" in tables:
            ibcols = {c["name"] for c in inspector.get_columns("import_batches")}
            _add_col(conn, "import_batches", "payload_json", "TEXT", ibcols)

        if "grain_movements" in tables:
            gmcols = {c["name"] for c in inspector.get_columns("grain_movements")}
            _add_col(conn, "grain_movements", "hauler", "VARCHAR(160)", gmcols)
            _add_col(conn, "grain_movements", "freight_per_bu", "FLOAT", gmcols)

        if "bin_shares" in tables:
            bscols = {c["name"] for c in inspector.get_columns("bin_shares")}
            _add_col(conn, "bin_shares", "share_pct", "FLOAT", bscols)
            _add_col(conn, "bin_shares", "carry_start_date", "DATE", bscols)
            _add_col(conn, "bin_shares", "carry_accrued", "FLOAT", bscols)
            _add_col(conn, "bin_shares", "carry_period_base", "FLOAT", bscols)

        if "field_operations" in tables:
            focols = {c["name"] for c in inspector.get_columns("field_operations")}
            _add_col(conn, "field_operations", "invoice_id", "INTEGER", focols)

        if "invoices" in tables:
            invcols = {c["name"] for c in inspector.get_columns("invoices")}
            _add_col(conn, "invoices", "field_id", "INTEGER", invcols)

        if "invoice_lines" in tables:
            ilcols = {c["name"] for c in inspector.get_columns("invoice_lines")}
            _add_col(conn, "invoice_lines", "field_operation_id", "INTEGER", ilcols)

        if "grain_contracts" in tables:
            gccols = {c["name"] for c in inspector.get_columns("grain_contracts")}
            _add_col(conn, "grain_contracts", "with_party_id", "INTEGER", gccols)
            _add_col(conn, "grain_contracts", "contract_number", "VARCHAR(80)", gccols)
            # Backfill contract_number from notes like import:12345 when empty
            conn.execute(
                text(
                    "UPDATE grain_contracts SET contract_number = SUBSTR(notes, 8) "
                    "WHERE (contract_number IS NULL OR contract_number = '') "
                    "AND notes IS NOT NULL AND notes LIKE 'import:%' "
                    "AND LENGTH(notes) > 7"
                )
            )

        if "grain_bins" in tables:
            gbcols = {c["name"] for c in inspector.get_columns("grain_bins")}
            _add_col(conn, "grain_bins", "with_party_id", "INTEGER", gbcols)
