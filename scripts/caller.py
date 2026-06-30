"""
NEXULITH CALLER TERMINAL
Cross-platform cold-call CRM. PC + Termux + any terminal.
"""

import os
import sys
import uuid
import time
import threading
from datetime import datetime, date, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.live import Live
from rich.align import Align
from rich.rule import Rule
from rich import box
import readchar
import pyfiglet

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

SESSION_ID  = uuid.uuid4()
SESSION_HEX = SESSION_ID.hex[:8].upper()
console     = Console()

_conn            = None
_cached_new      = "?"
_cached_followup = "?"

stats = {
    "start":      datetime.now(),
    "completed":  0,
    "interested": 0,
    "rejected":   0,
    "no_answer":  0,
    "demo":       0,
}

_last_action = ""
current_lead = None
_live: Live  = None


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def connect_db():
    global _conn
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in .env")
    _conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    _conn.autocommit = False


def db():
    global _conn
    try:
        if _conn is None or _conn.closed:
            connect_db()
        with _conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        connect_db()
    return _conn


def unlock_abandoned_sessions():
    try:
        c = db()
        with c.cursor() as cur:
            cur.execute("UPDATE business_leads SET locked=false, session_id=NULL WHERE locked=true")
        c.commit()
    except Exception as e:
        _fatal_db_error("unlock_abandoned_sessions", e)


def refresh_counts():
    global _cached_new, _cached_followup
    try:
        c = db()
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM business_leads WHERE contacted=false AND locked=false")
            _cached_new = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM business_leads "
                "WHERE followup_needed=true AND next_followup<=NOW() AND locked=false"
            )
            _cached_followup = cur.fetchone()["n"]
        c.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# LEAD MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def get_next_lead():
    try:
        c = db()
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM business_leads
                WHERE locked=false AND contacted=false
                ORDER BY created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT * FROM business_leads
                    WHERE locked=false AND followup_needed=true AND next_followup<=NOW()
                    ORDER BY next_followup ASC LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
            if row is None:
                c.commit()
                return None
            cur.execute(
                "UPDATE business_leads SET locked=true, session_id=%s WHERE id=%s",
                (str(SESSION_ID), row["id"]),
            )
        c.commit()
        return dict(row)
    except Exception as e:
        show_db_error("get_next_lead", e)
        return None


def _update_lead(lead_id, fields: dict):
    fields["updated_at"] = datetime.now()
    fields["locked"]     = False
    fields["session_id"] = None
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    vals = list(fields.values()) + [lead_id]
    try:
        c = db()
        with c.cursor() as cur:
            cur.execute(f"UPDATE business_leads SET {set_clause} WHERE id=%s", vals)
        c.commit()
    except Exception as e:
        show_db_error("_update_lead", e)


# ─────────────────────────────────────────────────────────────────────────────
# OUTCOME HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def mark_no_answer(lead):
    _update_lead(lead["id"], {
        "contacted":         True,
        "call_result":       "no_answer",
        "followup_needed":   True,
        "followup_count":    (lead.get("followup_count") or 0) + 1,
        "call_attempts":     (lead.get("call_attempts") or 0) + 1,
        "last_contacted_at": datetime.now(),
        "next_followup":     datetime.now() + timedelta(days=1),
    })
    stats["completed"] += 1
    stats["no_answer"] += 1
    set_action("[yellow]>> NO ANSWER — follow-up queued in 1 day[/yellow]")


def mark_positive(lead, days: int):
    _update_lead(lead["id"], {
        "contacted":         True,
        "call_result":       "positive",
        "followup_needed":   True,
        "followup_count":    (lead.get("followup_count") or 0) + 1,
        "call_attempts":     (lead.get("call_attempts") or 0) + 1,
        "last_contacted_at": datetime.now(),
        "next_followup":     datetime.now() + timedelta(days=days),
    })
    stats["completed"] += 1
    stats["interested"] += 1
    set_action(f"[green]>> INTERESTED — follow-up in {days}d[/green]")


