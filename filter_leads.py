import os
import psycopg2
from dotenv import load_dotenv
from openpyxl import Workbook

# -------------------------
# LOAD ENV
# -------------------------
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

MAX_EXPORT = 1000

# -------------------------
# DB CONNECTION
# -------------------------
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# -------------------------
# INPUT FILTERS
# -------------------------
print("\n=== LEAD FILTER SYSTEM ===\n")

table_name = input("Which table? (e.g. dentists_mumbai): ").strip().lower()

website_filter = input("Website? (1=Has, 2=No, 3=Ignore): ").strip()

min_rating = input("Min rating: ").strip()
max_rating = input("Max rating: ").strip()

min_reviews = input("Min reviews: ").strip()
max_reviews = input("Max reviews: ").strip()

city = input("City filter (optional): ").strip()

output_file = input("Output Excel filename (without extension): ").strip()

if not output_file:
    output_file = "filtered_leads"

# -------------------------
# BUILD QUERY SAFELY
# -------------------------

query = f"""
SELECT name, phone, address, rating, reviews, website
FROM {table_name}
WHERE 1=1
"""

params = []

# Website filter
if website_filter == "1":
    query += " AND website IS NOT NULL"
elif website_filter == "2":
    query += " AND website IS NULL"

# Rating filter
if min_rating:
    query += " AND rating >= %s"
    params.append(float(min_rating))

if max_rating:
    query += " AND rating <= %s"
    params.append(float(max_rating))

# Reviews filter
if min_reviews:
    query += " AND reviews >= %s"
    params.append(int(min_reviews))

if max_reviews:
    query += " AND reviews <= %s"
    params.append(int(max_reviews))

# City filter
if city:
    query += " AND address ILIKE %s"
    params.append(f"%{city}%")

query += " ORDER BY reviews DESC"

# -------------------------
# EXECUTE QUERY
# -------------------------

cur.execute(query, params)
rows = cur.fetchall()

print(f"\nMatched leads: {len(rows)}")

if not rows:
    print("No results found.")
    cur.close()
    conn.close()
    exit()

# Safety cap
if len(rows) > MAX_EXPORT:
    print(f"⚠ Capping output to {MAX_EXPORT}")
    rows = rows[:MAX_EXPORT]

cur.close()
conn.close()

# -------------------------
# EXPORT TO EXCEL (.xlsx)
# -------------------------

wb = Workbook()
ws = wb.active
ws.title = "Filtered Leads"

# Header
ws.append([
    "Business Name",
    "Phone",
    "Address",
    "Rating",
    "Reviews",
    "Website"
])

# Data
for row in rows:
    ws.append(row)

# Optional: Auto-size columns
for column in ws.columns:
    max_length = 0
    column_letter = column[0].column_letter

    for cell in column:
        try:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        except Exception:
            pass

    ws.column_dimensions[column_letter].width = min(max_length + 2, 60)

filename = f"{output_file}.xlsx"
wb.save(filename)

print(f"\n✅ Exported {len(rows)} leads to '{filename}'")