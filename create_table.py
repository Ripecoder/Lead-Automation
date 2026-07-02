import os
import re
import psycopg2
from dotenv import load_dotenv

# -------------------------
# LOAD ENV
# -------------------------
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise Exception("DATABASE_URL not found in .env")

# -------------------------
# DB CONNECT
# -------------------------
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# -------------------------
# INPUT TABLE NAME
# -------------------------
print("\n=== TABLE CREATOR ===\n")

table_name = input("Enter new table name (e.g. dentists_mumbai): ").strip().lower()

# sanitize (VERY IMPORTANT)
table_name = re.sub(r"[^a-z0-9_]", "", table_name)

if not table_name:
    raise Exception("Invalid table name")

# -------------------------
# CREATE TABLE
# -------------------------
cur.execute(f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    id SERIAL PRIMARY KEY,
    name TEXT,
    phone TEXT UNIQUE,
    website TEXT,
    address TEXT,
    rating NUMERIC,
    reviews INTEGER
);
""")

conn.commit()

print(f"\nTable '{table_name}' is ready.")

# -------------------------
# CLEANUP
# -------------------------
cur.close()
conn.close()