def mark_negative(lead):
    _update_lead(lead["id"], {
        "contacted":         True,
        "call_result":       "negative",
        "followup_needed":   False,
        "call_attempts":     (lead.get("call_attempts") or 0) + 1,
        "last_contacted_at": datetime.now(),
    })
    stats["completed"] += 1
    stats["rejected"] += 1
    set_action("[red]>> REJECTED — lead closed[/red]")


def mark_demo(lead, days: int):
    demo_dt = date.today() + timedelta(days=days)
    _update_lead(lead["id"], {
        "contacted":         True,
        "call_result":       "demo_booked",
        "followup_needed":   True,
        "followup_count":    (lead.get("followup_count") or 0) + 1,
        "call_attempts":     (lead.get("call_attempts") or 0) + 1,
        "last_contacted_at": datetime.now(),
        "demo_date":         demo_dt,
        "next_followup":     datetime.combine(demo_dt, datetime.min.time()),
    })
    stats["completed"] += 1
    stats["demo"] += 1
    set_action(f"[cyan]>> DEMO LOCKED — {demo_dt.strftime('%d %b %Y')}[/cyan]")


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────

def set_action(msg: str):
    global _last_action
    _last_action = msg


def elapsed_str():
    d = datetime.now() - stats["start"]
    h, r = divmod(int(d.total_seconds()), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cpm_str():
    secs = (datetime.now() - stats["start"]).total_seconds()
    return "0.00" if secs < 1 else f"{stats['completed'] / (secs / 60):.2f}"


def build_screen(lead) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=7),
        Layout(name="stats",   size=3),
        Layout(name="lead",    size=13),
        Layout(name="hotkeys", size=7),
        Layout(name="status",  size=1),
    )

    # ── HEADER ───────────────────────────────────────────────────────────────
    banner = pyfiglet.figlet_format("NEXULITH", font="doom").rstrip()
    header_text = Text()
    for line in banner.split("\n"):
        header_text.append(line + "\n", style="bold cyan")
    header_text.append(
        f"  CALLER TERMINAL  //  SESSION {SESSION_HEX}  //  {datetime.now().strftime('%H:%M:%S')}",
        style="dim cyan"
    )
    layout["header"].update(header_text)

    # ── STATS BAR ────────────────────────────────────────────────────────────
    sg = Table.grid(padding=(0, 3), expand=True)
    for _ in range(7):
        sg.add_column(ratio=1)
    sg.add_row(
        f"[dim]ELAPSED[/dim] [bold white]{elapsed_str()}[/bold white]",
        f"[dim]DONE[/dim] [bold green]{stats['completed']}[/bold green]",
        f"[dim]CPM[/dim] [bold white]{cpm_str()}[/bold white]",
        f"[dim]INTERESTED[/dim] [green]{stats['interested']}[/green]",
        f"[dim]NO ANS[/dim] [yellow]{stats['no_answer']}[/yellow]",
        f"[dim]REJECTED[/dim] [red]{stats['rejected']}[/red]",
        f"[dim]QUEUE[/dim] [yellow]{_cached_new}[/yellow][dim]n[/dim] [cyan]{_cached_followup}[/cyan][dim]f[/dim]",
    )
    layout["stats"].update(Panel(sg, border_style="dim", box=box.HORIZONTALS, padding=(0, 1)))

    # ── LEAD CARD ────────────────────────────────────────────────────────────
    fc  = lead.get("followup_count") or 0
    att = lead.get("call_attempts") or 0
    tag = (
        "[bold green on dark_green]  NEW  [/bold green on dark_green]"
        if not lead.get("contacted") else
        f"[bold cyan on dark_cyan]  FOLLOW-UP #{fc}  [/bold cyan on dark_cyan]"
    )
    attempts_tag = f"[dim]  {att} prev attempt{'s' if att != 1 else ''}[/dim]" if att else ""

    lt = Table.grid(padding=(0, 3))
    lt.add_column(style="dim", min_width=11)
    lt.add_column(min_width=44)

    def lr(label, val, style="bold white"):
        lt.add_row(f"[dim]{label}[/dim]", f"[{style}]{val}[/{style}]" if val else "[dim]—[/dim]")

    lr("COMPANY",  lead.get("name"),    "bold green")
    lr("PHONE",    lead.get("phone"),   "bold yellow")
    lr("WEBSITE",  lead.get("website"), "cyan")
    lr("ADDRESS",  lead.get("address"), "white")

    rating_raw = lead.get("rating")
    if rating_raw:
        try:
            stars = "█" * round(float(rating_raw)) + "░" * (5 - round(float(rating_raw)))
            lr("RATING", f"{stars}  {rating_raw}", "yellow")
        except Exception:
            lr("RATING", rating_raw, "yellow")
    else:
        lr("RATING", None)

    lr("REVIEWS",  lead.get("reviews"), "dim white")
    if lead.get("notes"):
        lr("NOTES",   lead.get("notes"),   "dim white")
    if lead.get("next_followup"):
        lr("NEXT F/U", str(lead["next_followup"])[:16], "cyan")

    layout["lead"].update(Panel(
        lt,
        title=f" LEAD #{lead['id']}  {tag}{attempts_tag} ",
        border_style="cyan",
        box=box.HEAVY,
        padding=(0, 2),
    ))

    # ── HOTKEYS ──────────────────────────────────────────────────────────────
    hk = Table.grid(padding=(0, 3), expand=True)
    for _ in range(6):
        hk.add_column(ratio=1)
    hk.add_row(
        "[bold red on grey11]  Q  [/bold red on grey11]",      "[red] REJECTED[/red]",
        "[bold yellow on grey11]  A  [/bold yellow on grey11]","[yellow] NO ANSWER[/yellow]",
        "[bold green on grey11]  F  [/bold green on grey11]",  "[green] INTERESTED[/green]",
    )
    hk.add_row("","","","","","")
    hk.add_row(
        "[bold cyan on grey11]  D  [/bold cyan on grey11]",    "[cyan] DEMO BOOKED[/cyan]",
        "[bold white on grey11]  S  [/bold white on grey11]",  "[white] SUMMARY[/white]",
        "[bold dim on grey11]  ESC  [/bold dim on grey11]",    "[dim] END SESSION[/dim]",
    )
    layout["hotkeys"].update(Panel(
        Align(hk, align="left", vertical="middle"),
        border_style="dim",
        box=box.SIMPLE_HEAVY,
        padding=(0, 2),
    ))

    # ── STATUS LINE ───────────────────────────────────────────────────────────
    status = _last_action or "[dim]// standing by —[/dim]"
    layout["status"].update(Align(Text.from_markup(status), align="left"))

    return layout


