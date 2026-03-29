# =============================================================
# CORE MODULE — Stacked Knowledge: A Library System
# This is the main backend file of the library system.

# REQUIREMENTS:
# 1. Install dependencies via terminal/command prompt:  pip install mysql-connector-python
# 2. XAMPP must be running with MySQL enabled.
# 3. Database 'library_db' will be auto-created on first run.
#
# NOTE: The GUI version of this system is in 'library_gui.py'.
#       For database and logic review, this is the file to read.
# =============================================================

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
import hashlib
import math
import gc
import multiprocessing
import os
import time
import unicodedata

try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False
    print("\nWARNING: mysql-connector-python is not installed.")
    print("  Install it with:  pip install mysql-connector-python")
    print("  Using fallback mode for now...\n")

# --- Database connection settings ---
DB_CONFIG = {
    "host":     "localhost",
    "user":     "root",
    "password": "",
    "database": "library_db",
}

# --- System-wide constants ---
MAX_BORROW    = 5    # Max books a single user may hold at once
FEE_PER_DAY   = 50  # Late fee in PHP per day past due date
BORROW_DAYS   = 7   # Loan period in days
MAX_STR_LEN   = 255 # Matches DB VARCHAR(255) for title/author
MAX_USER_LEN  = 50  # Matches DB VARCHAR(50) for username
MIN_BOOK_YEAR = 0   # Allows ancient/undated books (e.g. year 500 BC editions)

# Used when the DB is down so the UI still shows categories
DEWEY_FALLBACK = {
    "000": "General Knowledge", "100": "Philosophy",  "200": "Religion",
    "300": "Social Sciences",   "400": "Language",     "500": "Science",
    "600": "Technology",        "700": "Arts",         "800": "Literature",
    "900": "History"
}

# INPUT HELPERS
def safe_input(prompt="") -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        raise

# Strips whitespace, normalizes unicode to NFC, and truncates to max_len.
def sanitize_str(value: str, max_len: int = MAX_STR_LEN) -> str:
    value = value.strip()
    value = unicodedata.normalize("NFC", value)
    if len(value) > max_len:
        value = value[:max_len]
        print(f"  [Input] Text truncated to {max_len} characters.")
    return value

# Converts a string to a positive integer or returns None on failure.
def parse_positive_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit():
        return None
    if len(raw) > 7:
        return None
    value = int(raw)
    return value if value > 0 else None

# Like parse_positive_int but also accepts 0 (used as 'cancel'); Returns None on any truly invalid input.
def parse_book_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit():
        return None
    if len(raw) > 7:
        return None
    return int(raw)

# Parses a positive, finite float
def parse_positive_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value

# Parses a publication year string (Blank → None; Valid → int)
def parse_year(raw: str) -> int | None | bool:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit():
        return False
    try:
        year = int(raw)
    except (ValueError, OverflowError):
        return False
    if MIN_BOOK_YEAR <= year <= datetime.now().year:
        return year
    return False

# =============================================================

# SHA-256 hash for password storage. Never stored in plaintext.
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def line():
    print("-" * 75)

# PARALLEL REPORT WORKERS (These run in separate processes via multiprocessing)

# Calculates total outstanding fees and per-user breakdown.
def _worker_fee_summary(users, result_queue):
    try:
        total_fees = sum(u.total_fees for u in users)
        users_with_fees = [(u.username, u.total_fees) for u in users if u.total_fees > 0]
        result_queue.put(("fee_summary", total_fees, users_with_fees))
    except Exception as e:
        result_queue.put(("error", "fee_summary", str(e)))

# Finds all currently overdue borrows across all users.
def _worker_overdue_report(users, result_queue):
    try:
        overdue = [
            (u.username, r.title, r.due_on.strftime("%Y-%m-%d"), r.overdue_fee())
            for u in users
            for r in u.borrowed
            if r.overdue_fee() > 0
        ]
        result_queue.put(("overdue_report", overdue))
    except Exception as e:
        result_queue.put(("error", "overdue_report", str(e)))

# Summarizes inventory: totals, available copies, and waitlists.
def _worker_book_availability(books, result_queue):
    try:
        total_titles  = len(books)
        total_copies  = sum(b.total for b in books)
        available_now = sum(b.available for b in books)
        on_waitlist   = sum(len(b.waitlist) for b in books)
        # Only books with zero copies left are 'fully borrowed'
        unavailable   = [(b.title, b.total - b.available, b.total) for b in books if b.available == 0]
        result_queue.put(("book_availability", total_titles, total_copies, available_now, on_waitlist, unavailable))
    except Exception as e:
        result_queue.put(("error", "book_availability", str(e)))

