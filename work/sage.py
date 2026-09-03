#!/usr/bin/env python3
"""Read-only Sage connection helper. Every statement run through here is a
SELECT. Config comes from the project's .env (SQL_HOST / SQL_PORT / SQL_USER /
SQL_PASSWORD / SQL_DATABASE)."""
import os
import pymssql


def _env(path="/home/namansharma/Desktop/sage-pull/.env"):
    cfg = {}
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                cfg[k.strip()] = v.strip().strip("'\"")
    return cfg


CFG = _env()


def connect():
    return pymssql.connect(
        server=CFG["SQL_HOST"], port=int(CFG.get("SQL_PORT", 1433)),
        user=CFG["SQL_USER"], password=CFG["SQL_PASSWORD"],
        database=CFG["SQL_DATABASE"], login_timeout=20, timeout=600)


def q(sql, params=()):
    with connect() as cn, cn.cursor(as_dict=True) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


if __name__ == "__main__":
    print("host %s  db %s" % (CFG["SQL_HOST"], CFG["SQL_DATABASE"]))
    print(q("SELECT @@VERSION AS v")[0]["v"].splitlines()[0])