def show_db_error(context: str, exc: Exception):
    if _live:
        _live.stop()
    console.print(Panel(
        f"[bold red]DB ERROR[/bold red]  [dim]{context}[/dim]\n[yellow]{exc}[/yellow]",
        border_style="red", box=box.HEAVY,
    ))


def _fatal_db_error(context: str, exc: Exception):
    show_db_error(context, exc)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def show_summary(return_after=True):
    global _live
    refresh_counts()
    if _live:
        _live.stop()
        _live = None

    t = Table(box=box.HEAVY, border_style="cyan", show_header=False, padding=(0, 3))
    t.add_column(style="dim", min_width=24)
    t.add_column(style="bold white", min_width=12)
    t.add_row("Session Duration",     elapsed_str())
    t.add_row("Calls Completed",      f"[bold green]{stats['completed']}[/bold green]")
    t.add_row("  Interested",         f"[green]{stats['interested']}[/green]")
    t.add_row("  No Answer",          f"[yellow]{stats['no_answer']}[/yellow]")
    t.add_row("  Rejected",           f"[red]{stats['rejected']}[/red]")
    t.add_row("  Demo Booked",        f"[cyan]{stats['demo']}[/cyan]")
    t.add_row("Avg Calls / Min",      cpm_str())
    t.add_row("Remaining New Leads",  f"[yellow]{_cached_new}[/yellow]")
    t.add_row("Remaining Follow-Ups", f"[cyan]{_cached_followup}[/cyan]")

    console.print(Panel(t, title="  SESSION SUMMARY  ", border_style="cyan", box=box.HEAVY, padding=(1, 3)))

    if return_after:
        console.print("\n[dim]// press any key to return[/dim]")
        readchar.readkey()


