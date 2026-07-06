#!/usr/bin/env python3
"""
Google Maps Lead Scraper
=========================

A production-grade CLI tool that scrapes business leads from Google Maps
(name, phone, website, address, rating, review count) and persists them
into a PostgreSQL table.

Features:
    - Rich-powered CLI UI (banner, spinners, progress bar, live stats, summary table)
    - Batched DB commits for performance
    - Auto-detection of end-of-scroll
    - Retry-once logic for failed business pages
    - Safe, validated SQL identifiers (table name)
    - Defensive Selenium waits with minimal sleep() usage

Usage:
    python scraper.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import psycopg2
from dotenv import load_dotenv

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

GOOGLE_MAPS_URL = "https://www.google.com/maps"
EXPLICIT_WAIT_SECONDS = 20
POST_SEARCH_SETTLE_SECONDS = 4
SCROLL_PAUSE_SECONDS = 1.5
MAX_SCROLL_ATTEMPTS = 50
SCROLL_STABLE_ROUNDS_REQUIRED = 2  # consecutive rounds w/ no new cards = end of list
DB_COMMIT_BATCH_SIZE = 10

RESULT_CARD_SELECTOR = "div[role='article']"
RESULTS_FEED_SELECTOR = 'div[role="feed"]'
RATING_CONTAINER_SELECTOR = "div.F7nice span[aria-hidden='true']"
REVIEWS_CONTAINER_SELECTOR = "div.F7nice span"

PHONE_BUTTON_XPATH = '//button[contains(@data-item-id,"phone")]'
WEBSITE_LINK_XPATH = '//a[contains(@data-item-id,"authority")]'
ADDRESS_BUTTON_XPATH = '//button[contains(@data-item-id,"address")]'

console = Console()


# ---------------------------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------------------------

@dataclass
class Business:
    """A single scraped business record."""

    name: str
    phone: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None


@dataclass
class ScrapeStats:
    """Running statistics for the scrape session, shown live and in the final summary."""

    found: int = 0
    saved: int = 0
    duplicates: int = 0
    failed: int = 0
    processed: int = 0
    failed_reasons: list[str] = field(default_factory=list)

    def record_failure(self, url: str, reason: str) -> None:
        self.failed += 1
        self.failed_reasons.append(f"{url} -> {reason}")


# ---------------------------------------------------------------------------
# ENVIRONMENT / VALIDATION
# ---------------------------------------------------------------------------

def load_database_url() -> str:
    """Load and validate the DATABASE_URL environment variable."""
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        console.print("[bold red]✗ DATABASE_URL missing in environment (.env)[/bold red]")
        raise SystemExit(1)
    return database_url


def sanitize_table_name(raw_name: str) -> str:
    """Lowercase and strip any character that isn't a-z, 0-9, or underscore."""
    cleaned = re.sub(r"[^a-z0-9_]", "", raw_name.strip().lower())
    return cleaned


def prompt_for_inputs() -> tuple[str, str]:
    """Prompt the user for a search query and a valid table name."""
    console.print()
    search_query = console.input("[bold cyan]🔎 Enter search query:[/bold cyan] ").strip()
    while not search_query:
        console.print("[yellow]⚠ Search query cannot be empty.[/yellow]")
        search_query = console.input("[bold cyan]🔎 Enter search query:[/bold cyan] ").strip()

    while True:
        raw_table_name = console.input("[bold cyan]🗄  Table name:[/bold cyan] ").strip()
        table_name = sanitize_table_name(raw_table_name)
        if table_name:
            break
        console.print("[yellow]⚠ Table name must contain at least one letter, digit, or underscore.[/yellow]")

    return search_query, table_name


# ---------------------------------------------------------------------------
# UI HELPERS
# ---------------------------------------------------------------------------

def print_banner() -> None:
    banner_text = Text()
    banner_text.append("  MAPS LEAD SCRAPER\n", style="bold white")
    banner_text.append("  Extract business leads from Google Maps into PostgreSQL", style="dim")
    console.print(Panel(banner_text, border_style="cyan", expand=False, padding=(1, 4)))


def print_summary(stats: ScrapeStats, table_name: str, elapsed_seconds: float) -> None:
    summary = Table(title="Scrape Summary", show_header=True, header_style="bold cyan", border_style="cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")

    summary.add_row("Businesses Found", str(stats.found))
    summary.add_row("Businesses Saved", f"[green]{stats.saved}[/green]")
    summary.add_row("Duplicates", f"[yellow]{stats.duplicates}[/yellow]")
    summary.add_row("Failed", f"[red]{stats.failed}[/red]")
    summary.add_row("Execution Time", f"{elapsed_seconds:.1f}s")
    summary.add_row("Database Table", table_name)

    console.print()
    console.print(summary)

    if stats.failed_reasons:
        console.print()
        console.print("[bold red]Failure details:[/bold red]")
        for reason in stats.failed_reasons[:20]:
            console.print(f"  [red]✗[/red] {reason}")
        if len(stats.failed_reasons) > 20:
            console.print(f"  [dim]...and {len(stats.failed_reasons) - 20} more[/dim]")


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def connect_database(database_url: str):
    """Open a PostgreSQL connection and cursor."""
    try:
        conn = psycopg2.connect(database_url)
        return conn, conn.cursor()
    except psycopg2.OperationalError as exc:
        console.print(f"[bold red]✗ Could not connect to database: {exc}[/bold red]")
        raise SystemExit(1)


