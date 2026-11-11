# Copyright 2025 sysmocom - s.f.m.c. GmbH <info@sysmocom.de>
# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import pytest
import shlex
import shutil
import sqlglot
import subprocess
from conftest import wait_for_tcp_port
from database import Database
from logtool import LogTool
from pathlib import Path
from pyhss_config import config

top_dir = Path(Path(__file__) / "../..").resolve()
mariadb_port = 33061


@pytest.fixture
def run_mariadbd(tmpdir):
    programs = [
        "mariadb",
        "mariadb-install-db",
        "mariadbd",
    ]
    for program in programs:
        if not shutil.which(program):
            pytest.skip(f"{program} is not installed")
            return

    cmd = [
        "mariadb-install-db",
        "--no-defaults",
        "--auth-root-authentication-method=normal",
        "--skip-test-db",
        f"--datadir={tmpdir}",
    ]
    print(f"+ {cmd}")
    subprocess.run(cmd, check=True)

    cmd = [
        "mariadbd",
        "--no-defaults",
        "--bind-address=127.0.0.1",
        f"--port={mariadb_port}",
        f"--socket={tmpdir}/.socket",
        f"--datadir={tmpdir}",
    ]
    print(f"+ {cmd}")
    proc = subprocess.Popen(cmd)

    try:
        wait_for_tcp_port(mariadb_port)
        cmd = f"mariadb -u root -h 127.0.0.1 -P {mariadb_port} -e 'CREATE DATABASE hss;'"
        print(f"+ {cmd}")
        subprocess.run(cmd, check=True, shell=True)
        yield
    finally:
        proc.kill()


def mariadb_patch_config(monkeypatch):
    monkeypatch.setitem(config["database"], "db_type", "mysql")
    monkeypatch.setitem(config["database"], "server", f"127.0.0.1:{mariadb_port}")
    monkeypatch.setitem(config["database"], "username", "root")
    monkeypatch.setitem(config["database"], "password", "")
    monkeypatch.setitem(config["database"], "database", "hss")


def mariadb_import(tmpdir, sql_file):
    sql_path_orig = os.path.join(top_dir, "tests/db_schema", sql_file)
    sql_path_temp = os.path.join(tmpdir, "import.sql")

    with open(sql_path_orig) as f:
        sql = f.read()
    with open(sql_path_temp, "w") as f:
        sql = sqlglot.transpile(sql, read="sqlite", write="mysql", pretty=True)
        sql = ";\n\n".join(sql) + ";\n"
        f.write(sql)

    cmd = f"mariadb -u root -h 127.0.0.1 -P {mariadb_port} hss < {shlex.quote(sql_path_temp)}"
    print(f"+ {cmd}")
    subprocess.run(cmd, check=True, shell=True)


@pytest.mark.slow
def test_mariadb_new_db(run_mariadbd, monkeypatch):
    mariadb_patch_config(monkeypatch)

    db = Database(LogTool(config), main_service=True)
    db.engine.dispose()


@pytest.mark.slow
def test_mariadb_upgrade_from_1_0_1(run_mariadbd, monkeypatch, tmpdir):
    mariadb_patch_config(monkeypatch)
    mariadb_import(tmpdir, "20240125_release_1.0.1.sql")

    db = Database(LogTool(config), main_service=True)
    db.engine.dispose()


@pytest.mark.slow
def test_mariadb_unsupported_1_0_0(run_mariadbd, monkeypatch, tmpdir):
    mariadb_patch_config(monkeypatch)
    mariadb_import(tmpdir, "20231009_release_1.0.0.sql")

    with pytest.raises(SystemExit) as e:
        Database(LogTool(config), main_service=True)
    assert e.value.code == 20


def ifc_template_foreign_keys(db):
    import sqlalchemy
    inspector = sqlalchemy.inspect(db.engine)
    return {table for table in ("ims_subscriber", "operation_log")
            if any(fk["constrained_columns"] == ["ifc_template_id"] and fk["referred_table"] == "ifc_template"
                   for fk in inspector.get_foreign_keys(table))}


@pytest.mark.slow
def test_mariadb_upgrade_adds_ifc_template_foreign_keys(run_mariadbd, monkeypatch, tmpdir):
    # KI-65: upgrade 2 added the columns without the foreign keys a new
    # database has, so a template still in use could be deleted.
    mariadb_patch_config(monkeypatch)
    mariadb_import(tmpdir, "20240125_release_1.0.1.sql")

    db = Database(LogTool(config), main_service=True)

    assert ifc_template_foreign_keys(db) == {"ims_subscriber", "operation_log"}
    db.engine.dispose()


@pytest.mark.slow
def test_mariadb_upgrade_clears_dangling_ifc_template_ids(run_mariadbd, monkeypatch, tmpdir):
    # A subscriber pointing at a deleted template would make ADD CONSTRAINT
    # fail, and with it the start of every HSS pod. The upgrade clears the id.
    import sqlalchemy
    from databaseSchema import DatabaseSchema
    mariadb_patch_config(monkeypatch)
    mariadb_import(tmpdir, "20240125_release_1.0.1.sql")
    with monkeypatch.context() as before_upgrade_8:
        before_upgrade_8.setattr(DatabaseSchema, "latest", 7)
        before_upgrade_8.setattr(DatabaseSchema, "upgrade_add_ifc_template_foreign_keys", lambda self: None)
        db = Database(LogTool(config), main_service=True)
        with db.engine.connect() as conn:
            conn.execute(sqlalchemy.text(
                "INSERT INTO ims_subscriber (ims_subscriber_id, imsi, msisdn, ifc_template_id) VALUES (9999, '001010000000009', '4915100000009', 999)"))
            conn.commit()
        db.engine.dispose()

    db = Database(LogTool(config), main_service=True)

    with db.engine.connect() as conn:
        rows = conn.execute(sqlalchemy.text(
            "SELECT ifc_template_id FROM ims_subscriber WHERE imsi = '001010000000009'")).all()
    assert rows == [(None,)]
    assert ifc_template_foreign_keys(db) == {"ims_subscriber", "operation_log"}
    db.engine.dispose()
