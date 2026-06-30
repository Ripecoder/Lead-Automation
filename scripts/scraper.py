# Line 1-12: imports
import os
import time
from dotenv import load_dotenv
import psycopg2

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tqdm import tqdm

# Line 15: load .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Line 20: connect to postgres
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# Line 25: start browser
driver = webdriver.Chrome()

# Line 28: open Google Maps
driver.get("https://www.google.com/maps")

# Line 31: get search query
search_query = input("Enter search query: ")

# Line 34-36: wait for search box
search_box = WebDriverWait(driver, 30).until(
    EC.presence_of_element_located((By.NAME, "q"))
)

# Line 40-41: search
search_box.send_keys(search_query)
search_box.send_keys(Keys.ENTER)

time.sleep(5)

print("\nURL:", driver.current_url)
print("TITLE:", driver.title)
input("Inspect browser and press Enter...")

# Line 47-50
results_feed = WebDriverWait(driver, 30).until(
    EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="feed"]'))
)

print("\n[OK] Results feed loaded\n")

# ---------------------------
# PHASE 1: SCROLL + LOAD
# ---------------------------

previous_count = 0
cards_total = 0

for i in tqdm(range(30), desc="Scrolling results"):
    
    driver.execute_script(
        "arguments[0].scrollTop = arguments[0].scrollHeight",
        results_feed
    )

    time.sleep(2)

    cards = driver.find_elements(By.CSS_SELECTOR, 'div[role="article"]')
    current_count = len(cards)

    cards_total = current_count

    if current_count == previous_count:
        break

    previous_count = current_count

print(f"\n[OK] Total cards loaded: {cards_total}\n")

# ---------------------------
# PHASE 2: COLLECT URLS
# ---------------------------

links = driver.find_elements(By.CSS_SELECTOR, "a.hfpxzc")

business_urls = []

for link in links:
    try:
        href = link.get_attribute("href")
        if href and href not in business_urls:
            business_urls.append(href)
    except:
        pass

print(f"[OK] Collected {len(business_urls)} URLs\n")

# ---------------------------
# PHASE 3: SCRAPE + INSERT
# ---------------------------

seen_names = set()

progress = tqdm(total=len(business_urls), desc="Scraping leads")

for i, url in enumerate(business_urls):

    try:
        driver.get(url)

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "h1"))
        )

        # ---------------- Name ----------------
        try:
            name = driver.find_element(By.TAG_NAME, "h1").text
        except:
            name = ""

        if name in seen_names:
            progress.update(1)
            continue

        seen_names.add(name)

        # ---------------- Phone ----------------
        try:
            phone = driver.find_element(
                By.XPATH,
                '//button[contains(@data-item-id,"phone")]'
            ).text
        except:
            phone = ""

        # ---------------- Website ----------------
        try:
            website = driver.find_element(
                By.XPATH,
                '//a[contains(@data-item-id,"authority")]'
            ).get_attribute("href")
        except:
            website = ""

        # ---------------- Address ----------------
        try:
            address = driver.find_element(
                By.XPATH,
                '//button[contains(@data-item-id,"address")]'
            ).text
        except:
            address = ""

        # ---------------- Rating ----------------
        try:
            rating = driver.find_element(
                By.CSS_SELECTOR,
                'div.F7nice span[aria-hidden="true"]'
            ).text
        except:
            rating = ""

        # ---------------- Reviews ----------------
        try:
            reviews = driver.find_elements(
                By.CSS_SELECTOR,
                'div.F7nice span'
            )[1].text
        except:
            reviews = ""

        # ---------------- DB INSERT ----------------
        cur.execute(
            """
            INSERT INTO business_leads (
                name,
                phone,
                website,
                address,
                rating,
                reviews
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (phone) DO NOTHING
            """,
            (name, phone, website, address, rating, reviews)
        )

        conn.commit()

        progress.set_postfix_str(name[:30])

    except Exception:
        pass

    progress.update(1)

progress.close()

# ---------------------------
# CLEANUP
# ---------------------------

cur.close()
conn.close()
driver.quit()

input("\nDone. Press Enter to exit...")