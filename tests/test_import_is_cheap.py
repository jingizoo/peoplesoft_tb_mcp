"""Importing the server must not touch the database.

A module-level `query_catalog.integration_endpoints()` put an Oracle round
trip on the import path. Every consumer of pstb.server paid it — the MCP
server, the GUI, the probe, the eval harness — and a failure there was
fatal to the whole process rather than to one tool.

On a real instance it was fatal: the IB catalog's gateway column is not
named the same across tools releases, the unguarded SELECT raised
ORA-00904, the MCP subprocess never finished starting, and the browser
showed "MCP error: connection closed" behind a splash screen that never
cleared. A guard on the column alone would have fixed that one query and
left the next one to find.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import sys
sys.path.insert(0, %r)
import pstb.db
queries = []
real = pstb.db.Database.query
pstb.db.Database.query = lambda self, sql, params=None, **kw: (
    queries.append(sql) or real(self, sql, params, **kw))
import pstb.server            # the whole point
print("QUERIES:%%d" %% len(queries))
for q in queries[:5]:
    print("  " + " ".join(q.split())[:110])
""" % str(ROOT)


class ImportTests(unittest.TestCase):
    def test_importing_the_server_runs_no_queries(self) -> None:
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        line = next((l for l in out.stdout.splitlines()
                     if l.startswith("QUERIES:")), "")
        self.assertTrue(line, f"probe produced no count:\n{out.stdout}")
        count = int(line.split(":")[1])
        self.assertEqual(count, 0,
                         "import performed database I/O:\n" + out.stdout)

    def test_the_qas_handle_resolves_lazily(self) -> None:
        sys.path.insert(0, str(ROOT))
        from pstb import server
        self.assertIsNone(server.qas._real,
                          "the QAS gateway resolved at import; it must wait "
                          "for the first call that needs it")

    def test_gateway_discovery_failing_is_not_fatal(self) -> None:
        sys.path.insert(0, str(ROOT))
        from pstb import server
        from pstb.db import DbError

        lazy = type(server.qas)()
        broken = lambda: (_ for _ in ()).throw(  # noqa: E731
            DbError("ORA-00904: \"TARGETLOCATION\": invalid identifier"))
        original = server.query_catalog.integration_endpoints
        server.query_catalog.integration_endpoints = broken
        try:
            self.assertIsNotNone(lazy._resolve(),
                                 "a site whose IB catalog cannot be read "
                                 "must still get a QAS handle — discovery "
                                 "is optional, the tool is not")
        finally:
            server.query_catalog.integration_endpoints = original


if __name__ == "__main__":
    unittest.main()
