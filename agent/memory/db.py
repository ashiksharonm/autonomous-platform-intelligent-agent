import os
import sqlite3
from contextlib import contextmanager

from agent.config import sqlite_file_location

database_tables_sql = """
CREATE TABLE IF NOT EXISTS capabilities (
    op_name           TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    code              TEXT,
    signature_desc    TEXT NOT NULL,
    success_count     INTEGER NOT NULL DEFAULT 0,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    total_api_calls   INTEGER NOT NULL DEFAULT 0,
    total_latency_ms  INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT,
    known_constraints TEXT NOT NULL DEFAULT '[]',
    last_error_class  TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    raw_instruction    TEXT NOT NULL,
    task_type          TEXT NOT NULL,
    decomposition_json TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    total_api_calls    INTEGER NOT NULL DEFAULT 0,
    total_latency_ms   INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    step_id       TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    step_index    INTEGER NOT NULL,
    op_name       TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_class   TEXT,
    api_calls     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    agent_decision TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_task_type ON runs(task_type);
CREATE INDEX IF NOT EXISTS idx_steps_run_id ON steps(run_id);
"""


def create_db_connection():
    folder_path = os.path.dirname(sqlite_file_location)
    
    # this will create the folder if it doesn't exist.
    if folder_path == "":
        os.makedirs(".", exist_ok=True)
    else:
        os.makedirs(folder_path, exist_ok=True)
        
    my_conn = sqlite3.connect(sqlite_file_location)
    my_conn.row_factory = sqlite3.Row
    my_conn.execute("PRAGMA foreign_keys = ON")
    
    return my_conn


def setup_tables():
    # this will create the necessary tables in the database if they don't already exist.
    active_conn = create_db_connection()
    try:
        active_conn.executescript(database_tables_sql)
        active_conn.commit()
    finally:
        active_conn.close()


@contextmanager
def db_cursor():
    # this will create a database connection and yield a cursor for executing SQL commands.
    active_conn = create_db_connection()
    try:
        active_cursor = active_conn.cursor()
        yield active_cursor
        active_conn.commit()
    finally:
        active_conn.close()