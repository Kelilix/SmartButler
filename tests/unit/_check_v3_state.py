import sqlite3, json
from pathlib import Path

con = sqlite3.connect(r'D:\soft\0project\test\SmartButler\data\task_index.db')
cur = con.cursor()

print("=== tables ===")
for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(row[0])

print()
print("=== tasks schema ===")
for row in cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"):
    print(row[0])

print()
print("=== all tasks ===")
for row in cur.execute("SELECT * FROM tasks"):
    print(row)

print()
print("=== memory_records count ===")
try:
    for row in cur.execute("SELECT task_id, status, COUNT(*) FROM memory_records GROUP BY task_id"):
        print(row)
except Exception as e:
    print(e)

print()
print("=== qdrant meta ===")
mp = Path(r'D:\soft\0project\test\SmartButler\data\qdrant_long_term\meta.json')
print("exists:", mp.exists())
if mp.exists():
    print(mp.read_text())

con.close()