# Spawns 3 worker processes simultaneously to generate reports.
def run_parallel_tasks(lib):
    users        = lib.users
    books        = lib.books
    result_queue = multiprocessing.Queue()

    processes = [
        multiprocessing.Process(target=_worker_fee_summary,       args=(users, result_queue)),
        multiprocessing.Process(target=_worker_overdue_report,    args=(users, result_queue)),
        multiprocessing.Process(target=_worker_book_availability, args=(books, result_queue)),
    ]

    print("\n" + "=" * 75)
    print("  OPAC Parallel Report Generation — Starting 3 processes...")
    print("=" * 75)
    start = time.time()

    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        if p.is_alive():
            print(f"  [Warning] Process {p.name} timed out and was terminated.")
            p.terminate()
            p.join()

    elapsed = time.time() - start

    # Drain the queue into a dict keyed by report type
    reports = {}
    while not result_queue.empty():
        item = result_queue.get()
        if item[0] == "error":
            print(f"  [Report Error] '{item[1]}' failed: {item[2]}")
        else:
            reports[item[0]] = item[1:]

    print(f"\n  All reports generated in {elapsed:.2f}s")
    line()

    if "fee_summary" in reports:
        total_fees, users_with_fees = reports["fee_summary"]
        print("  FEE SUMMARY")
        if users_with_fees:
            for username, fee in users_with_fees:
                print(f"    {username:<15} P{fee}")
        else:
            print("    No outstanding fees.")
        print(f"    Total owed: P{total_fees}")
        line()

    if "overdue_report" in reports:
        overdue = reports["overdue_report"][0]
        print("  OVERDUE BOOKS")
        if overdue:
            for username, title, due, fee in overdue:
                print(f"    {username:<12} '{title[:20]}' due {due} | P{fee}")
        else:
            print("    No overdue books.")
        line()

    if "book_availability" in reports:
        total_titles, total_copies, available_now, on_waitlist, unavailable = reports["book_availability"]
        print("  BOOK AVAILABILITY")
        print(f"    Total titles   : {total_titles}")
        print(f"    Total copies   : {total_copies}")
        print(f"    Available now  : {available_now}")
        print(f"    On waitlist    : {on_waitlist}")
        if unavailable:
            print(f"    Fully borrowed :")
            for title, borrowed, total in unavailable:
                print(f"      '{title[:25]}' ({borrowed}/{total} out)")
        else:
            print(f"    Fully borrowed : None")
        line()

    safe_input("\n  Press Enter to continue...")


# DATABASE LAYER
# Wraps mysql-connector-python. Handles auto-schema creation,
# reconnection, and degrades gracefully when MySQL is unavailable.
class Database:
    def __init__(self, config: dict):
        self._config = config
        self.conn    = None
        if MYSQL_AVAILABLE:
            self._connect_and_init()
        else:
            print("  [DB] MySQL not available. Database operations disabled.")

# Creates the database if it doesn't exist, then connects to it.
# Pops 'database' from config to connect without specifying a DB first
    def _connect_and_init(self):
        cfg     = self._config.copy()
        db_name = cfg.pop("database")

        try:
            tmp = mysql.connector.connect(**cfg)
            cur = tmp.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            tmp.commit()
            cur.close()
            tmp.close()
        except MySQLError as e:
            if "10061" in str(e) or "Can't connect" in str(e):
                print("\n" + "=" * 60)
                print("  ERROR: Cannot connect to MySQL")
                print("=" * 60)
                print("  MySQL is not running on localhost:3306.")
                print()
                print("  To fix this:")
                print("    1. Open XAMPP Control Panel")
                print("    2. Click 'Admin' next to MySQL")
                print("    3. Restart this program")
                print("=" * 60 + "\n")
                raise SystemExit
            print(f"  [DB] Could not create database '{db_name}': {e}")
            raise

        try:
            self.conn = mysql.connector.connect(database=db_name, **cfg)
            self.conn.autocommit = True
            self._init_schema()
        except MySQLError as e:
            print(f"  [DB] Connection failed: {e}")
            raise

# Creates all tables if they don't exist yet (idempotent).
# FK constraint on books.dewey ensures only valid Dewey codes are stored.
# CHECK constraints enforce business rules at the DB level as a safety net.
    def _init_schema(self):
        ddl = [
            """CREATE TABLE IF NOT EXISTS dewey_categories (
                code  CHAR(3)     NOT NULL,
                label VARCHAR(50) NOT NULL,
                PRIMARY KEY (code)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS books (
                id        INT          NOT NULL AUTO_INCREMENT,
                title     VARCHAR(255) NOT NULL,
                author    VARCHAR(255) NOT NULL,
                dewey     CHAR(3)      NOT NULL DEFAULT '000',
                total     INT          NOT NULL DEFAULT 1,
                available INT          NOT NULL DEFAULT 1,
                year      SMALLINT,
                PRIMARY KEY (id),
                CONSTRAINT chk_available CHECK (available >= 0 AND available <= total),
                CONSTRAINT fk_dewey FOREIGN KEY (dewey) REFERENCES dewey_categories(code)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS users (
                username      VARCHAR(50) NOT NULL,
                password_hash CHAR(64)    NOT NULL,
                active        TINYINT(1)  NOT NULL DEFAULT 1,
                PRIMARY KEY (username)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS borrow_records (
                id          INT            NOT NULL AUTO_INCREMENT,
                username    VARCHAR(50)    NOT NULL,
                book_id     INT            NOT NULL,
                book_title  VARCHAR(255)   NOT NULL,
                borrowed_on DATETIME       NOT NULL,
                due_on      DATETIME       NOT NULL,
                returned_on DATETIME,
                fee         DECIMAL(10,2)  NOT NULL DEFAULT 0.00,
                PRIMARY KEY (id),
                CONSTRAINT chk_fee CHECK (fee >= 0),
                INDEX idx_user (username),
                INDEX idx_book (book_id)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS waitlist (
                book_id  INT         NOT NULL,
                username VARCHAR(50) NOT NULL,
                position INT         NOT NULL,
                PRIMARY KEY (book_id, username)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS payments (
                id       INT            NOT NULL AUTO_INCREMENT,
                username VARCHAR(50)    NOT NULL,
                amount   DECIMAL(10,2)  NOT NULL,
                paid_on  DATETIME       NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT chk_payment_amount CHECK (amount > 0),
                INDEX idx_user (username)
            ) ENGINE=InnoDB""",
        ]

        cur = self.conn.cursor()
        for stmt in ddl:
            cur.execute(stmt)
        cur.close()
        self._seed_dewey_categories()

    def _seed_dewey_categories(self):
        categories = [
            ("000", "General Knowledge"), ("100", "Philosophy"),
            ("200", "Religion"),          ("300", "Social Sciences"),
            ("400", "Language"),          ("500", "Science"),
            ("600", "Technology"),        ("700", "Arts"),
            ("800", "Literature"),        ("900", "History"),
        ]
        cur = self.conn.cursor()
        cur.executemany(
            "INSERT IGNORE INTO dewey_categories (code, label) VALUES (%s, %s)",
            categories
        )
        cur.close()

