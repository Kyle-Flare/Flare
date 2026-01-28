import sys
import os
import csv
import sqlite3
import hashlib
import importlib.util
import urllib.request
import datetime as dt
import traceback
from dataclasses import dataclass
from typing import Optional, List, Tuple

from PySide6.QtCore import Qt, QTimer, QSize, QObject, QEvent
from PySide6.QtGui import QAction, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QComboBox,
    QTableWidget, QTableWidgetItem, QFileDialog, QTabWidget,
    QTextEdit, QDialogButtonBox, QSplitter, QFrame, QHeaderView,
    QGraphicsDropShadowEffect, QInputDialog, QListWidget, QListWidgetItem
)



APP_TITLE = "Inventory MVP"
DB_FILE = "inventory.db"
FIREPLACE_CATEGORY_NAME = "fireplaces"  # case-insensitive match


# -----------------------------
# Utilities
# -----------------------------

def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def week_start(d: Optional[dt.date] = None) -> dt.date:
    if d is None:
        d = dt.date.today()
    return d - dt.timedelta(days=d.weekday())


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def norm(s: str) -> str:
    return (s or "").strip().lower()


def sanitize_scanner_input(raw: str) -> str:
    return (raw or "").strip()


# -----------------------------
# "Card" helper (shadow modules)
# -----------------------------

def make_card(title: str, content_widget: QWidget) -> QFrame:
    """
    QGraphicsDropShadowEffect in PySide6 doesn't have setOpacity().
    Use QColor alpha to control shadow strength.
    """
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 14, 14, 14)
    lay.setSpacing(10)

    t = QLabel(title)
    t.setStyleSheet("font-size: 13px; font-weight: 700; color: #333;")
    lay.addWidget(t)
    lay.addWidget(content_widget)

    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(18)
    shadow.setOffset(0, 6)
    shadow.setColor(QColor(0, 0, 0, 35))  # subtle shadow via alpha
    card.setGraphicsEffect(shadow)

    return card


def make_glass_card(title: str, content_widget: QWidget) -> QFrame:
    card = QFrame()
    card.setObjectName("GlassCard")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 18, 18, 18)
    lay.setSpacing(12)

    t = QLabel(title)
    t.setStyleSheet("font-size: 13px; font-weight: 700; color: #1f2a37;")
    lay.addWidget(t)
    lay.addWidget(content_widget)

    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(28)
    shadow.setOffset(0, 10)
    shadow.setColor(QColor(20, 32, 45, 50))
    card.setGraphicsEffect(shadow)

    return card