def ensure_table_exists(cur, table_name: str) -> None:
    """Create the destination table if it doesn't already exist.

    table_name has already been sanitized to [a-z0-9_]+ by sanitize_table_name,
    so it is safe to interpolate directly into the DDL/DML statements.
    """
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


def insert_business(cur, table_name: str, business: Business) -> bool:
    """Insert a business row. Returns True if a new row was inserted, False if it was a duplicate."""
    cur.execute(
        f"""
        INSERT INTO {table_name} (name, phone, website, address, rating, reviews)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (phone) DO NOTHING
        RETURNING id;
        """,
        (business.name, business.phone, business.website, business.address, business.rating, business.reviews),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# SELENIUM SETUP
# ---------------------------------------------------------------------------

def create_driver() -> webdriver.Chrome:
    """Launch a Chrome WebDriver instance."""
    return webdriver.Chrome()


def run_search(driver: webdriver.Chrome, wait: WebDriverWait, search_query: str) -> None:
    """Navigate to Google Maps and submit the search query."""
    driver.get(GOOGLE_MAPS_URL)
    search_box = wait.until(EC.presence_of_element_located((By.NAME, "q")))
    search_box.send_keys(search_query)
    search_box.send_keys(Keys.ENTER)

    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, RESULTS_FEED_SELECTOR)))
    time.sleep(POST_SEARCH_SETTLE_SECONDS)


def scroll_results_to_end(driver: webdriver.Chrome, wait: WebDriverWait) -> list:
    """Scroll the results feed until no new cards load, then return all found cards."""
    feed = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, RESULTS_FEED_SELECTOR)))

    stable_rounds = 0
    last_count = 0

    with console.status("[bold cyan]Scrolling results feed...[/bold cyan]") as status:
        for attempt in range(1, MAX_SCROLL_ATTEMPTS + 1):
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", feed)
            time.sleep(SCROLL_PAUSE_SECONDS)

            cards = driver.find_elements(By.CSS_SELECTOR, RESULT_CARD_SELECTOR)
            status.update(f"[bold cyan]Scrolling results feed...[/bold cyan] ({len(cards)} cards, pass {attempt})")

            if len(cards) == last_count:
                stable_rounds += 1
                if stable_rounds >= SCROLL_STABLE_ROUNDS_REQUIRED:
                    break
            else:
                stable_rounds = 0
                last_count = len(cards)

    cards = driver.find_elements(By.CSS_SELECTOR, RESULT_CARD_SELECTOR)
    console.print(f"[green]✓[/green] Found [bold]{len(cards)}[/bold] result cards")
    return cards


def collect_result_urls(cards: list) -> list[str]:
    """Extract unique business detail URLs from result cards."""
    urls: list[str] = []
    seen_urls = set()

    for card in cards:
        try:
            link = card.find_element(By.CSS_SELECTOR, "a")
            href = link.get_attribute("href")
        except NoSuchElementException:
            continue

        if href and href not in seen_urls:
            seen_urls.add(href)
            urls.append(href)

    console.print(f"[green]✓[/green] Collected [bold]{len(urls)}[/bold] unique business URLs")
    return urls


# ---------------------------------------------------------------------------
# FIELD EXTRACTION
# ---------------------------------------------------------------------------

def extract_text_by_xpath(driver: webdriver.Chrome, xpath: str) -> Optional[str]:
    try:
        return driver.find_element(By.XPATH, xpath).text.strip() or None
    except NoSuchElementException:
        return None


def extract_website(driver: webdriver.Chrome) -> Optional[str]:
    try:
        return driver.find_element(By.XPATH, WEBSITE_LINK_XPATH).get_attribute("href")
    except NoSuchElementException:
        return None


def extract_rating(driver: webdriver.Chrome) -> Optional[float]:
    try:
        raw = driver.find_element(By.CSS_SELECTOR, RATING_CONTAINER_SELECTOR).text
        return float(raw)
    except (NoSuchElementException, ValueError):
        return None


def extract_reviews(driver: webdriver.Chrome) -> Optional[int]:
    try:
        raw = driver.find_elements(By.CSS_SELECTOR, REVIEWS_CONTAINER_SELECTOR)[1].text
        digits = re.sub(r"\D", "", raw)
        return int(digits) if digits else None
    except (NoSuchElementException, IndexError, ValueError):
        return None