# Pings the DB before each query to catch stale connections
# (e.g. after MySQL restart). Falls back to a full reconnect if ping fails.
    def _reconnect_if_needed(self):
        if not MYSQL_AVAILABLE or not self.conn:
            return
        try:
            self.conn.ping(reconnect=True, attempts=3, delay=1)
        except MySQLError as e:
            print(f"  [DB] Reconnect failed: {e}. Attempting full reconnect...")
            try:
                cfg = self._config.copy()
                self.conn = mysql.connector.connect(**cfg)
                self.conn.autocommit = True
            except MySQLError as e2:
                print(f"  [DB] Full reconnect also failed: {e2}. DB operations may not persist.")

    def execute(self, sql: str, params=None):
        if not MYSQL_AVAILABLE or not self.conn:
            return None
        self._reconnect_if_needed()
        cur = self.conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        return cur

    def fetchall(self, sql: str, params=None) -> list:
        cur = self.execute(sql, params)
        if cur is None:
            return []
        rows = cur.fetchall()
        cur.close()
        return rows

    def fetchone(self, sql: str, params=None):
        cur = self.execute(sql, params)
        if cur is None:
            return None
        row = cur.fetchone()
        cur.close()
        return row


# EVENT DISPATCHER
# Implements a simple publish-subscribe (observer) pattern.
# Menus register named events; user input triggers emit().
class EventDispatcher:
    def __init__(self):
        self._handlers: dict[str, list] = {}

    # Subscribes handler to an event. Multiple handlers per event are allowed.
    def register(self, event: str, handler):
        self._handlers.setdefault(event, []).append(handler)

    # Calls all handlers registered to the given event name."
    def emit(self, event: str, *args):
        handlers = self._handlers.get(event, [])
        if not handlers:
            print(f"  [Event] No handler for event: '{event}'")
            return
        for handler in handlers:
            handler(*args)


# DOMAIN MODELS
class Person(ABC):
    # Abstract base class for all system actors (User, Admin)
    def __init__(self, username, password, pre_hashed=False):
        self.username = username
        # Accept already-hashed passwords when loading from DB
        self._pw      = password if pre_hashed else hash_pw(password)

    def check_password(self, password):
        # Compares a plaintext attempt against the stored hash
        return self._pw == hash_pw(password)

    @abstractmethod
    def view_dashboard(self):
        # Each subclass must implement its own dashboard view.
        pass


class Book:
    # Represents a library book with copy/availability tracking.
    _next_id = 1

    def __init__(self, title, author, dewey, copies=1, year=None, book_id=None):
        # Use a provided DB ID (on load) or auto-increment the class counter
        if book_id is not None:
            self.id = book_id
        else:
            self.id        = Book._next_id
            Book._next_id += 1
        self.title     = title
        self.author    = author
        self.dewey     = dewey
        self.total     = copies
        self.available = copies
        self.waitlist  = []
        self.year      = year


class BorrowRecord:
    # Tracks one borrow transaction: dates, fees, and return status.
    def __init__(self, book):
        self.record_id   = None
        self.book        = book
        self.title       = book.title
        self.book_id     = book.id
        self.borrowed_on = datetime.now()
        self.due_on      = self.borrowed_on + timedelta(days=BORROW_DAYS)
        self.returned_on = None
        self.fee         = 0

    def overdue_fee(self):
        # Returns current late fee. Only applies while the book is still out.
        if not self.returned_on and datetime.now() > self.due_on:
            return (datetime.now() - self.due_on).days * FEE_PER_DAY
        return 0

    def do_return(self):
        # Finalizes the return: stamps return time, locks in the fee, increments book availability, and notifies the next waitlisted user.
        self.returned_on = datetime.now()
        self.fee = self.overdue_fee()
        if self.book:
            self.book.available += 1
            if self.book.waitlist:
                next_user = self.book.waitlist.pop(0)
                print(f"[Waitlist] '{self.book.title}' is now reserved for {next_user.username}!")
            self.book = None


