import os
import time
import re
from dotenv import load_dotenv
import psycopg2

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ---------------------------
# ENV SAFE LOAD
# ---------------------------
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL missing in environment")

# ---------------------------
# DB
# ---------------------------
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# ---------------------------
# SETUP
# ---------------------------
driver = webdriver.Chrome()
wait = WebDriverWait(driver, 20)

driver.get("https://www.google.com/maps")

search_query = input("Enter search query: ")

table_name = input("Table name: ").strip().lower()
table_name = re.sub(r"[^a-z0-9_]", "", table_name)

# ---------------------------
# TABLE
# ---------------------------
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

# ---------------------------
# SEARCH
# ---------------------------
search_box = wait.until(EC.presence_of_element_located((By.NAME, "q")))
search_box.send_keys(search_query)
search_box.send_keys(Keys.ENTER)

time.sleep(5)

# ---------------------------
# RESULTS PANEL
# ---------------------------
feed = wait.until(
    EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="feed"]'))
)

# ---------------------------
# SMART SCROLL (until stable)
# ---------------------------
last_count = 0

for _ in range(50):
    driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", feed)
    time.sleep(2)

    cards = driver.find_elements(By.CSS_SELECTOR, "div[role='article']")
    if len(cards) == last_count:
        break
    last_count = len(cards)

print(f"Found cards: {last_count}")

# ---------------------------
# GET RESULTS (IMPORTANT FIX)
# ---------------------------
cards = driver.find_elements(By.CSS_SELECTOR, "div[role='article']")

urls = []
for c in cards:
    try:
        a = c.find_element(By.CSS_SELECTOR, "a")
        href = a.get_attribute("href")
        if href and href not in urls:
            urls.append(href)
    except:
        continue

print("URLs:", len(urls))

# ---------------------------
# SCRAPE
# ---------------------------
seen = set()

for url in urls:
    try:
        driver.get(url)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "h1")))

        name = driver.find_element(By.TAG_NAME, "h1").text.strip()
        if not name or name in seen:
            continue
        seen.add(name)

        # phone
        try:
            phone = driver.find_element(
                By.XPATH,
                '//button[contains(@data-item-id,"phone")]'
            ).text
        except:
            phone = None

        # website
        try:
            website = driver.find_element(
                By.XPATH,
                '//a[contains(@data-item-id,"authority")]'
            ).get_attribute("href")
        except:
            website = None

        # address
        try:
            address = driver.find_element(
                By.XPATH,
                '//button[contains(@data-item-id,"address")]'
            ).text
        except:
            address = None

        # rating
        try:
            rating = float(driver.find_element(
                By.CSS_SELECTOR,
                'div.F7nice span[aria-hidden="true"]'
            ).text)
        except:
            rating = None

        # reviews
        try:
            reviews_text = driver.find_elements(By.CSS_SELECTOR, 'div.F7nice span')[1].text
            reviews = int(re.sub(r"\D", "", reviews_text))
        except:
            reviews = None

        # insert
        cur.execute(f"""
            INSERT INTO {table_name}
            (name, phone, website, address, rating, reviews)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (phone) DO NOTHING
        """, (name, phone, website, address, rating, reviews))

        conn.commit()

        print("Saved:", name)

    except Exception as e:
        continue


cur.close()
conn.close()
driver.quit()

print("DONE")