"""
save_db.py: copy the CSVs in data/ into JUDICIAL_DB_URL.

The CSVs are the source of truth, so each run recreates the tables in one
transaction. A failed run leaves the database as it was and loses nothing.
A CSV that does not exist yet is skipped.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from catalog import load_env

DATA = Path(__file__).parent / "data"

TABLES = {
    "catalogo": """
        principio_ativo    TEXT PRIMARY KEY,
        preco_max          NUMERIC(12, 2),
        termos_busca       TEXT NOT NULL,
        marcas             TEXT,
        classe_terapeutica TEXT,
        n_eans             INTEGER NOT NULL,
        n_lojas            INTEGER NOT NULL,
        origem             TEXT NOT NULL
    """,
    "notas": """
        termo         TEXT PRIMARY KEY,
        notas         INTEGER NOT NULL,
        favoraveis    INTEGER,
        preco_max     NUMERIC(12, 2),
        atualizado_em DATE NOT NULL
    """,
    "medicamentos": """
        principio_ativo  TEXT PRIMARY KEY,
        marcas           TEXT,
        preco_max        NUMERIC(12, 2),
        notas_min        INTEGER NOT NULL,
        notas_max        INTEGER NOT NULL,
        termo_principal  TEXT NOT NULL,
        favoraveis       INTEGER,
        pct_favoravel    NUMERIC(5, 1),
        termos_coletados INTEGER NOT NULL,
        termos_total     INTEGER NOT NULL,
        atualizado_em    DATE NOT NULL
    """,
    "judiciario": """
        tribunal       TEXT PRIMARY KEY,
        periodo        TEXT NOT NULL,
        acoes          INTEGER NOT NULL,
        amostra        INTEGER NOT NULL,
        com_julgamento INTEGER NOT NULL,
        procedentes    INTEGER NOT NULL,
        parciais       INTEGER NOT NULL,
        improcedentes  INTEGER NOT NULL,
        pct_favoravel  NUMERIC(5, 1),
        dias_mediana   NUMERIC(8, 1),
        dias_p25       NUMERIC(8, 1),
        dias_p75       NUMERIC(8, 1),
        atualizado_em  DATE NOT NULL
    """,
    "historico": """
        data  DATE NOT NULL,
        termo TEXT NOT NULL,
        notas INTEGER NOT NULL,
        PRIMARY KEY (data, termo)
    """,
}
# A count that changes twice in a day (a local run and the workflow) keeps the last value.
UNIQUE_KEYS = {"historico": ["data", "termo"]}


def read_rows(table: str) -> tuple[list, list]:
    frame = pd.read_csv(DATA / f"{table}.csv", dtype=str, keep_default_na=False)
    frame = frame.drop_duplicates(UNIQUE_KEYS.get(table), keep="last")
    frame = frame.astype(object).where(frame != "", None)
    return list(frame.columns), list(frame.itertuples(index=False, name=None))


def main() -> int:
    load_env()
    url = os.environ.get("JUDICIAL_DB_URL")
    if not url:
        print("JUDICIAL_DB_URL is not set")
        return 1
    try:
        with psycopg2.connect(url, connect_timeout=30) as connection, connection.cursor() as cursor:
            for table, columns in TABLES.items():
                if not (DATA / f"{table}.csv").exists():
                    print(f"{table}: no CSV yet, skipped")
                    continue
                names, rows = read_rows(table)
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                cursor.execute(f"CREATE TABLE {table} ({columns})")
                if rows:
                    execute_values(cursor, f"INSERT INTO {table} ({', '.join(names)}) VALUES %s", rows)
                print(f"{table}: {len(rows):,} rows")
    except psycopg2.Error as error:
        print(f"Database error: {type(error).__name__}")  # the message could hold the host
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