class User(Person):
    # A library patron who can borrow books, pay fees, and join waitlists
    def __init__(self, username, password, db=None, pre_hashed=False):
        super().__init__(username, password, pre_hashed=pre_hashed)
        self.db       = db      # DB handle; None in worker processes
        self.active   = True
        self.borrowed = []      # Currently borrowed BorrowRecords
        self.history  = []      # Returned BorrowRecords (for fee tracking)
        self.paid     = 0       # Cumulative payments made (from payments table)

    def __getstate__(self):
        # Called before pickling (multiprocessing). Strips the DB connection.
        state = self.__dict__.copy()
        state["db"] = None
        return state

    def __setstate__(self, state):
        # Called after unpickling. Re-inserts db=None safely.
        state.setdefault("db", None)
        self.__dict__.update(state)

    @property
    def total_fees(self):
        # Computed property: sums current overdue fees + settled fees from history,
        # then subtracts payments already made. Floors at 0 (no negative debt).
        owed = (sum(r.overdue_fee() for r in self.borrowed)
                + sum(r.fee for r in self.history))
        return max(0, owed - self.paid)

    def borrow_book(self, book):
        if book is None:
            print("Book not found."); return
        if len(self.borrowed) >= MAX_BORROW:
            print(f"Borrow limit reached ({MAX_BORROW} books max)."); return
        if self.total_fees > 0:
            print(f"Please settle your fees first: P{self.total_fees}"); return
        if book.available <= 0:
            print("Book is not available.")
            ans = safe_input("Join waitlist? (yes/no): ").strip().lower()
            if ans == "yes":
                if self not in book.waitlist:
                    book.waitlist.append(self)
                    pos = book.waitlist.index(self)
                    if self.db:
                        # ON DUPLICATE KEY UPDATE handles re-queueing
                        self.db.execute(
                            "INSERT INTO waitlist (book_id, username, position) "
                            "VALUES (%s, %s, %s) "
                            "ON DUPLICATE KEY UPDATE position = VALUES(position)",
                            (book.id, self.username, pos)
                        )
                    print(f"Added to waitlist. Position: {pos + 1}")
                else:
                    print("You're already on the waitlist.")
            return

        record = BorrowRecord(book)
        self.borrowed.append(record)
        book.available -= 1

        if self.db:
            self.db.conn.autocommit = False
            try:
                cur = self.db.execute(
                    "INSERT INTO borrow_records "
                    "(username, book_id, book_title, borrowed_on, due_on) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (self.username, book.id, book.title,
                     record.borrowed_on, record.due_on)
                )
                record.record_id = cur.lastrowid
                cur.close()
                self.db.execute(
                    "UPDATE books SET available = %s WHERE id = %s",
                    (book.available, book.id)
                )
                self.db.conn.commit()
            except Exception as e:
                self.db.conn.rollback()
                # Undo in-memory changes so state stays in sync with DB
                book.available += 1
                self.borrowed.remove(record)
                print(f"Borrow failed and was rolled back: {e}")
                return
            finally:
                self.db.conn.autocommit = True

        print(f"Borrowed '{book.title}' | Borrowed on: "
              f"{record.borrowed_on.strftime('%Y-%m-%d')} | "
              f"Due: {record.due_on.strftime('%Y-%m-%d')}")

    def return_book(self, book_id):
        # Returns a borrowed book by its ID. Moves the record from borrowed → history,
        # updates returned_on + fee in DB, increments availability, and refreshes
        # the waitlist table to reflect the new positions.
        # Rolls back on any DB error.

        record = next((r for r in self.borrowed if r.book_id == book_id), None)
        if not record:
            print("No active borrow found for that ID."); return

        book = record.book
        record.do_return()
        self.borrowed.remove(record)
        self.history.append(record)

        if self.db:
            self.db.conn.autocommit = False
            try:
                self.db.execute(
                    "UPDATE borrow_records "
                    "SET returned_on = %s, fee = %s "
                    "WHERE id = %s",
                    (record.returned_on, record.fee, record.record_id)
                )
                if book:
                    self.db.execute(
                        "UPDATE books SET available = %s WHERE id = %s",
                        (book.available, book.id)
                    )
                    # Rebuild the waitlist from scratch to keep positions consistent
                    self.db.execute(
                        "DELETE FROM waitlist WHERE book_id = %s", (book.id,)
                    )
                    for pos, wuser in enumerate(book.waitlist):
                        self.db.execute(
                            "INSERT INTO waitlist (book_id, username, position) "
                            "VALUES (%s, %s, %s)",
                            (book.id, wuser.username, pos)
                        )
                self.db.conn.commit()
            except Exception as e:
                self.db.conn.rollback()
                # Undo in-memory move if DB write failed
                self.history.remove(record)
                self.borrowed.append(record)
                print(f"Return failed and was rolled back: {e}")
                return
            finally:
                self.db.conn.autocommit = True

        print(f"Returned '{record.title}' | Fee: P{record.fee}")

    def pay_fees(self, amount: float):
        # Records a payment. Caps the amount at what's actually owed.
        # Rolls back the in-memory increment if the DB write fails.
        # Amount is pre-validated by parse_positive_float before reaching here.
        if self.total_fees == 0:
            print("No fees to pay."); return

        amount = min(amount, self.total_fees)  # Don't accept overpayment
        self.paid += amount

        if self.db:
            try:
                self.db.execute(
                    "INSERT INTO payments (username, amount, paid_on) "
                    "VALUES (%s, %s, %s)",
                    (self.username, amount, datetime.now())
                )
            except Exception as e:
                self.paid -= amount  # Undo in-memory change
                print(f"Payment failed: {e}")
                return

        print(f"Paid P{amount:.2f}. Remaining fees: P{self.total_fees}")

    def view_dashboard(self):
        # Displays the user's active borrows and return history in a table.
        print(f"\n  {self.username}'s Dashboard  |  "
              f"Borrowing: {len(self.borrowed)}/{MAX_BORROW}  |  Fees: P{self.total_fees}")
        line()
        print(f"  {'ID':<4} {'Title':<25} {'Borrowed On':<12} {'Due':<12} {'Status':<10} Fee")
        line()
        for r in self.borrowed:
            flag = " !" if r.overdue_fee() else ""  # Visual warning for overdue
            print(f"  {r.book_id:<4} {r.title[:24]:<25} "
                  f"{r.borrowed_on.strftime('%Y-%m-%d'):<12} "
                  f"{r.due_on.strftime('%Y-%m-%d'):<12} {'Borrowed':<10} "
                  f"P{r.overdue_fee()}{flag}")
        for r in self.history:
            print(f"  {r.book_id:<4} {r.title[:24]:<25} "
                  f"{r.borrowed_on.strftime('%Y-%m-%d'):<12} "
                  f"{r.due_on.strftime('%Y-%m-%d'):<12} {'Returned':<10} P{r.fee}")
        line()