def cleanup(lead):
    if lead:
        try:
            c = db()
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE business_leads SET locked=false, session_id=NULL WHERE id=%s",
                    (lead["id"],),
                )
            c.commit()
        except Exception:
            pass


def exit_program(lead):
    global _live
    if _live:
        _live.stop()
        _live = None
    cleanup(lead)
    show_summary(return_after=False)
    console.print("\n[bold cyan]// session terminated — good hunting[/bold cyan]\n")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# INTEGER PROMPT  — stops Live, reads input, caller restarts Live
# ─────────────────────────────────────────────────────────────────────────────

def prompt_int(prompt_text: str):
    global _live
    if _live:
        _live.stop()
        _live = None
    console.print(f"\n[bold yellow]{prompt_text}[/bold yellow] ", end="")
    try:
        val = input().strip()
        n = int(val)
        return n if n > 0 else None
    except Exception:
        return None


def restart_live(lead):
    """Start a fresh Live context after prompt_int."""
    global _live
    _live = Live(
        build_screen(lead),
        console=console,
        refresh_per_second=2,
        screen=False,        # inline — works on Termux and Windows alike
        transient=False,
    )
    _live.start()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global current_lead, _live

    connect_db()
    unlock_abandoned_sessions()
    refresh_counts()

    current_lead = get_next_lead()
    if current_lead is None:
        console.print(Panel(
            "[yellow]No leads available.\nAll new leads contacted; no follow-ups are due yet.[/yellow]",
            title="[yellow]QUEUE EMPTY[/yellow]", border_style="yellow", box=box.HEAVY,
        ))
        console.print("[dim]// press any key to exit[/dim]")
        readchar.readkey()
        sys.exit(0)

    restart_live(current_lead)

    # Clock ticker — updates elapsed time every second in background
    def tick():
        while True:
            time.sleep(1)
            if _live and _live.is_started and current_lead:
                try:
                    _live.update(build_screen(current_lead))
                except Exception:
                    pass
    t = threading.Thread(target=tick, daemon=True)
    t.start()

    # ── MAIN INPUT LOOP ───────────────────────────────────────────────────────
    while True:
        try:
            key = readchar.readkey().lower()
        except Exception:
            continue

        # ── Q  Rejected ───────────────────────────────────────────────────────
        if key == "q":
            mark_negative(current_lead)
            refresh_counts()
            current_lead = get_next_lead()
            if current_lead:
                _live.update(build_screen(current_lead))
            else:
                exit_program(None)

        # ── A  No Answer ──────────────────────────────────────────────────────
        elif key == "a":
            mark_no_answer(current_lead)
            refresh_counts()
            current_lead = get_next_lead()
            if current_lead:
                _live.update(build_screen(current_lead))
            else:
                exit_program(None)

        # ── F  Interested ─────────────────────────────────────────────────────
        elif key == "f":
            lead_snapshot = current_lead
            days = prompt_int("Follow-up in how many days?")
            if days:
                mark_positive(lead_snapshot, days)
                refresh_counts()
                current_lead = get_next_lead()
            if current_lead:
                restart_live(current_lead)
            else:
                exit_program(None)

        # ── D  Demo ───────────────────────────────────────────────────────────
        elif key == "d":
            lead_snapshot = current_lead
            days = prompt_int("Demo in how many days?")
            if days:
                mark_demo(lead_snapshot, days)
                refresh_counts()
                current_lead = get_next_lead()
            if current_lead:
                restart_live(current_lead)
            else:
                exit_program(None)

        # ── S  Summary ────────────────────────────────────────────────────────
        elif key == "s":
            show_summary(return_after=True)
            restart_live(current_lead)

        # ── ESC  Exit ─────────────────────────────────────────────────────────
        elif key == readchar.key.ESC:
            exit_program(current_lead)


if __name__ == "__main__":
    main()