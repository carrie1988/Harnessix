"""以原500 Thread重启负载观察SQLite页与对象聚合，不读取或导出业务正文。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from scripts import soak_restart

ROOT = Path('/private/tmp/harnessix-restart-root-20260929')
observations = []
original = soak_restart._file_watermarks


def inspect_storage(state, *, require_complete=False):
    result = original(state, require_complete=require_complete)
    if not require_complete:
        return result
    database = state / 'sessions.db'
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
        rows = []
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            count = connection.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]
            rows.append({'table': name, 'row_count': count})
        facts = {'page_count': connection.execute('PRAGMA page_count').fetchone()[0],
                 'page_size': connection.execute('PRAGMA page_size').fetchone()[0],
                 'freelist_count': connection.execute('PRAGMA freelist_count').fetchone()[0],
                 'schema_version': connection.execute('PRAGMA user_version').fetchone()[0],
                 'table_counts': rows}
        try:
            facts['object_pages'] = [
                {'object': name, 'pages': count, 'page_bytes': size, 'payload_bytes': payload, 'unused_bytes': unused}
                for name,count,size,payload,unused in connection.execute(
                    'SELECT name,COUNT(*),SUM(pgsize),SUM(payload),SUM(unused) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC'
                )
            ]
        except sqlite3.OperationalError:
            facts['dbstat_available'] = False
        observations.append(facts)
    finally:
        connection.close()
    return result


async def main():
    soak_restart._file_watermarks = inspect_storage
    directory,manifest = await soak_restart.run_product_restart(
        ROOT/'session-storage-diagnostic', code_revision='1bc3794bfbdb9ce5fa58103d372c68de4401f90a',
        thread_count=500,warmup_count=1,measured_restarts=3,timeout_seconds=120,
    )
    result = {'spec_version':'harnessix.session-storage-diagnostic/v1',
              'source_revision':manifest.code_revision,'run_id':manifest.run_id,
              'diagnostic_only':True,'observer':'read-only SQLite object/page aggregate after all original cycles',
              'provider_requests':manifest.provider.request_count,'observations':observations}
    (ROOT/'session-storage-observations.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    asyncio.run(main())