class Admin(Person):
    # System administrator with access to full reports and book management.
    def view_dashboard(self):
        print("Use view_report(library) for the full report.")

    def view_report(self, lib):
        # Prints a system-wide report: user counts, borrows, fees, and top book.
        users       = lib.users
        all_records = [r for u in users for r in u.borrowed + u.history]
        total_fees  = sum(u.total_fees for u in users)

        # Find the most borrowed title by frequency across all records
        counts = {}
        for r in all_records:
            counts[r.title] = counts.get(r.title, 0) + 1
        top = max(counts, key=counts.get) if counts else "N/A"

        print("\n=== SYSTEM REPORT ===")
        print(f"  Users         : {len(users)}")
        print(f"  Active borrows: {sum(len(u.borrowed) for u in users)}")
        print(f"  Total returns : {sum(len(u.history) for u in users)}")
        print(f"  Fees owed     : P{total_fees}")
        print(f"  Most borrowed : {top}")
        line()
        if not all_records:
            print("  No borrow records yet.")
        else:
            print(f"  {'User':<12} {'ID':<4} {'Title':<25} "
                  f"{'Status':<10} {'Borrowed On':<12} {'Due/Returned':<12} Fee")
            line()
            for u in users:
                for r in u.borrowed:
                    flag = " !" if r.overdue_fee() else ""
                    print(f"  {u.username:<12} {r.book_id:<4} {r.title[:24]:<25} "
                          f"{'Borrowed':<10} {r.borrowed_on.strftime('%Y-%m-%d'):<12} "
                          f"{r.due_on.strftime('%Y-%m-%d'):<12} "
                          f"P{r.overdue_fee()}{flag}")
                for r in u.history:
                    print(f"  {u.username:<12} {r.book_id:<4} {r.title[:24]:<25} "
                          f"{'Returned':<10} {r.borrowed_on.strftime('%Y-%m-%d'):<12} "
                          f"{r.returned_on.strftime('%Y-%m-%d'):<12} P{r.fee}")
        line()