def extract_business(driver: webdriver.Chrome, wait: WebDriverWait) -> Optional[Business]:
    """Extract a single business's details from its currently-loaded detail page."""
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "h1")))

    name = driver.find_element(By.TAG_NAME, "h1").text.strip()
    if not name:
        return None

    return Business(
        name=name,
        phone=extract_text_by_xpath(driver, PHONE_BUTTON_XPATH),
        website=extract_website(driver),
        address=extract_text_by_xpath(driver, ADDRESS_BUTTON_XPATH),
        rating=extract_rating(driver),
        reviews=extract_reviews(driver),
    )


def visit_and_extract(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    url: str,
    retry_on_failure: bool = True,
) -> Business:
    """Visit a business URL and extract its details, retrying once on failure."""
    try:
        driver.get(url)
        business = extract_business(driver, wait)
        if business is None:
            raise ValueError("Business name was empty")
        return business
    except (TimeoutException, NoSuchElementException, WebDriverException, ValueError) as exc:
        if retry_on_failure:
            time.sleep(1.5)
            return visit_and_extract(driver, wait, url, retry_on_failure=False)
        raise exc


# ---------------------------------------------------------------------------
# MAIN SCRAPE LOOP
# ---------------------------------------------------------------------------

def scrape_businesses(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    urls: list[str],
    cur,
    conn,
    table_name: str,
) -> ScrapeStats:
    """Visit each business URL, extract details, and persist to the database in batches."""
    stats = ScrapeStats(found=len(urls))
    seen_names: set[str] = set()
    uncommitted_inserts = 0

    progress_columns = (
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )

    with Progress(*progress_columns, console=console) as progress:
        task = progress.add_task("Scraping businesses", total=len(urls))

        for url in urls:
            stats.processed += 1

            try:
                business = visit_and_extract(driver, wait, url)
            except Exception as exc:  # noqa: BLE001 - keep scraping regardless of failure cause
                stats.record_failure(url, str(exc))
                console.print(f"[red]✗ Failed[/red] {url} [dim]({exc})[/dim]")
                progress.advance(task)
                continue

            if business.name in seen_names:
                stats.duplicates += 1
                console.print(f"[yellow]⚠ Duplicate[/yellow] {business.name}")
                progress.advance(task)
                continue

            seen_names.add(business.name)

            try:
                inserted = insert_business(cur, table_name, business)
            except psycopg2.Error as exc:
                conn.rollback()
                stats.record_failure(url, f"DB error: {exc}")
                console.print(f"[red]✗ Failed[/red] {business.name} [dim](DB error: {exc})[/dim]")
                progress.advance(task)
                continue

            if inserted:
                stats.saved += 1
                uncommitted_inserts += 1
                console.print(f"[green]✓ Saved[/green] {business.name}")
            else:
                stats.duplicates += 1
                console.print(f"[yellow]⚠ Duplicate[/yellow] {business.name} [dim](phone already exists)[/dim]")

            if uncommitted_inserts >= DB_COMMIT_BATCH_SIZE:
                conn.commit()
                uncommitted_inserts = 0

            progress.advance(task)

    if uncommitted_inserts > 0:
        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main() -> None:
    print_banner()

    database_url = load_database_url()
    search_query, table_name = prompt_for_inputs()

    conn, cur = connect_database(database_url)
    driver: Optional[webdriver.Chrome] = None

    start_time = time.time()
    stats = ScrapeStats()

    try:
        ensure_table_exists(cur, table_name)
        conn.commit()

        console.print()
        with console.status("[bold cyan]Launching Chrome...[/bold cyan]"):
            driver = create_driver()
            wait = WebDriverWait(driver, EXPLICIT_WAIT_SECONDS)

        with console.status(f"[bold cyan]Searching Google Maps for '{search_query}'...[/bold cyan]"):
            run_search(driver, wait, search_query)

        cards = scroll_results_to_end(driver, wait)
        urls = collect_result_urls(cards)

        if not urls:
            console.print("[yellow]⚠ No results found. Exiting.[/yellow]")
            return

        stats = scrape_businesses(driver, wait, urls, cur, conn, table_name)

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Interrupted by user. Saving progress and shutting down...[/yellow]")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        console.print(f"[bold red]✗ Unexpected error: {exc}[/bold red]")
        conn.rollback()
    finally:
        elapsed_seconds = time.time() - start_time

        try:
            cur.close()
            conn.close()
        except Exception:
            pass

        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

        print_summary(stats, table_name, elapsed_seconds)
        console.print("\n[bold green]✓ DONE[/bold green]\n")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        console.print(f"[bold red]✗ Fatal error: {exc}[/bold red]")
        sys.exit(1)