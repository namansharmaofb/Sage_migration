#!/usr/bin/env python
"""Find the Sage box wherever DHCP has put it today, and prove it IS Sage.

  .venv/bin/python work/find_sage.py            # print the host, exit 0
  .venv/bin/python work/find_sage.py --write    # ...and rewrite SQL_HOST in .env
  .venv/bin/python work/find_sage.py --quiet    # print only the host

WHY THIS EXISTS

    The "Sage box" is not a server. It is a colleague's laptop (named in the
    devbox's /etc/hosts) holding a snapshot of IDEDAT, and it sits on the
    office Wi-Fi with a DHCP lease. Its address therefore MOVES - measured, it
    changed four times in five days, and twice the new lease came from a
    different /24 than the old one, so guessing the last octet will not find
    it.

    Worse, the office Wi-Fi is a /22 that OVERLAPS the range those leases
    come from, so a stale address does not fail cleanly:
    the laptop ARPs for it, some unrelated device answers, and you get
    "connection refused" or a silent nothing. A whole afternoon went into
    concluding "Sage is down" when Sage was up the whole time at another
    address.

THE RULE THIS IS BUILT AROUND

    An open port 1433 is NOT proof. There is a second SQL Server on this
    network that refuses the migration's read-only login. Pointing the
    migration at the wrong database would be far worse than not finding one,
    so a candidate is only accepted when it proves its identity:

        DB_NAME() == SQL_DATABASE          it is the right database, and
        ICITEMO HSNCODE rows >= FLOOR      it is the real item master, not an
                                           empty or partial restore

    Anything that merely listens on 1433 is rejected and reported.
"""
import argparse
import concurrent.futures
import ipaddress
import os
import re
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P                                     # noqa: E402

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env")

# ICITEMO carries 258,770 non-empty HSNCODE rows. A restore with far fewer is
# not the database this migration was reconciled against, so it is refused
# rather than silently used.
HSN_FLOOR = 200000
PORT_TIMEOUT = 1.5
MAX_WORKERS = 200


def local_networks():
    """-> the IPv4 networks this host is on, from `ip -o addr`.

    Derived rather than hardcoded: the Sage laptop's lease comes from whatever
    subnet the office Wi-Fi hands out, and that has already changed once.
    """
    nets = []
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:                                           # noqa: BLE001
        return nets
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
        try:
            net = ipaddress.ip_network(m.group(1), strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.num_addresses > 65536:
            continue
        nets.append(net)
    return nets


def port_open(host, port=1433, timeout=PORT_TIMEOUT):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:                                           # noqa: BLE001
        return False


def is_sage(host, verbose=False):
    """-> (True, detail) only if `host` proves it is the Sage IDEDAT box."""
    try:
        import pymssql
        cn = pymssql.connect(server=host, port=int(P.cfg("SQL_PORT", 1433)),
                             user=P.cfg("SQL_USER"),
                             password=P.cfg("SQL_PASSWORD"),
                             database=P.cfg("SQL_DATABASE"),
                             login_timeout=8, timeout=30)
    except Exception as exc:                                    # noqa: BLE001
        return False, "login refused (%s)" % str(exc)[:70]
    try:
        cur = cn.cursor()
        cur.execute("SELECT DB_NAME()")
        db = cur.fetchone()[0]
        want = P.cfg("SQL_DATABASE")
        if db != want:
            return False, "wrong database: %s (want %s)" % (db, want)
        cur.execute("SELECT COUNT(*) FROM ICITEMO "
                    "WHERE RTRIM(OPTFIELD)='HSNCODE' AND RTRIM(VALUE)<>''")
        n = cur.fetchone()[0]
        if n < HSN_FLOOR:
            return False, ("ICITEMO has only %d HSNCODE rows (floor %d) - "
                           "a partial restore, refusing" % (n, HSN_FLOOR))
        return True, "%s, ICITEMO %d HSNCODE rows" % (db, n)
    except Exception as exc:                                    # noqa: BLE001
        return False, "probe failed (%s)" % str(exc)[:70]
    finally:
        try:
            cn.close()
        except Exception:                                       # noqa: BLE001
            pass


def scan(nets, log):
    hosts = []
    for net in nets:
        hosts.extend(str(h) for h in net.hosts())
    log("  scanning %d addresses on %s"
        % (len(hosts), ", ".join(str(n) for n in nets)))
    open_hosts = []
    with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as ex:
        for host, ok in zip(hosts, ex.map(port_open, hosts)):
            if ok:
                open_hosts.append(host)
    log("  %d host(s) listening on 1433: %s"
        % (len(open_hosts), ", ".join(open_hosts) or "none"))
    return open_hosts


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Locate and verify the Sage IDEDAT host")
    ap.add_argument("--write", action="store_true",
                    help="rewrite SQL_HOST in .env with what is found")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the host address")
    a = ap.parse_args(argv)

    def log(msg):
        if not a.quiet:
            print(msg, flush=True)

    current = P.cfg("SQL_HOST")

    # Fast path: whatever .env says now is usually still right.
    if current:
        log("trying configured SQL_HOST %s" % current)
        if port_open(current):
            ok, detail = is_sage(current)
            if ok:
                log("  CONFIRMED: %s (%s)" % (current, detail))
                print(current if a.quiet else "\nSage is at %s" % current)
                return 0
            log("  listening, but not Sage: %s" % detail)
        else:
            log("  no answer on 1433")

    nets = local_networks()
    if not nets:
        print("could not determine the local network to scan", file=sys.stderr)
        return 3

    rejected = []
    for host in scan(nets, log):
        if host == current:
            continue
        ok, detail = is_sage(host)
        if ok:
            log("  CONFIRMED: %s (%s)" % (host, detail))
            if a.write:
                if not os.path.exists(ENV_PATH):
                    print("no .env at %s" % ENV_PATH, file=sys.stderr)
                    return 3
                with open(ENV_PATH) as fh:
                    txt = fh.read()
                new, n = re.subn(r"(?m)^SQL_HOST=.*$",
                                 "SQL_HOST=" + host, txt)
                if n:
                    with open(ENV_PATH, "w") as fh:
                        fh.write(new)
                    log("  .env updated: SQL_HOST=%s" % host)
                else:
                    log("  .env has no SQL_HOST line; not modified")
            print(host if a.quiet else "\nSage is at %s" % host)
            return 0
        rejected.append((host, detail))
        log("  %s rejected: %s" % (host, detail))

    print("\nNo host on this network proved it is the Sage %s database."
          % P.cfg("SQL_DATABASE"), file=sys.stderr)
    if rejected:
        print("Rejected candidates:", file=sys.stderr)
        for h, why in rejected:
            print("  %-16s %s" % (h, why), file=sys.stderr)
    print("The Sage laptop is probably off the network. It is a Wi-Fi DHCP\n"
          "host, so it only answers while its owner is online.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