# LIBRARY
class Library:
    def __init__(self):
        self.books            = []
        self.users            = []
        self.admins           = [Admin("admin", "123")]  # Hardcoded admin for simplicity
        self.dewey_categories = DEWEY_FALLBACK.copy()   # Overwritten by DB on load

        self.db = Database(DB_CONFIG)
        self._load_from_db()

    def _load_from_db(self):
        # Load categories first (books reference them)
        dewey_rows = self.db.fetchall("SELECT code, label FROM dewey_categories")
        if dewey_rows:
            self.dewey_categories = {r["code"]: r["label"] for r in dewey_rows}

        book_rows = self.db.fetchall("SELECT * FROM books ORDER BY id")
        if not book_rows:
            self._seed_default_books()
        else:
            for r in book_rows:
                book           = Book(r["title"], r["author"], r["dewey"],
                                      r["total"], r["year"], book_id=r["id"])
                book.available = r["available"]  # Stored in DB, not recomputed
                self.books.append(book)
            # Sync the class counter so new books get unique IDs
            Book._next_id = max(b.id for b in self.books) + 1

        book_map = {b.id: b for b in self.books}

        user_rows = self.db.fetchall("SELECT * FROM users ORDER BY username")
        for r in user_rows:
            user        = User(r["username"], r["password_hash"],
                               db=self.db, pre_hashed=True)
            user.active = bool(r["active"])
            self.users.append(user)

        user_map = {u.username: u for u in self.users}

        # Hydrate borrow records and link them to live Book objects
        rec_rows = self.db.fetchall("SELECT * FROM borrow_records ORDER BY id")
        for r in rec_rows:
            user = user_map.get(r["username"])
            if not user:
                continue
            book = book_map.get(r["book_id"])

            # Bypass __init__ since we're restoring existing state, not creating new
            rec             = BorrowRecord.__new__(BorrowRecord)
            rec.record_id   = r["id"]
            rec.book        = book        # None if book was deleted after borrowing
            rec.title       = r["book_title"]
            rec.book_id     = r["book_id"]
            rec.borrowed_on = r["borrowed_on"]
            rec.due_on      = r["due_on"]
            rec.returned_on = r["returned_on"]
            rec.fee         = float(r["fee"])

            if rec.returned_on is None:
                user.borrowed.append(rec)
            else:
                user.history.append(rec)

        # Restore waitlists in position order
        wl_rows = self.db.fetchall(
            "SELECT * FROM waitlist ORDER BY book_id, position"
        )
        for r in wl_rows:
            book = book_map.get(r["book_id"])
            user = user_map.get(r["username"])
            if book and user and user not in book.waitlist:
                book.waitlist.append(user)

        # Load cumulative payments per user (grouped in SQL to avoid N+1 queries)
        payment_rows = self.db.fetchall(
            "SELECT username, SUM(amount) as total_paid "
            "FROM payments GROUP BY username"
        )
        for r in payment_rows:
            user = user_map.get(r["username"])
            if user:
                user.paid = float(r["total_paid"])

    def _seed_default_books(self):
        defaults = [
            ("Intro to Programming",      "Ada Lovelace",         "600", 3, 2020),
            ("Philosophy 101",            "Aristotle",            "100", 2, None),
            ("World History",             "Howard Zinn",          "900", 4, 1980),
            ("Basic Science",             "Isaac Newton",         "500", 2, None),
            ("English Literature",        "Shakespeare",          "800", 5, None),
            ("Thinking, Fast and Slow",   "Daniel Kahneman",      "100", 5, 2011),
            ("A Brief History of Time",   "Stephen Hawking",      "500", 6, 1988),
            ("Sapiens",                   "Yuval Noah Harari",    "900", 8, 2011),
            ("The Pragmatic Programmer",  "Hunt & Thomas",        "000", 4, 1999),
            ("The Art of War",            "Sun Tzu",              "300", 7, 500),
            ("The Language Instinct",     "Steven Pinker",        "400", 3, 1994),
            ("The Story of Art",          "E.H. Gombrich",        "700", 5, 1950),
            ("The God Delusion",          "Richard Dawkins",      "200", 6, 2006),
            ("Don Quixote",               "Miguel de Cervantes",  "800", 4, 1605),
            ("The Clean Coder",           "Robert C. Martin",     "600", 5, 2011),
        ]
        for t, a, d, c, y in defaults:
            cur = self.db.execute(
                "INSERT INTO books (title, author, dewey, total, available, year) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (t, a, d, c, c, y)
            )
            if cur is None:
                book = Book(t, a, d, c, y)
            else:
                db_id = cur.lastrowid
                cur.close()
                book = Book(t, a, d, c, y, book_id=db_id)
            self.books.append(book)
        if self.books:
            Book._next_id = max(b.id for b in self.books) + 1

    # --- CRUD Operations ---

    def add_book(self, title, author, dewey, copies, year):
        # Inserts a new book into DB and appends it to the in-memory list.
        try:
            cur = self.db.execute(
                "INSERT INTO books (title, author, dewey, total, available, year) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (title, author, dewey, copies, copies, year)
            )
            if cur is None:
                book = Book(title, author, dewey, copies, year)
            else:
                db_id = cur.lastrowid
                cur.close()
                book = Book(title, author, dewey, copies, year, book_id=db_id)
            self.books.append(book)
            print(f"Book '{title}' added!")
        except Exception as e:
            print(f"Failed to add book: {e}")

    def delete_book(self, book):
        # Removes a book from the DB and in-memory list. Deletes waitlist entries first to avoid FK violations.
        try:
            self.db.execute("DELETE FROM waitlist WHERE book_id = %s", (book.id,))
            self.db.execute("DELETE FROM books     WHERE id      = %s", (book.id,))
            self.books.remove(book)
            print(f"Book '{book.title}' deleted.")
        except Exception as e:
            print(f"Failed to delete book: {e}")

    def update_book(self, book):
        # Persists in-memory changes to an existing book back to the DB.
        try:
            self.db.execute(
                "UPDATE books "
                "SET title = %s, author = %s, dewey = %s, "
                "    total = %s, available = %s, year = %s "
                "WHERE id = %s",
                (book.title, book.author, book.dewey,
                 book.total, book.available, book.year, book.id)
            )
        except Exception as e:
            print(f"Failed to save book update to database: {e}")

    def find_by_id(self, book_id):
        return next((b for b in self.books if b.id == book_id), None)

    def find_by_title(self, title):
        return next((b for b in self.books if b.title.lower() == title.lower()), None)

    def find_by_id_or_title(self, query):
        parsed_id = parse_book_id(query)
        if parsed_id is not None and parsed_id > 0:
            return self.find_by_id(parsed_id)
        return self.find_by_title(query)

    def register(self, username, password):
        username = sanitize_str(username, max_len=MAX_USER_LEN)
        if len(username) < 3:
            print("Username must be at least 3 characters."); return None
        if any(u.username == username for u in self.users):
            print("Username already taken."); return None
        if not password:
            print("Password cannot be empty."); return None

        user = User(username, password, db=self.db)
        self.users.append(user)
        try:
            self.db.execute(
                "INSERT INTO users (username, password_hash, active) "
                "VALUES (%s, %s, %s)",
                (user.username, user._pw, 1)
            )
        except Exception as e:
            self.users.remove(user)  # Keep in-memory list consistent with DB
            print(f"Registration failed: {e}")
            return None
        print(f"Registered '{username}'!"); return user

    def login_user(self, username, password):
        user = next((u for u in self.users if u.username == username), None)
        if user and user.check_password(password) and user.active:
            return user
        print("Invalid credentials or account inactive."); return None

    def login_admin(self, username, password):
        admin = next((a for a in self.admins if a.username == username), None)
        if admin and admin.check_password(password):
            return admin
        print("Access denied."); return None

    def show_books(self, query=""):
        # Searches books by title, author, Dewey code, or category name.
        # Empty query returns all books.
        results = [
            b for b in self.books
            if query.lower() in b.title.lower()
            or query.lower() in b.author.lower()
            or query in b.dewey
            or query.lower() in self.dewey_categories.get(b.dewey, "").lower()
        ]
        if not results:
            print("No books found."); return
        print(f"\n  {'ID':<4} {'Title':<25} {'Author':<18} {'Year':<6} {'Category':<16} Avail")
        line()
        for b in results:
            wl   = f" (waitlist:{len(b.waitlist)})" if b.waitlist else ""
            year = str(b.year) if b.year else "N/A"
            print(f"  {b.id:<4} {b.title[:24]:<25} {b.author[:17]:<18} "
                  f"{year:<6} {self.dewey_categories.get(b.dewey, b.dewey)[:15]:<16} "
                  f"{b.available}/{b.total}{wl}")
        line()