# -----------------------------
# Database Layer
# -----------------------------
class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('ADMIN','SCANNER')),
            created_at TEXT NOT NULL
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            created_at TEXT NOT NULL
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS skus (
            sku TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            image_path TEXT,
            qty_on_hand INTEGER NOT NULL DEFAULT 0 CHECK(qty_on_hand >= 0),
            par_level INTEGER NOT NULL DEFAULT 0 CHECK(par_level >= 0),
            created_at TEXT NOT NULL
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('IN','OUT')),
            qty INTEGER NOT NULL CHECK(qty > 0),
            serials TEXT,
            note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
            FOREIGN KEY(sku) REFERENCES skus(sku) ON DELETE RESTRICT
        );
        """)

        # Safe migration for existing dbs missing serials
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(transactions);").fetchall()]
        if "serials" not in cols:
            self.conn.execute("ALTER TABLE transactions ADD COLUMN serials TEXT;")

        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_ts ON transactions(ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_sku_ts ON transactions(sku, ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_skus_name ON skus(name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_skus_category ON skus(category);")

        self.conn.commit()

        # Seed categories from existing SKUs
        for row in self.conn.execute(
            "SELECT DISTINCT category FROM skus WHERE category IS NOT NULL AND TRIM(category) != ''"
        ).fetchall():
            self._ensure_category(row["category"])

        # Seed: create default admin if none exists
        if self.count_users() == 0:
            self.create_user("admin", "1234", "ADMIN")

    # --- Settings ---
    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key.strip(),)
        ).fetchone()
        if not row:
            return None
        return row["value"]

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key.strip(), value)
        )
        self.conn.commit()

    # --- Categories ---
    def _ensure_category(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO categories(name, created_at) VALUES(?, ?)",
            (name, now_iso())
        )
        self.conn.commit()

    def list_categories(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT name FROM categories ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [r["name"] for r in rows]

    def add_category(self, name: str) -> Tuple[bool, str]:
        name = (name or "").strip()
        if not name:
            return False, "Category name is required."
        self._ensure_category(name)
        return True, "Category added."

    # --- Users ---
    def count_users(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()["c"])

    def create_user(self, username: str, pin: str, role: str) -> None:
        self.conn.execute(
            "INSERT INTO users(username, pin_hash, role, created_at) VALUES(?,?,?,?)",
            (username.strip(), sha256_hex(pin.strip()), role, now_iso())
        )
        self.conn.commit()

    def update_user_role(self, username: str, role: str) -> None:
        self.conn.execute("UPDATE users SET role=? WHERE username=? COLLATE NOCASE", (role, username))
        self.conn.commit()

    def update_user_pin(self, username: str, new_pin: str) -> None:
        self.conn.execute(
            "UPDATE users SET pin_hash=? WHERE username=? COLLATE NOCASE",
            (sha256_hex(new_pin.strip()), username)
        )
        self.conn.commit()

    def delete_user(self, username: str) -> None:
        self.conn.execute("DELETE FROM users WHERE username=? COLLATE NOCASE", (username,))
        self.conn.commit()

    def list_users(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY username COLLATE NOCASE;"
        ).fetchall()

    def authenticate(self, username: str, pin: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, username, role FROM users WHERE username=? COLLATE NOCASE AND pin_hash=?",
            (username.strip(), sha256_hex(pin.strip()))
        ).fetchone()

    def verify_pin(self, username: str, pin: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM users WHERE username=? COLLATE NOCASE AND pin_hash=?",
            (username.strip(), sha256_hex(pin.strip()))
        ).fetchone()
        return row is not None

    # --- SKUs ---
    def upsert_sku(self, sku: str, name: str, description: str, category: str,
                   image_path: Optional[str], qty_on_hand: int, par_level: int) -> None:
        sku = sku.strip()
        category = category.strip()
        self._ensure_category(category)
        self.conn.execute("""
        INSERT INTO skus(sku, name, description, category, image_path, qty_on_hand, par_level, created_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(sku) DO UPDATE SET
            name=excluded.name,
            description=excluded.description,
            category=excluded.category,
            image_path=excluded.image_path,
            qty_on_hand=excluded.qty_on_hand,
            par_level=excluded.par_level
        """, (sku, name.strip(), description.strip(), category, image_path,
              int(qty_on_hand), int(par_level), now_iso()))
        self.conn.commit()

    def delete_sku(self, sku: str) -> None:
        self.conn.execute("DELETE FROM skus WHERE sku=?", (sku.strip(),))
        self.conn.commit()

    def get_sku(self, sku: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM skus WHERE sku=?", (sku.strip(),)).fetchone()

    def search_skus(self, q: str) -> List[sqlite3.Row]:
        q = (q or "").strip()
        like = f"%{q}%"
        return self.conn.execute("""
        SELECT sku, name, description, category, qty_on_hand, par_level, image_path
        FROM skus
        WHERE sku LIKE ? OR name LIKE ? OR category LIKE ?
        ORDER BY name COLLATE NOCASE
        LIMIT 500
        """, (like, like, like)).fetchall()

    def list_inventory(self, q: str, category: str) -> List[sqlite3.Row]:
        q = (q or "").strip()
        category = (category or "").strip()
        like = f"%{q}%"
        return self.conn.execute("""
        SELECT sku, name, category, qty_on_hand, par_level, image_path
        FROM skus
        WHERE (sku LIKE ? OR name LIKE ? OR category LIKE ?)
          AND (? = '' OR category = ?)
        ORDER BY name COLLATE NOCASE
        LIMIT 1000
        """, (like, like, like, category, category)).fetchall()

    def list_low_stock(self, limit: int = 5) -> List[sqlite3.Row]:
        return self.conn.execute("""
        SELECT sku, name, category, qty_on_hand, par_level
        FROM skus
        WHERE qty_on_hand <= par_level
        ORDER BY (par_level - qty_on_hand) DESC, name COLLATE NOCASE
        LIMIT ?
        """, (limit,)).fetchall()

    # --- Transactions / Inventory moves ---
    def record_move(self, user_id: int, sku: str, direction: str, qty: int,
                    note: str = "", serials: Optional[str] = None) -> Tuple[bool, str]:
        sku = sku.strip()
        qty = int(qty)
        if qty <= 0:
            return False, "Quantity must be > 0."

        row = self.get_sku(sku)
        if row is None:
            return False, f"SKU '{sku}' not found."

        current = int(row["qty_on_hand"])
        if direction == "OUT" and current - qty < 0:
            return False, f"Not enough inventory. On hand: {current}, trying to remove: {qty}."

        new_qty = current + qty if direction == "IN" else current - qty

        cur = self.conn.cursor()
        cur.execute("UPDATE skus SET qty_on_hand=? WHERE sku=?", (new_qty, sku))
        cur.execute("""
            INSERT INTO transactions(ts, user_id, sku, direction, qty, serials, note)
            VALUES(?,?,?,?,?,?,?)
        """, (now_iso(), int(user_id), sku, direction, qty, serials, note.strip() if note else None))
        self.conn.commit()
        return True, f"{direction} OK. New on-hand: {new_qty}."

    def recent_transactions(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute("""
        SELECT t.ts, u.username, t.sku, s.name, t.direction, t.qty, t.serials, t.note
        FROM transactions t
        JOIN users u ON u.id = t.user_id
        JOIN skus s ON s.sku = t.sku
        ORDER BY t.ts DESC
        LIMIT ?
        """, (limit,)).fetchall()

    def top_selling_this_week(self, limit: int = 5) -> List[sqlite3.Row]:
        start = dt.datetime.combine(week_start(), dt.time.min).isoformat(sep=" ")
        return self.conn.execute("""
        SELECT t.sku, s.name, SUM(t.qty) AS out_qty
        FROM transactions t
        JOIN skus s ON s.sku = t.sku
        WHERE t.direction='OUT' AND t.ts >= ?
        GROUP BY t.sku
        ORDER BY out_qty DESC, s.name COLLATE NOCASE
        LIMIT ?
        """, (start, limit)).fetchall()

    def export_transactions_csv(self, path: str) -> None:
        rows = self.conn.execute("""
        SELECT t.id, t.ts, u.username, u.role, t.sku, s.name, t.direction, t.qty, t.serials, t.note
        FROM transactions t
        JOIN users u ON u.id = t.user_id
        JOIN skus s ON s.sku = t.sku
        ORDER BY t.ts DESC
        """).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id","ts","username","role","sku","sku_name","direction","qty","serials","note"])
            for r in rows:
                w.writerow([
                    r["id"], r["ts"], r["username"], r["role"], r["sku"], r["name"],
                    r["direction"], r["qty"], r["serials"] or "", r["note"] or ""
                ])

    # --- CSV import ---
    def import_skus_csv(self, path: str) -> Tuple[int, int, List[str]]:
        created = 0
        updated = 0
        errors: List[str] = []

        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return 0, 0, ["CSV has no headers."]

            fields = {k.lower().strip(): k for k in reader.fieldnames}

            def get(row, key, default=""):
                if key in fields:
                    return (row.get(fields[key]) or "").strip()
                return default

            for i, row in enumerate(reader, start=2):
                sku = get(row, "sku")
                name = get(row, "name")
                description = get(row, "description")
                category = get(row, "category")
                qty = get(row, "qty") or get(row, "qty_on_hand") or "0"
                par = get(row, "par") or get(row, "par_level") or "0"
                image_path = get(row, "image_path") or ""

                if not sku or not name or not description or not category:
                    errors.append(f"Row {i}: Missing required fields (sku/name/description/category).")
                    continue

                try:
                    qty_i = max(0, int(float(qty)))
                    par_i = max(0, int(float(par)))
                except Exception:
                    errors.append(f"Row {i}: qty/par must be numbers.")
                    continue

                existed = self.get_sku(sku) is not None
                self.upsert_sku(
                    sku=sku,
                    name=name,
                    description=description,
                    category=category,
                    image_path=image_path if image_path else None,
                    qty_on_hand=qty_i,
                    par_level=par_i
                )

                if existed:
                    updated += 1
                else:
                    created += 1

        return created, updated, errors


# -----------------------------
# QR Label Generation
# -----------------------------

def generate_qr_label_pdf(output_pdf: str, sku: str, title: str = "", size_in: float = 1.5) -> None:
    if importlib.util.find_spec("qrcode") is None or importlib.util.find_spec("reportlab") is None:
        raise RuntimeError(
            "QR label generation requires the 'qrcode' and 'reportlab' packages. "
            "Install them to enable PDF label export."
        )

    import qrcode
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch

    c = canvas.Canvas(output_pdf, pagesize=(size_in * inch, size_in * inch))

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(sku)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    margin = 0.08 * inch
    qr_size = (size_in * inch) - 2 * margin

    tmp_png = os.path.join(os.getcwd(), "_tmp_qr.png")
    img.save(tmp_png)

    c.drawImage(
        tmp_png,
        margin,
        margin + 0.26 * inch,
        width=qr_size,
        height=qr_size - 0.26 * inch,
        preserveAspectRatio=True,
        mask="auto"
    )

    c.setFont("Helvetica", 7.8)
    c.drawCentredString((size_in * inch) / 2, 0.10 * inch, sku)

    if title.strip():
        c.setFont("Helvetica", 6.5)
        c.drawCentredString((size_in * inch) / 2, 0.18 * inch, title.strip()[:22])

    c.showPage()
    c.save()

    try:
        os.remove(tmp_png)
    except Exception:
        pass


# -----------------------------
# Table polish helpers
# -----------------------------

def polish_table(tbl: QTableWidget, stretch_last: bool = True) -> None:
    tbl.setAlternatingRowColors(True)
    header = tbl.horizontalHeader()
    header.setStretchLastSection(stretch_last)
    header.setSectionResizeMode(QHeaderView.Interactive)
    tbl.verticalHeader().setVisible(False)
    tbl.setShowGrid(False)
    tbl.setWordWrap(False)
    tbl._resizer = _TableResizeWatcher(tbl)
    tbl.viewport().installEventFilter(tbl._resizer)


class _TableResizeWatcher(QObject):
    def __init__(self, table: QTableWidget):
        super().__init__(table)
        self.table = table

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            repolish_table(self.table)
        return False


def repolish_table(tbl: QTableWidget, min_col_px: int = 90) -> None:
    tbl.resizeColumnsToContents()
    visible_cols = []
    for c in range(tbl.columnCount()):
        if tbl.isColumnHidden(c):
            continue
        w = tbl.columnWidth(c)
        if w < min_col_px:
            tbl.setColumnWidth(c, min_col_px)
        visible_cols.append(c)

    if not visible_cols:
        return

    total = sum(tbl.columnWidth(c) for c in visible_cols)
    viewport = tbl.viewport().width()
    if viewport <= total:
        return

    extra = viewport - total
    per_col = extra // len(visible_cols)
    remainder = extra % len(visible_cols)
    for idx, c in enumerate(visible_cols):
        add = per_col + (1 if idx < remainder else 0)
        tbl.setColumnWidth(c, tbl.columnWidth(c) + add)


# -----------------------------
# UI Models
# -----------------------------
@dataclass
class Session:
    user_id: int
    username: str
    role: str  # ADMIN or SCANNER


class PinDialog(QDialog):
    def __init__(self, username: str):
        super().__init__()
        self.username = username
        self.pin_value: Optional[str] = None

        self.setWindowTitle("Confirm PIN")
        self.setModal(True)
        self.setMinimumWidth(340)

        lay = QVBoxLayout(self)
        title = QLabel(f"Re-enter PIN for: <b>{username}</b>")
        lay.addWidget(title)

        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.Password)
        self.pin.setPlaceholderText("4+ digit PIN")
        self.pin.setMaxLength(12)
        self.pin.setInputMethodHints(Qt.ImhDigitsOnly)
        lay.addWidget(self.pin)

        self.hint = QLabel("PIN must be at least 4 digits.")
        self.hint.setStyleSheet("color:#666;")
        lay.addWidget(self.hint)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        lay.addWidget(self.buttons)

        self.ok_btn = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_btn.setEnabled(False)
        self.ok_btn.setDefault(True)
        self.ok_btn.setAutoDefault(True)

        self.buttons.accepted.connect(self._try_accept)
        self.buttons.rejected.connect(self.reject)

        self.pin.textChanged.connect(self._on_text_changed)
        self.pin.returnPressed.connect(self._try_accept)

        QTimer.singleShot(0, self.pin.setFocus)

    def _on_text_changed(self, txt: str):
        txt = (txt or "").strip()
        self.ok_btn.setEnabled(txt.isdigit() and len(txt) >= 4)

    def _try_accept(self):
        p = (self.pin.text() or "").strip()
        if not (p.isdigit() and len(p) >= 4):
            QMessageBox.warning(self, "Invalid PIN", "Enter at least 4 digits (numbers only).")
            return
        self.pin_value = p
        self.accept()


class LoginDialog(QDialog):
    """
    FIXED:
    - Pressing Enter will NOT close the dialog unless login succeeds.
    - Enter triggers try_login.
    - Default button is Login.
    - Digits-only PIN with 4+ length (no input mask).
    """
    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.session: Optional[Session] = None

        self.setWindowTitle(f"{APP_TITLE} — Login")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        title = QLabel("Sign in")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        form = QFormLayout()
        self.user = QLineEdit()
        self.user.setPlaceholderText("admin")
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.Password)
        self.pin.setPlaceholderText("PIN (4+ digits)")
        self.pin.setMaxLength(12)
        self.pin.setInputMethodHints(Qt.ImhDigitsOnly)

        form.addRow("Username", self.user)
        form.addRow("PIN", self.pin)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        self.login_btn = QPushButton("Login")
        self.login_btn.setDefault(True)
        self.login_btn.setAutoDefault(True)
        self.login_btn.clicked.connect(self.try_login)

        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.login_btn)
        layout.addLayout(btns)

        hint = QLabel("Tip: First run creates admin / 1234")
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)

        # Enter triggers login attempt
        self.user.returnPressed.connect(self.try_login)
        self.pin.returnPressed.connect(self.try_login)

        QTimer.singleShot(0, self.user.setFocus)

    def accept(self):
        # Prevent "Enter = accept = close dialog = exit app"
        self.try_login()

    def try_login(self):
        username = (self.user.text() or "").strip()
        pin = (self.pin.text() or "").strip()

        if not username or not (pin.isdigit() and len(pin) >= 4):
            QMessageBox.warning(self, "Missing info", "Enter username and a 4+ digit PIN (numbers only).")
            return

        row = self.db.authenticate(username, pin)
        if not row:
            QMessageBox.critical(self, "Nope", "Invalid username or PIN.")
            self.pin.selectAll()
            self.pin.setFocus()
            return

        self.session = Session(user_id=int(row["id"]), username=row["username"], role=row["role"])
        super().accept()


class SerialPrompt(QDialog):
    def __init__(self, sku: str, name: str, qty: int):
        super().__init__()
        self.serials: Optional[str] = None
        self.setWindowTitle("Serial Numbers Required")
        self.setModal(True)
        self.setMinimumWidth(520)

        lay = QVBoxLayout()
        lay.addWidget(QLabel(f"SKU: <b>{sku}</b> — {name}"))
        lay.addWidget(QLabel(f"Quantity: <b>{qty}</b> (need {qty} serial number(s))"))
        lay.addWidget(QLabel("Enter serials separated by commas or new lines:"))

        self.text = QTextEdit()
        self.text.setFixedHeight(130)
        lay.addWidget(self.text)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.setLayout(lay)

    def _ok(self):
        raw = (self.text.toPlainText() or "").strip()
        if not raw:
            QMessageBox.warning(self, "Missing", "Serial numbers are required for this item.")
            return
        parts = []
        for line in raw.replace(",", "\n").splitlines():
            s = line.strip()
            if s:
                parts.append(s)
        if len(parts) == 0:
            QMessageBox.warning(self, "Missing", "Serial numbers are required for this item.")
            return
        self.serials = ", ".join(parts)
        self.accept()


class ColumnManagerDialog(QDialog):
    def __init__(self, parent: QWidget, columns: List[str], hidden: List[str]):
        super().__init__(parent)
        self.setWindowTitle("Manage Columns")
        self.setModal(True)
        self.setMinimumWidth(320)

        self.columns = list(columns)
        self.hidden = set(hidden)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Show or hide columns:"))

        self.list_widget = QListWidget()
        for col in self.columns:
            item = QListWidgetItem(col)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if col in self.hidden else Qt.Checked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Add Column")
        self.del_btn = QPushButton("Delete Column")
        self.close_btn = QPushButton("Close")
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.del_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self.add_btn.clicked.connect(self.add_column)
        self.del_btn.clicked.connect(self.delete_selected)
        self.close_btn.clicked.connect(self.accept)

    def add_column(self):
        name, ok = QInputDialog.getText(self, "Add Column", "Column name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, "Missing", "Column name is required.")
            return
        if name in self.columns:
            QMessageBox.warning(self, "Exists", "That column already exists.")
            return
        self.columns.append(name)
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.list_widget.addItem(item)

    def delete_selected(self):
        items = self.list_widget.selectedItems()
        if not items:
            QMessageBox.information(self, "Select a column", "Select a column to delete.")
            return
        for item in items:
            name = item.text()
            if name in self.columns:
                self.columns.remove(name)
            if name in self.hidden:
                self.hidden.remove(name)
            row = self.list_widget.row(item)
            self.list_widget.takeItem(row)

    def results(self) -> Tuple[List[str], List[str]]:
        hidden = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() != Qt.Checked:
                hidden.append(item.text())
        return self.columns, hidden


class ScanWidget(QWidget):
    def __init__(self, db: DB, session: Session, direction: str, refresh_callbacks: list):
        super().__init__()
        self.db = db
        self.session = session
        self.direction = direction
        self.refresh_callbacks = refresh_callbacks

        root = QVBoxLayout()

        header = QLabel("Scan In" if direction == "IN" else "Scan Out")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        content = QWidget()
        box_layout = QGridLayout(content)
        box_layout.setHorizontalSpacing(12)
        box_layout.setVerticalSpacing(10)

        self.qty = QSpinBox()
        self.qty.setRange(1, 9999)
        self.qty.setValue(1)

        self.scan_input = QLineEdit()
        self.scan_input.setPlaceholderText("Click here, then scan a QR code…")
        self.scan_input.returnPressed.connect(self.on_scan_enter)

        self.webcam_btn = QPushButton("Scan with Webcam")
        self.webcam_btn.clicked.connect(self.scan_with_webcam)

        self.note = QLineEdit()
        self.note.setPlaceholderText("(Optional note)")

        box_layout.addWidget(QLabel("Quantity"), 0, 0)
        box_layout.addWidget(self.qty, 0, 1)
        box_layout.addWidget(QLabel("Scan field"), 1, 0)
        box_layout.addWidget(self.scan_input, 1, 1)
        box_layout.addWidget(self.webcam_btn, 2, 1)
        box_layout.addWidget(QLabel("Note"), 3, 0)
        box_layout.addWidget(self.note, 3, 1)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        box_layout.addWidget(self.result, 4, 0, 1, 2)

        root.addWidget(make_card("Scanner", content))

        tips = QLabel("Tip: USB/Bluetooth scanners act like a keyboard + Enter. Keep focus in the scan field.")
        tips.setStyleSheet("color:#666;")
        root.addWidget(tips)
        root.addStretch(1)

        self.setLayout(root)
        QTimer.singleShot(200, self.scan_input.setFocus)

    def on_scan_enter(self):
        raw = sanitize_scanner_input(self.scan_input.text())
        self.scan_input.clear()
        self.scan_input.setFocus()

        if not raw:
            return

        qty = int(self.qty.value())
        note = self.note.text().strip()

        sku_row = self.db.get_sku(raw)
        if not sku_row:
            self.result.setStyleSheet("color: #b42318; font-weight: 600;")
            self.result.setText(f"SKU '{raw}' not found.")
            return

        serials_to_store: Optional[str] = None
        if norm(sku_row["category"]) == FIREPLACE_CATEGORY_NAME:
            dlg = SerialPrompt(raw, sku_row["name"], qty)
            if dlg.exec() != QDialog.Accepted or not dlg.serials:
                self.result.setStyleSheet("color: #b42318; font-weight: 600;")
                self.result.setText("Scan cancelled: serial numbers required.")
                return

            parts = [p.strip() for p in dlg.serials.replace(",", "\n").splitlines() if p.strip()]
            if len(parts) != qty:
                QMessageBox.warning(
                    self, "Serial count mismatch",
                    f"You scanned quantity {qty} but entered {len(parts)} serial(s).\n\n"
                    f"Enter exactly {qty} serial(s) for fireplaces."
                )
                self.result.setStyleSheet("color: #b42318; font-weight: 600;")
                self.result.setText("Scan cancelled: serial count mismatch.")
                return

            serials_to_store = ", ".join(parts)

        ok, msg = self.db.record_move(
            self.session.user_id, raw, self.direction, qty,
            note=note, serials=serials_to_store
        )

        if ok:
            self.result.setStyleSheet("color: #1a7f37; font-weight: 600;")
            self.note.clear()
        else:
            self.result.setStyleSheet("color: #b42318; font-weight: 600;")
        self.result.setText(msg)

        for cb in self.refresh_callbacks:
            cb()

    def scan_with_webcam(self):
        if importlib.util.find_spec("cv2") is None:
            QMessageBox.warning(
                self,
                "Webcam scanner unavailable",
                "OpenCV (cv2) is not installed. Install it to enable webcam QR scanning."
            )
            return
        import cv2

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            QMessageBox.warning(self, "Webcam error", "Could not access the webcam.")
            return

        detector = cv2.QRCodeDetector()
        cv2.namedWindow("Scan QR (press q to cancel)")

        decoded = None
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            data, points, _ = detector.detectAndDecode(frame)
            if points is not None:
                pts = points[0] if len(points.shape) == 3 else points
                for i in range(len(pts)):
                    pt1 = tuple(pts[i].astype(int))
                    pt2 = tuple(pts[(i + 1) % len(pts)].astype(int))
                    cv2.line(frame, pt1, pt2, (0, 255, 0), 2)
            cv2.imshow("Scan QR (press q to cancel)", frame)
            if data:
                decoded = data.strip()
                break
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        cap.release()
        cv2.destroyAllWindows()

        if decoded:
            self.scan_input.setText(decoded)
            self.on_scan_enter()


class DashboardWidget(QWidget):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db

        root = QVBoxLayout()
        header = QLabel("Today")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        self.low_tbl = QTableWidget(0, 5)
        self.low_tbl.setHorizontalHeaderLabels(["SKU", "Name", "Category", "On Hand", "Par"])
        self.low_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.low_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.low_tbl, stretch_last=True)

        self.recent_tbl = QTableWidget(0, 7)
        self.recent_tbl.setHorizontalHeaderLabels(["When", "User", "SKU", "Name", "Dir", "Qty", "Serials"])
        self.recent_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.recent_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.recent_tbl, stretch_last=True)

        self.top_tbl = QTableWidget(0, 3)
        self.top_tbl.setHorizontalHeaderLabels(["SKU", "Name", "Out Qty (This Week)"])
        self.top_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.top_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.top_tbl, stretch_last=True)

        top_row = QHBoxLayout()
        top_row.setSpacing(18)
        top_row.addWidget(make_card("Top 5 Low Stock (On Hand ≤ Par)", self.low_tbl), 1)
        top_row.addWidget(make_card("Recent Scans", self.recent_tbl), 2)

        root.addLayout(top_row)
        root.addSpacing(14)
        root.addWidget(make_card("Top 5 Selling (Scanned Out This Week)", self.top_tbl))

        root.addStretch(1)
        self.setLayout(root)

        self.refresh()

    def refresh(self):
        lows = self.db.list_low_stock(5)
        self.low_tbl.setRowCount(0)
        for r in lows:
            row = self.low_tbl.rowCount()
            self.low_tbl.insertRow(row)
            self.low_tbl.setItem(row, 0, QTableWidgetItem(r["sku"]))
            self.low_tbl.setItem(row, 1, QTableWidgetItem(r["name"]))
            self.low_tbl.setItem(row, 2, QTableWidgetItem(r["category"]))
            self.low_tbl.setItem(row, 3, QTableWidgetItem(str(r["qty_on_hand"])))
            self.low_tbl.setItem(row, 4, QTableWidgetItem(str(r["par_level"])))
        repolish_table(self.low_tbl)

        rec = self.db.recent_transactions(20)
        self.recent_tbl.setRowCount(0)
        for r in rec:
            row = self.recent_tbl.rowCount()
            self.recent_tbl.insertRow(row)
            self.recent_tbl.setItem(row, 0, QTableWidgetItem(r["ts"]))
            self.recent_tbl.setItem(row, 1, QTableWidgetItem(r["username"]))
            self.recent_tbl.setItem(row, 2, QTableWidgetItem(r["sku"]))
            self.recent_tbl.setItem(row, 3, QTableWidgetItem(r["name"]))
            self.recent_tbl.setItem(row, 4, QTableWidgetItem(r["direction"]))
            self.recent_tbl.setItem(row, 5, QTableWidgetItem(str(r["qty"])))
            self.recent_tbl.setItem(row, 6, QTableWidgetItem(r["serials"] or ""))
        repolish_table(self.recent_tbl)

        top = self.db.top_selling_this_week(5)
        self.top_tbl.setRowCount(0)
        for r in top:
            row = self.top_tbl.rowCount()
            self.top_tbl.insertRow(row)
            self.top_tbl.setItem(row, 0, QTableWidgetItem(r["sku"]))
            self.top_tbl.setItem(row, 1, QTableWidgetItem(r["name"]))
            self.top_tbl.setItem(row, 2, QTableWidgetItem(str(r["out_qty"])))
        repolish_table(self.top_tbl)


class TextMessageGeneratorWidget(QWidget):
    def __init__(self):
        super().__init__()

        root = QVBoxLayout()
        header = QLabel("Text Message Generator")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        form_widget = QWidget()
        form_widget.setObjectName("GlassPanel")
        form_layout = QFormLayout(form_widget)
        form_layout.setLabelAlignment(Qt.AlignLeft)
        form_layout.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form_layout.setHorizontalSpacing(14)
        form_layout.setVerticalSpacing(10)

        self.business_name = QLineEdit()
        self.business_name.setObjectName("GlassInput")
        self.business_name.setPlaceholderText("e.g., Postfully")

        self.recipient_name = QLineEdit()
        self.recipient_name.setObjectName("GlassInput")
        self.recipient_name.setPlaceholderText("Optional recipient name")

        self.offer = QLineEdit()
        self.offer.setObjectName("GlassInput")
        self.offer.setPlaceholderText("e.g., 20% off, free demo, new update")

        self.call_to_action = QLineEdit()
        self.call_to_action.setObjectName("GlassInput")
        self.call_to_action.setPlaceholderText("e.g., Reply YES, Book a time, Shop now")

        self.link = QLineEdit()
        self.link.setObjectName("GlassInput")
        self.link.setPlaceholderText("Optional link")

        self.sender_name = QLineEdit()
        self.sender_name.setObjectName("GlassInput")
        self.sender_name.setPlaceholderText("e.g., Jamie from Postfully")

        self.tone = QComboBox()
        self.tone.setObjectName("GlassSelect")
        self.tone.addItems(["Friendly", "Professional", "Casual", "Urgent", "Promotional"])

        self.message_type = QComboBox()
        self.message_type.setObjectName("GlassSelect")
        self.message_type.addItems(["Promotion", "Reminder", "Announcement", "Follow-up"])

        self.length = QComboBox()
        self.length.setObjectName("GlassSelect")
        self.length.addItems(["Short", "Standard", "Detailed"])

        form_layout.addRow("Business name", self.business_name)
        form_layout.addRow("Recipient name", self.recipient_name)
        form_layout.addRow("Offer or update", self.offer)
        form_layout.addRow("Call to action", self.call_to_action)
        form_layout.addRow("Link", self.link)
        form_layout.addRow("Sender name", self.sender_name)
        form_layout.addRow("Tone", self.tone)
        form_layout.addRow("Message type", self.message_type)
        form_layout.addRow("Length", self.length)

        root.addWidget(make_glass_card("Details", form_widget))

        output_widget = QWidget()
        output_widget.setObjectName("GlassPanel")
        output_layout = QVBoxLayout(output_widget)
        output_layout.setContentsMargins(0, 0, 0, 0)

        self.output = QTextEdit()
        self.output.setObjectName("GlassOutput")
        self.output.setPlaceholderText("Generated message will appear here.")
        self.output.setReadOnly(True)
        output_layout.addWidget(self.output)

        button_row = QHBoxLayout()
        self.generate_btn = QPushButton("Generate Message")
        self.generate_btn.setObjectName("GlassPrimaryButton")
        self.copy_btn = QPushButton("Copy to Clipboard")
        self.copy_btn.setObjectName("GlassButton")
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("GlassButton")
        button_row.addWidget(self.generate_btn)
        button_row.addWidget(self.copy_btn)
        button_row.addWidget(self.reset_btn)
        button_row.addStretch(1)
        output_layout.addLayout(button_row)

        root.addWidget(make_glass_card("Message", output_widget))
        root.addStretch(1)

        self.setLayout(root)

        self.generate_btn.clicked.connect(self.generate_message)
        self.copy_btn.clicked.connect(self.copy_message)
        self.reset_btn.clicked.connect(self.reset_form)

    def _build_message_parts(self) -> Tuple[str, str, str]:
        tone = self.tone.currentText().lower()
        message_type = self.message_type.currentText().lower()
        length = self.length.currentText().lower()

        greeting = "Hi there"
        name = self.recipient_name.text().strip()
        if name:
            greeting = f"Hi {name}"

        business = self.business_name.text().strip()
        offer = self.offer.text().strip()
        cta = self.call_to_action.text().strip()

        intro_bits = []
        if message_type == "promotion":
            intro_bits.append("We've got something special for you" if tone != "professional" else "We have a new offer available")
        elif message_type == "reminder":
            intro_bits.append("Just a quick reminder" if tone != "urgent" else "Important reminder")
        elif message_type == "announcement":
            intro_bits.append("Wanted to share an update" if tone != "casual" else "Quick update for you")
        else:
            intro_bits.append("Following up" if tone != "casual" else "Just checking in")

        if business:
            intro_bits.append(f"from {business}")

        if offer:
            if message_type == "reminder":
                intro_bits.append(f"about {offer}")
            else:
                intro_bits.append(f": {offer}")

        body = " ".join(intro_bits).replace(" :", ":")
        if length == "detailed":
            extra = "Let me know if you'd like more details." if tone != "urgent" else "Time-sensitive, so please take a look."
            body = f"{body} {extra}".strip()
        elif length == "short":
            body = body.split(".")[0]

        cta_line = ""
        if cta:
            cta_line = f"{cta}."
        return greeting, body, cta_line

    def generate_message(self):
        greeting, body, cta_line = self._build_message_parts()

        link = self.link.text().strip()
        sender = self.sender_name.text().strip()

        parts = [f"{greeting},"]
        if body:
            parts.append(body)
        if cta_line:
            parts.append(cta_line)
        if link:
            parts.append(link)
        if sender:
            parts.append(f"- {sender}")

        message = " ".join([p for p in parts if p]).strip()
        self.output.setPlainText(message)

    def copy_message(self):
        text = self.output.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Nothing to copy", "Generate a message first.")
            return
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Copied", "Message copied to clipboard.")

    def reset_form(self):
        self.business_name.clear()
        self.recipient_name.clear()
        self.offer.clear()
        self.call_to_action.clear()
        self.link.clear()
        self.sender_name.clear()
        self.tone.setCurrentIndex(0)
        self.message_type.setCurrentIndex(0)
        self.length.setCurrentIndex(1)
        self.output.clear()


class InventoryWidget(QWidget):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db

        root = QVBoxLayout()
        header = QLabel("Inventory")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by SKU, name, or category…")
        self.search.textChanged.connect(self.refresh)

        self.category_filter = QComboBox()
        self.category_filter.currentTextChanged.connect(self.refresh)

        filter_row.addWidget(self.search, 2)
        filter_row.addWidget(QLabel("Category"))
        filter_row.addWidget(self.category_filter, 1)
        root.addLayout(filter_row)

        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(["SKU", "Name", "Category", "On Hand", "Par", "Image"])
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.tbl, stretch_last=True)
        root.addWidget(self.tbl)

        self.setLayout(root)
        self.refresh_categories()
        self.refresh()

    def refresh_categories(self):
        categories = self.db.list_categories()
        current = self.category_filter.currentText()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All")
        for cat in categories:
            self.category_filter.addItem(cat)
        if current:
            idx = self.category_filter.findText(current)
            if idx >= 0:
                self.category_filter.setCurrentIndex(idx)
        self.category_filter.blockSignals(False)

    def refresh(self):
        q = self.search.text().strip()
        cat = self.category_filter.currentText()
        if cat == "All":
            cat = ""
        rows = self.db.list_inventory(q, cat)
        self.tbl.setRowCount(0)
        for r in rows:
            row = self.tbl.rowCount()
            self.tbl.insertRow(row)
            self.tbl.setItem(row, 0, QTableWidgetItem(r["sku"]))
            self.tbl.setItem(row, 1, QTableWidgetItem(r["name"]))
            self.tbl.setItem(row, 2, QTableWidgetItem(r["category"]))
            self.tbl.setItem(row, 3, QTableWidgetItem(str(r["qty_on_hand"])))
            self.tbl.setItem(row, 4, QTableWidgetItem(str(r["par_level"])))
            self.tbl.setItem(row, 5, QTableWidgetItem(r["image_path"] or ""))
        repolish_table(self.tbl)


class SkuManagerWidget(QWidget):
    def __init__(self, db: DB, is_admin: bool, refresh_callbacks: list):
        super().__init__()
        self.db = db
        self.is_admin = is_admin
        self.refresh_callbacks = refresh_callbacks
        self.selected_sku: Optional[str] = None
        self.columns = ["SKU", "Name", "Category", "On Hand", "Par"]
        self.hidden_columns: List[str] = []

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)

        header = QLabel("SKUs")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        left_lay.addWidget(header)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by SKU, name, or category…")
        self.search.textChanged.connect(self.refresh_table)
        search_row.addWidget(self.search)

        self.import_btn = QPushButton("Import CSV")
        self.import_btn.clicked.connect(self.import_csv)
        self.export_lbl_btn = QPushButton("Generate QR Label")
        self.export_lbl_btn.clicked.connect(self.generate_label)
        search_row.addWidget(self.import_btn)
        search_row.addWidget(self.export_lbl_btn)
        left_lay.addLayout(search_row)

        self.tbl = QTableWidget(0, len(self.columns))
        self.tbl.setHorizontalHeaderLabels(self.columns)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.itemSelectionChanged.connect(self.on_select)
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self.open_column_manager)
        polish_table(self.tbl, stretch_last=True)
        left_lay.addWidget(self.tbl)

        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)

        right_header = QLabel("Preview + Quick Edit")
        right_header.setStyleSheet("font-size: 16px; font-weight: 700;")
        right_lay.addWidget(right_header)

        self.img_preview = QLabel("No image")
        self.img_preview.setAlignment(Qt.AlignCenter)
        self.img_preview.setMinimumHeight(220)
        self.img_preview.setStyleSheet("background:#fbfbfc; border: 1px solid #e6e6e8; border-radius: 12px; color:#777;")
        right_lay.addWidget(make_card("Image", self.img_preview))

        quick = QWidget()
        form = QFormLayout(quick)

        self.sku = QLineEdit()
        self.name = QLineEdit()
        self.category = QComboBox()
        self.category.setEditable(True)

        self.add_category_btn = QPushButton("Add Category")
        self.add_category_btn.clicked.connect(self.add_category)
        cat_row = QHBoxLayout()
        cat_row.addWidget(self.category)
        cat_row.addWidget(self.add_category_btn)

        self.desc = QTextEdit()
        self.desc.setFixedHeight(90)

        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("(Optional image path)")
        self.browse_img = QPushButton("Browse…")
        self.browse_img.clicked.connect(self.pick_image)

        img_row = QHBoxLayout()
        img_row.addWidget(self.image_path)
        img_row.addWidget(self.browse_img)

        self.qty = QSpinBox()
        self.qty.setRange(0, 10_000_000)
        self.par = QSpinBox()
        self.par.setRange(0, 10_000_000)

        form.addRow("SKU", self.sku)
        form.addRow("Name", self.name)
        form.addRow("Category", cat_row)
        form.addRow("Description", self.desc)
        form.addRow("Image", img_row)
        form.addRow("On Hand", self.qty)
        form.addRow("Par Level", self.par)

        right_lay.addWidget(make_card("Details", quick))

        btns = QHBoxLayout()
        self.save_btn = QPushButton("Save (Add/Update)")
        self.save_btn.clicked.connect(self.save_sku)
        self.delete_btn = QPushButton("Delete SKU")
        self.delete_btn.clicked.connect(self.delete_sku)
        btns.addWidget(self.save_btn)
        btns.addWidget(self.delete_btn)
        btns.addStretch(1)
        right_lay.addLayout(btns)

        if not self.is_admin:
            self.save_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            self.import_btn.setEnabled(False)
            self.add_category_btn.setEnabled(False)

        right_lay.addStretch(1)
        splitter.addWidget(right)
        splitter.setSizes([750, 450])

        root = QVBoxLayout()
        root.addWidget(splitter)
        self.setLayout(root)

        self.refresh_categories()
        self.refresh_table()

    def refresh_categories(self):
        categories = self.db.list_categories()
        current = self.category.currentText()
        self.category.blockSignals(True)
        self.category.clear()
        for cat in categories:
            self.category.addItem(cat)
        if current:
            self.category.setCurrentText(current)
        self.category.blockSignals(False)

    def refresh_table(self):
        q = self.search.text().strip()
        rows = self.db.search_skus(q) if q else self.db.search_skus("")
        hidden_by_name = {self.columns[i]: self.tbl.isColumnHidden(i) for i in range(self.tbl.columnCount())}
        self.hidden_columns = [name for name, is_hidden in hidden_by_name.items() if is_hidden]
        self.tbl.setColumnCount(len(self.columns))
        self.tbl.setHorizontalHeaderLabels(self.columns)
        self.tbl.setRowCount(0)
        for r in rows:
            row = self.tbl.rowCount()
            self.tbl.insertRow(row)
            data = {
                "SKU": r["sku"],
                "Name": r["name"],
                "Category": r["category"],
                "On Hand": str(r["qty_on_hand"]),
                "Par": str(r["par_level"])
            }
            for col_idx, col_name in enumerate(self.columns):
                value = data.get(col_name, "")
                self.tbl.setItem(row, col_idx, QTableWidgetItem(value))
        for i, name in enumerate(self.columns):
            self.tbl.setColumnHidden(i, name in self.hidden_columns)
        repolish_table(self.tbl)

    def open_column_manager(self, pos):
        dlg = ColumnManagerDialog(self, self.columns, self.hidden_columns)
        if dlg.exec() != QDialog.Accepted:
            return
        columns, hidden = dlg.results()
        self.columns = columns
        self.hidden_columns = hidden
        self.refresh_table()

    def _set_preview_image(self, path: Optional[str]):
        resolved = self._resolve_image_path(path)
        if not resolved:
            self.img_preview.setText("No image")
            self.img_preview.setPixmap(QPixmap())
            return
        pix = self._load_pixmap(resolved)
        if pix.isNull():
            self.img_preview.setText("Unsupported image")
            self.img_preview.setPixmap(QPixmap())
            return
        scaled = pix.scaled(380, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.img_preview.setText("")
        self.img_preview.setPixmap(scaled)

    def on_select(self):
        items = self.tbl.selectedItems()
        if not items:
            self.selected_sku = None
            return
        sku = items[0].text()
        self.selected_sku = sku
        row = self.db.get_sku(sku)
        if not row:
            return
        self.sku.setText(row["sku"])
        self.name.setText(row["name"])
        self.category.setCurrentText(row["category"])
        self.desc.setPlainText(row["description"])
        self.image_path.setText(row["image_path"] or "")
        self.qty.setValue(int(row["qty_on_hand"]))
        self.par.setValue(int(row["par_level"]))
        self._set_preview_image(row["image_path"])

    def pick_image(self):
        start_dir = self.db.get_setting("image_base_dir") or ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            start_dir,
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.image_path.setText(path)
            self._set_preview_image(path)

    def _resolve_image_path(self, path: Optional[str]) -> Optional[str]:
        raw = (path or "").strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://")):
            return raw
        if os.path.isabs(raw) and os.path.exists(raw):
            return raw
        base_dir = self.db.get_setting("image_base_dir")
        if base_dir:
            candidate = os.path.join(base_dir, raw)
            if os.path.exists(candidate):
                return candidate
        if os.path.exists(raw):
            return raw
        return None

    def _load_pixmap(self, path: str) -> QPixmap:
        if path.startswith(("http://", "https://")):
            try:
                with urllib.request.urlopen(path, timeout=5) as resp:
                    data = resp.read()
            except Exception:
                return QPixmap()
            pix = QPixmap()
            pix.loadFromData(data)
            return pix
        return QPixmap(path)

    def add_category(self):
        name, ok = QInputDialog.getText(self, "New Category", "Category name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, "Missing", "Category name is required.")
            return
        self.db.add_category(name)
        self.refresh_categories()
        self.category.setCurrentText(name)

    def save_sku(self):
        sku = self.sku.text().strip()
        name = self.name.text().strip()
        cat = self.category.currentText().strip()
        desc = self.desc.toPlainText().strip()
        img = self.image_path.text().strip() or None
        qty = int(self.qty.value())
        par = int(self.par.value())

        if not sku or not name or not cat or not desc:
            QMessageBox.warning(self, "Missing fields", "SKU, Name, Category, and Description are required.")
            return

        self.db.upsert_sku(sku, name, desc, cat, img, qty, par)
        self.refresh_categories()
        self.refresh_table()
        for cb in self.refresh_callbacks:
            cb()

    def delete_sku(self):
        sku = self.sku.text().strip()
        if not sku:
            return
        confirm = QMessageBox.question(
            self, "Delete SKU",
            f"Delete SKU '{sku}' permanently?\n\nTransactions referencing this SKU will prevent deletion.",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.db.delete_sku(sku)
        except sqlite3.IntegrityError:
            QMessageBox.critical(self, "Can't delete", "This SKU has transactions. MVP keeps history.")
            return

        self.refresh_table()
        self.sku.clear(); self.name.clear(); self.category.setCurrentText(""); self.desc.clear()
        self.image_path.clear(); self.qty.setValue(0); self.par.setValue(0)
        self._set_preview_image(None)
        for cb in self.refresh_callbacks:
            cb()

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import SKUs CSV", "", "CSV (*.csv)")
        if not path:
            return
        created, updated, errors = self.db.import_skus_csv(path)
        msg = f"Imported.\nCreated: {created}\nUpdated: {updated}"
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n- " + "\n- ".join(errors[:15])
            if len(errors) > 15:
                msg += f"\n...and {len(errors)-15} more"
        QMessageBox.information(self, "CSV Import", msg)
        self.refresh_categories()
        self.refresh_table()
        for cb in self.refresh_callbacks:
            cb()

    def generate_label(self):
        sku = self.sku.text().strip()
        if not sku:
            QMessageBox.warning(self, "No SKU", "Select or enter a SKU first.")
            return
        row = self.db.get_sku(sku)
        if not row:
            QMessageBox.warning(self, "Unknown SKU", "That SKU does not exist yet.")
            return
        out_path, _ = QFileDialog.getSaveFileName(self, "Save QR Label PDF", f"{sku}_label.pdf", "PDF (*.pdf)")
        if not out_path:
            return
        try:
            generate_qr_label_pdf(out_path, sku=sku, title=row["name"], size_in=1.5)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Missing dependency", str(exc))
            return
        QMessageBox.information(self, "Label created", f"Saved:\n{out_path}")


class TransactionsWidget(QWidget):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db

        root = QVBoxLayout()
        header = QLabel("Transactions")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        self.tbl = QTableWidget(0, 8)
        self.tbl.setHorizontalHeaderLabels(["When", "User", "SKU", "Name", "Dir", "Qty", "Serials", "Note"])
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.tbl, stretch_last=True)
        root.addWidget(self.tbl)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(self.refresh_btn)

        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export_csv)
        btn_row.addWidget(self.export_btn)

        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.setLayout(root)
        self.refresh()

    def refresh(self):
        rows = self.db.recent_transactions(300)
        self.tbl.setRowCount(0)
        for r in rows:
            row = self.tbl.rowCount()
            self.tbl.insertRow(row)
            self.tbl.setItem(row, 0, QTableWidgetItem(r["ts"]))
            self.tbl.setItem(row, 1, QTableWidgetItem(r["username"]))
            self.tbl.setItem(row, 2, QTableWidgetItem(r["sku"]))
            self.tbl.setItem(row, 3, QTableWidgetItem(r["name"]))
            self.tbl.setItem(row, 4, QTableWidgetItem(r["direction"]))
            self.tbl.setItem(row, 5, QTableWidgetItem(str(r["qty"])))
            self.tbl.setItem(row, 6, QTableWidgetItem(r["serials"] or ""))
            self.tbl.setItem(row, 7, QTableWidgetItem(r["note"] or ""))
        repolish_table(self.tbl)

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Transactions CSV", "transactions.csv", "CSV (*.csv)")
        if not path:
            return
        self.db.export_transactions_csv(path)
        QMessageBox.information(self, "Exported", f"Saved:\n{path}")


class UsersWidget(QWidget):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db

        root = QVBoxLayout()
        header = QLabel("Users (Admin)")
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(header)

        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["Username", "Role", "Created"])
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        polish_table(self.tbl, stretch_last=True)
        root.addWidget(self.tbl)

        form = QFormLayout()
        self.u_user = QLineEdit()
        self.u_pin = QLineEdit()
        self.u_pin.setEchoMode(QLineEdit.Password)
        self.u_pin.setPlaceholderText("4+ digits")
        self.u_pin.setMaxLength(12)
        self.u_pin.setInputMethodHints(Qt.ImhDigitsOnly)

        self.u_role = QComboBox()
        self.u_role.addItems(["ADMIN", "SCANNER"])

        form.addRow("Username", self.u_user)
        form.addRow("PIN", self.u_pin)
        form.addRow("Role", self.u_role)
        root.addLayout(form)

        btns = QHBoxLayout()
        self.add_btn = QPushButton("Create User")
        self.add_btn.clicked.connect(self.add_user)
        self.pin_btn = QPushButton("Reset PIN")
        self.pin_btn.clicked.connect(self.reset_pin)
        self.role_btn = QPushButton("Change Role")
        self.role_btn.clicked.connect(self.change_role)
        self.del_btn = QPushButton("Delete User")
        self.del_btn.clicked.connect(self.delete_user)

        btns.addWidget(self.add_btn)
        btns.addWidget(self.pin_btn)
        btns.addWidget(self.role_btn)
        btns.addWidget(self.del_btn)
        btns.addStretch(1)
        root.addLayout(btns)

        self.setLayout(root)
        self.refresh()

    def refresh(self):
        rows = self.db.list_users()
        self.tbl.setRowCount(0)
        for r in rows:
            row = self.tbl.rowCount()
            self.tbl.insertRow(row)
            self.tbl.setItem(row, 0, QTableWidgetItem(r["username"]))
            self.tbl.setItem(row, 1, QTableWidgetItem(r["role"]))
            self.tbl.setItem(row, 2, QTableWidgetItem(r["created_at"]))
        repolish_table(self.tbl)

    def _selected_username(self) -> Optional[str]:
        items = self.tbl.selectedItems()
        if not items:
            return None
        return items[0].text()

    def add_user(self):
        u = (self.u_user.text() or "").strip()
        p = (self.u_pin.text() or "").strip()
        role = self.u_role.currentText()
        if not u or not (p.isdigit() and len(p) >= 4):
            QMessageBox.warning(self, "Missing", "Enter username and a 4+ digit PIN (numbers only).")
            return
        try:
            self.db.create_user(u, p, role)
        except sqlite3.IntegrityError:
            QMessageBox.critical(self, "Exists", "That username already exists.")
            return
        self.u_user.clear()
        self.u_pin.clear()
        self.refresh()

    def reset_pin(self):
        u = self._selected_username() or (self.u_user.text() or "").strip()
        p = (self.u_pin.text() or "").strip()
        if not u or not (p.isdigit() and len(p) >= 4):
            QMessageBox.warning(self, "Missing", "Select a user and enter a 4+ digit PIN (numbers only).")
            return
        self.db.update_user_pin(u, p)
        self.refresh()

    def change_role(self):
        u = self._selected_username() or (self.u_user.text() or "").strip()
        if not u:
            QMessageBox.warning(self, "Missing", "Select a user.")
            return
        role = self.u_role.currentText()
        self.db.update_user_role(u, role)
        self.refresh()

    def delete_user(self):
        u = self._selected_username() or (self.u_user.text() or "").strip()
        if not u:
            return
        confirm = QMessageBox.question(self, "Delete user", f"Delete '{u}'?", QMessageBox.Yes | QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        try:
            self.db.delete_user(u)
        except sqlite3.IntegrityError:
            QMessageBox.critical(self, "Can't delete", "User has transactions. MVP keeps history.")
        self.refresh()


class MainWindow(QMainWindow):
    def __init__(self, db: DB, session: Session):
        super().__init__()
        self.db = db
        self.session = session
        self.setWindowTitle(f"{APP_TITLE} — {session.username} ({session.role})")
        self.setMinimumSize(QSize(1180, 760))

        self._prev_tab_index = 0
        self._users_tab_index: Optional[int] = None

        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #e8f0ff,
                    stop:0.5 #f7faff,
                    stop:1 #e9f6ff
                );
            }
            QWidget { font-size: 13px; }
            QFrame#Card {
                background: white;
                border: 1px solid #e7e7ea;
                border-radius: 14px;
            }
            QFrame#GlassCard {
                background: rgba(255, 255, 255, 0.65);
                border: 1px solid rgba(255, 255, 255, 0.9);
                border-radius: 20px;
            }
            QWidget#GlassPanel {
                background: rgba(255, 255, 255, 0.4);
                border-radius: 16px;
            }
            QLineEdit, QTextEdit, QSpinBox, QComboBox {
                background: white;
                border: 1px solid #dadade;
                border-radius: 10px;
                padding: 7px;
            }
            QLineEdit#GlassInput, QTextEdit#GlassOutput {
                background: rgba(255, 255, 255, 0.7);
                border: 1px solid rgba(255, 255, 255, 0.85);
                border-radius: 14px;
                padding: 10px;
                color: #1f2a37;
            }
            QComboBox#GlassSelect {
                background: rgba(255, 255, 255, 0.7);
                border: 1px solid rgba(255, 255, 255, 0.85);
                border-radius: 14px;
                padding: 7px 10px;
                color: #1f2a37;
            }
            QPushButton {
                background: white;
                border: 1px solid #dadade;
                border-radius: 12px;
                padding: 8px 12px;
                font-weight: 600;
            }
            QPushButton:hover { border-color: #bdbdc2; }
            QPushButton:disabled { color: #999; }
            QPushButton#GlassButton {
                background: rgba(255, 255, 255, 0.55);
                border: 1px solid rgba(255, 255, 255, 0.85);
                border-radius: 14px;
                padding: 10px 16px;
                color: #1f2a37;
            }
            QPushButton#GlassButton:hover {
                background: rgba(255, 255, 255, 0.75);
            }
            QPushButton#GlassPrimaryButton {
                background: rgba(56, 125, 255, 0.2);
                border: 1px solid rgba(56, 125, 255, 0.4);
                border-radius: 14px;
                padding: 10px 18px;
                color: #0b1a33;
            }
            QPushButton#GlassPrimaryButton:hover {
                background: rgba(56, 125, 255, 0.3);
            }
            QTableWidget {
                background: white;
                border: 1px solid #e7e7ea;
                border-radius: 12px;
            }
            QHeaderView::section {
                background: #fafafb;
                border: none;
                border-bottom: 1px solid #e7e7ea;
                padding: 8px;
                font-weight: 700;
                color: #333;
            }
        """)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.on_tab_changed)

        self.dashboard = DashboardWidget(db)
        self.text_message_generator = TextMessageGeneratorWidget()
        self.inventory = InventoryWidget(db)
        self.skus = SkuManagerWidget(db, is_admin=(session.role == "ADMIN"), refresh_callbacks=[])

        refreshers = [self.dashboard.refresh, self.inventory.refresh, self.inventory.refresh_categories, self.skus.refresh_table]
        self.skus.refresh_callbacks = refreshers

        self.scan_in = ScanWidget(db, session, "IN", refreshers)
        self.scan_out = ScanWidget(db, session, "OUT", refreshers)
        self.transactions = TransactionsWidget(db)

        self.tabs.addTab(self.dashboard, "Today")
        self.tabs.addTab(self.text_message_generator, "Text Messages")
        self.tabs.addTab(self.inventory, "Inventory")
        self.tabs.addTab(self.scan_in, "Scan In")
        self.tabs.addTab(self.scan_out, "Scan Out")
        self.tabs.addTab(self.skus, "SKUs")
        self.tabs.addTab(self.transactions, "Transactions")

        if session.role == "ADMIN":
            self.users = UsersWidget(db)
            self._users_tab_index = self.tabs.addTab(self.users, "Users")

        self.setCentralWidget(self.tabs)

        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        refresh_action = QAction("Refresh Dashboard", self)
        refresh_action.triggered.connect(self.dashboard.refresh)
        file_menu.addAction(refresh_action)

        refresh_inventory_action = QAction("Refresh Inventory", self)
        refresh_inventory_action.triggered.connect(self.inventory.refresh)
        file_menu.addAction(refresh_inventory_action)

        image_folder_action = QAction("Set Image Folder…", self)
        image_folder_action.triggered.connect(self.set_image_folder)
        file_menu.addAction(image_folder_action)

        clear_image_folder_action = QAction("Clear Image Folder", self)
        clear_image_folder_action.triggered.connect(self.clear_image_folder)
        file_menu.addAction(clear_image_folder_action)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def on_tab_changed(self, idx: int):
        if self._users_tab_index is not None and idx == self._users_tab_index:
            dlg = PinDialog(self.session.username)
            if dlg.exec() != QDialog.Accepted or not dlg.pin_value:
                self.tabs.setCurrentIndex(self._prev_tab_index)
                self.tabs.setFocus()
                return
            if not self.db.verify_pin(self.session.username, dlg.pin_value):
                QMessageBox.critical(self, "Denied", "Incorrect PIN.")
                self.tabs.setCurrentIndex(self._prev_tab_index)
                self.tabs.setFocus()
                return

        self._prev_tab_index = idx

    def set_image_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose Image Folder", "")
        if not path:
            return
        self.db.set_setting("image_base_dir", path)
        self.skus._set_preview_image(self.skus.image_path.text())

    def clear_image_folder(self):
        self.db.set_setting("image_base_dir", "")
        self.skus._set_preview_image(self.skus.image_path.text())


def main():
    try:
        app = QApplication(sys.argv)
        db = DB(DB_FILE)

        login = LoginDialog(db)
        if login.exec() != QDialog.Accepted or not login.session:
            sys.exit(0)

        win = MainWindow(db, login.session)
        win.show()
        sys.exit(app.exec())

    except Exception:
        QMessageBox.critical(None, "App crashed", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