# MENUS
# Each menu creates a local EventDispatcher and maps number keys to event names.
def admin_menu(admin, lib):
    dispatcher = EventDispatcher()

    def on_view_books():
        cats = "  |  ".join(f"{k}: {v}" for k, v in lib.dewey_categories.items())
        print(cats)
        lib.show_books(sanitize_str(safe_input("Search (blank = all): ")))

    def on_add_book():
        title = sanitize_str(safe_input("Title: "))
        if not title:
            print("Title cannot be empty."); return

        author = sanitize_str(safe_input("Author: "))
        if not author:
            print("Author cannot be empty."); return

        for code, cat in lib.dewey_categories.items():
            print(f"  {code}: {cat}")
        while True:
            dewey = safe_input("Dewey code: ").strip()
            if dewey in lib.dewey_categories:
                break
            print(f"  Invalid code. Choose from: {', '.join(lib.dewey_categories.keys())}")

        while True:
            copies = parse_positive_int(safe_input("Copies: "))
            if copies is not None:
                break
            print("  Invalid input. Enter a whole number greater than 0.")

        while True:
            year = parse_year(safe_input("Year published (blank to skip): "))
            if year is not False:    # None = blank (ok), int = valid year
                break
            print(f"  Invalid year. Enter a year between {MIN_BOOK_YEAR} and {datetime.now().year}.")

        lib.add_book(title, author, dewey, copies, year)

    def on_edit_book():
        lib.show_books()
        query = sanitize_str(safe_input("ID or Title to edit: "))
        book  = lib.find_by_id_or_title(query)
        if not book:
            print("Not found."); return

        new_title = sanitize_str(safe_input(f"Title [{book.title}]: "))
        book.title = new_title or book.title

        new_author = sanitize_str(safe_input(f"Author [{book.author}]: "))
        book.author = new_author or book.author

        while True:
            nc = parse_positive_int(safe_input(f"Copies [{book.total}]: ") or "")
            if nc is None:
                # Blank input = keep current value; break out of loop
                break
            new_total     = nc
            currently_out = book.total - book.available
            if new_total < currently_out:
                print(f"  Warning: {currently_out} copies are currently borrowed. "
                      f"Setting total to {new_total} (available will be 0).")
            book.available = max(0, new_total - currently_out)
            book.total     = new_total
            break

        while True:
            year = parse_year(safe_input(f"Year [{book.year or 'N/A'}]: "))
            if year is not False:
                if year is not None:   # None = blank = keep existing
                    book.year = year
                break
            print(f"  Invalid year. Enter a year between {MIN_BOOK_YEAR} and {datetime.now().year}.")

        print("Updated!")
        lib.update_book(book)

    def on_delete_book():
        lib.show_books()
        query = sanitize_str(safe_input("ID or Title to delete: "))
        book  = lib.find_by_id_or_title(query)
        if not book:
            print("Not found."); return
        if safe_input(f"Delete '{book.title}'? (yes/no): ").strip().lower() == "yes":
            lib.delete_book(book)

    def on_report():
        admin.view_report(lib)
        safe_input("\nPress Enter to continue...")

    def on_parallel_tasks():
        run_parallel_tasks(lib)

    # Map event names to handler functions
    dispatcher.register("view_books",     on_view_books)
    dispatcher.register("add_book",       on_add_book)
    dispatcher.register("edit_book",      on_edit_book)
    dispatcher.register("delete_book",    on_delete_book)
    dispatcher.register("report",         on_report)
    dispatcher.register("parallel_tasks", on_parallel_tasks)

    # Map menu numbers to event names
    event_map = {
        "1": "view_books",
        "2": "add_book",
        "3": "edit_book",
        "4": "delete_book",
        "5": "report",
        "6": "parallel_tasks",
    }

    while True:
        print("\n" + "=" * 35)
        print("         ADMIN PANEL")
        print("=" * 35)
        print("  1. View Books")
        print("  2. Add Book")
        print("  3. Edit Book")
        print("  4. Delete Book")
        print("  5. Report")
        print("  6. Parallel Report Generation")
        print("  7. Logout")
        print("=" * 35)
        c = safe_input("> ").strip()

        if c == "7":
            break
        elif c in event_map:
            dispatcher.emit(event_map[c])
        else:
            print("Invalid option.")


def user_menu(user, lib):
    dispatcher = EventDispatcher()

    def on_view_books():
        lib.show_books(sanitize_str(safe_input("Search (blank = all): ")))

    def on_borrow():
        # Warn the user if they already have overdue books before they borrow more
        overdue = [r for r in user.borrowed if r.overdue_fee() > 0]
        if overdue:
            print(f"\n  Warning: You have {len(overdue)} overdue book(s). "
                  f"Total fees: P{user.total_fees}")
        print(f"\nRules: {BORROW_DAYS}-day loan | P{FEE_PER_DAY}/day overdue "
              f"| Max {MAX_BORROW} books\n")
        lib.show_books()
        bid = sanitize_str(safe_input("Book ID or Title to borrow (0 to cancel): "))
        if bid and bid != "0":
            user.borrow_book(lib.find_by_id_or_title(bid))

    def on_return():
        if not user.borrowed:
            print("Nothing to return."); return
        user.view_dashboard()
        raw = safe_input("Book ID to return (0 to cancel): ").strip()
        # parse_book_id blocks negatives, unicode, and overflow (safe replacement for int())
        bid = parse_book_id(raw)
        if bid is None:
            print("Invalid ID. Please enter a number."); return
        if bid != 0:
            user.return_book(bid)

    def on_pay_fees():
        if user.total_fees == 0:
            print("You have no outstanding fees."); return
        print(f"Outstanding fees: P{user.total_fees}")
        raw = safe_input("Amount to pay P: ").strip()
        # parse_positive_float blocks nan, inf, 1e999, and negatives
        amount = parse_positive_float(raw)
        if amount is None:
            print("Invalid amount. Please enter a positive number (e.g. 100 or 50.50).")
            return
        user.pay_fees(amount)

    def on_dashboard():
        user.view_dashboard()
        safe_input("\nPress Enter to continue...")

    dispatcher.register("view_books",  on_view_books)
    dispatcher.register("borrow",      on_borrow)
    dispatcher.register("return_book", on_return)
    dispatcher.register("pay_fees",    on_pay_fees)
    dispatcher.register("dashboard",   on_dashboard)

    event_map = {
        "1": "view_books",
        "2": "borrow",
        "3": "return_book",
        "4": "pay_fees",
        "5": "dashboard",
    }

    while True:
        print("\n" + "=" * 35)
        print(f"    USER MENU  [{user.username}]")
        print("=" * 35)
        print("  1. View Books")
        print("  2. Borrow Book")
        print("  3. Return Book")
        print("  4. Pay Fees")
        print("  5. My Dashboard")
        print("  7. Logout")
        print("=" * 35)
        c = safe_input("> ").strip()

        if c == "7":
            break
        elif c in event_map:
            dispatcher.emit(event_map[c])
        else:
            print("Invalid option.")


def auth_menu(lib):
    # Login/registration screen. Returns a User object on success, None on back.
    dispatcher = EventDispatcher()
    result = [None]  # List used as a mutable container so closures can write to it

    def on_login():
        username = sanitize_str(safe_input("Username: "), max_len=MAX_USER_LEN)
        password = safe_input("Password: ")
        user = lib.login_user(username, password)
        if user:
            result[0] = user

    def on_register():
        username = sanitize_str(safe_input("Username: "), max_len=MAX_USER_LEN)
        password = safe_input("Password: ")
        user = lib.register(username, password)
        if user:
            result[0] = user

    dispatcher.register("login",    on_login)
    dispatcher.register("register", on_register)

    event_map = {"1": "login", "2": "register"}

    while True:
        print("\n" + "=" * 35)
        print("           ACCOUNT")
        print("=" * 35)
        print("  1. Login")
        print("  2. Register")
        print("  3. Back")
        print("=" * 35)
        c = safe_input("> ").strip()

        if c == "3":
            break
        elif c in event_map:
            dispatcher.emit(event_map[c])
            if result[0]:
                return result[0]
        else:
            print("Invalid option.")

    return None


def main():
    # Entry point. Initializes the Library (which connects to DB and loads state),
    # then runs the top-level menu loop. gc.collect() on exit releases any
    # remaining references and triggers __del__ cleanup where applicable.
    try:
        lib        = Library()
        dispatcher = EventDispatcher()

        def on_admin():
            admin = lib.login_admin(
                sanitize_str(safe_input("Admin username: "), max_len=MAX_USER_LEN),
                safe_input("Password: ")
            )
            if admin:
                admin_menu(admin, lib)

        def on_user():
            user = auth_menu(lib)
            if user:
                user_menu(user, lib)

        dispatcher.register("admin", on_admin)
        dispatcher.register("user",  on_user)

        event_map = {"1": "admin", "2": "user"}

        while True:
            print("\n" + "=" * 35)
            print("   STACKED KNOWLEDGE")
            print("=" * 35)
            print("  1. Admin")
            print("  2. User")
            print("  3. Exit")
            print("=" * 35)
            c = safe_input("> ").strip()

            if c == "3":
                print("Thank you for using Stacked Knowledge!")
                lib = None       # Drop reference so GC can finalize DB connection
                gc.collect()
                break
            elif c in event_map:
                dispatcher.emit(event_map[c])
            else:
                print("Invalid option.")

    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")
        gc.collect()
    except Exception as e:
        print(f"\n  [Fatal Error] An unexpected error occurred: {e}")
        print("  The program will now exit.")
        gc.collect()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()