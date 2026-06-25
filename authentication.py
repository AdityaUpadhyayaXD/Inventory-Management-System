from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import mysql.connector
from mysql.connector import Error
from PySide6.QtCore import Qt, QSettings, QThread, Signal, QTimer, QObject, QEvent
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
	QApplication,
	QCheckBox,
	QDialog,
	QFormLayout,
	QFrame,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QMainWindow,
	QMessageBox,
	QPushButton,
	QStackedWidget,
	QVBoxLayout,
	QWidget,
	QGraphicsDropShadowEffect,
)


class FocusGlowFilter(QObject):
	"""Event filter to apply a glowing shadow effect to focused input widgets."""

	def __init__(self, parent: Optional[QObject] = None) -> None:
		super().__init__(parent)
		self._active_effects = {}

	def eventFilter(self, watched: QObject, event: QEvent) -> bool:
		from PySide6.QtWidgets import QWidget, QGraphicsDropShadowEffect, QAbstractSpinBox, QComboBox

		if event.type() == QEvent.FocusIn:
			if isinstance(watched, QWidget):
				target = watched
				if isinstance(watched.parent(), (QAbstractSpinBox, QComboBox)):
					target = watched.parent()

				if target not in self._active_effects:
					glow = QGraphicsDropShadowEffect(target)
					glow.setBlurRadius(12)
					glow.setColor(QColor(59, 130, 246, 160))  # Soft blue glow matching theme accent #3b82f6
					glow.setOffset(0, 0)
					target.setGraphicsEffect(glow)
					self._active_effects[target] = glow
		elif event.type() == QEvent.FocusOut:
			if isinstance(watched, QWidget):
				target = watched
				if isinstance(watched.parent(), (QAbstractSpinBox, QComboBox)):
					target = watched.parent()

				if target in self._active_effects:
					target.setGraphicsEffect(None)
					self._active_effects.pop(target, None)
		return super().eventFilter(watched, event)


def apply_focus_glow(parent_widget: QWidget, filter_obj: FocusGlowFilter) -> None:
	"""Recursively installs FocusGlowFilter on all input fields in a parent widget."""
	from PySide6.QtWidgets import QLineEdit, QComboBox, QAbstractSpinBox
	widgets = []
	widgets.extend(parent_widget.findChildren(QLineEdit))
	widgets.extend(parent_widget.findChildren(QComboBox))
	widgets.extend(parent_widget.findChildren(QAbstractSpinBox))
	
	for widget in widgets:
		widget.installEventFilter(filter_obj)
		if isinstance(widget, QAbstractSpinBox):
			li = widget.lineEdit()
			if li:
				li.installEventFilter(filter_obj)
		elif isinstance(widget, QComboBox) and widget.isEditable():
			li = widget.lineEdit()
			if li:
				li.installEventFilter(filter_obj)


# ---------------------------------------------------------------------------
# Animated Loading Screen
# ---------------------------------------------------------------------------

class _SpinnerWidget(QWidget):
	"""A circular arc spinner drawn with QPainter."""

	def __init__(self, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setFixedSize(72, 72)
		self._angle = 0
		self._timer = QTimer(self)
		self._timer.timeout.connect(self._rotate)
		self._timer.start(16)  # ~60 fps

	def _rotate(self) -> None:
		self._angle = (self._angle + 6) % 360
		self.update()

	def paintEvent(self, event) -> None:  # noqa: N802
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing)

		w, h = self.width(), self.height()
		margin = 8
		rect = self.rect().adjusted(margin, margin, -margin, -margin)

		# Track (dim background ring)
		track_pen = QPen(QColor("#1e293b"), 6)
		track_pen.setCapStyle(Qt.RoundCap)
		painter.setPen(track_pen)
		painter.drawEllipse(rect)

		# Spinning arc (bright blue)
		arc_pen = QPen(QColor("#3b82f6"), 6)
		arc_pen.setCapStyle(Qt.RoundCap)
		painter.setPen(arc_pen)
		# Qt uses 1/16th degrees
		start = (90 - self._angle) * 16
		span = -270 * 16
		painter.drawArc(rect, start, span)
		painter.end()

	def stop(self) -> None:
		self._timer.stop()


class LoadingScreen(QDialog):
	"""
	A translucent modal loading overlay.
	Create it, call .show(), do your work, then call .finish().
	"""

	def __init__(self, parent: Optional[QWidget] = None, message: str = "Loading…") -> None:
		super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
		self.setAttribute(Qt.WA_TranslucentBackground)
		self.setModal(True)
		self._build_ui(message)

	def _build_ui(self, message: str) -> None:
		outer = QVBoxLayout(self)
		outer.setContentsMargins(0, 0, 0, 0)

		# Card
		card = QFrame(self)
		card.setObjectName("loadingCard")
		card.setStyleSheet(
			"""
			#loadingCard {
				background-color: #111827;
				border: 1px solid #1f2937;
				border-radius: 16px;
				padding: 32px 40px;
			}
			QLabel {
				color: #cbd5e1;
				font-family: "Segoe UI", Roboto, sans-serif;
				font-size: 14px;
				font-weight: 600;
				background: transparent;
			}
			#loadingTitle {
				color: #ffffff;
				font-size: 18px;
				font-weight: 700;
			}
			"""
		)

		card_layout = QVBoxLayout(card)
		card_layout.setSpacing(16)
		card_layout.setAlignment(Qt.AlignHCenter)

		# App name at top
		title = QLabel("Inventory System")
		title.setObjectName("loadingTitle")
		title.setAlignment(Qt.AlignCenter)
		card_layout.addWidget(title)

		# Spinner
		self._spinner = _SpinnerWidget()
		spinner_row = QHBoxLayout()
		spinner_row.addStretch()
		spinner_row.addWidget(self._spinner)
		spinner_row.addStretch()
		card_layout.addLayout(spinner_row)

		# Status message
		self._msg_label = QLabel(message)
		self._msg_label.setAlignment(Qt.AlignCenter)
		card_layout.addWidget(self._msg_label)

		outer.addStretch()
		outer.addWidget(card, 0, Qt.AlignHCenter)
		outer.addStretch()

	def set_message(self, text: str) -> None:
		self._msg_label.setText(text)

	def finish(self) -> None:
		"""Stop spinner and close dialog."""
		self._spinner.stop()
		self.accept()


# ---------------------------------------------------------------------------
# Background worker threads
# ---------------------------------------------------------------------------

class _DatabaseInitWorker(QThread):
	"""Initialises AuthenticationDatabase off the main thread."""

	succeeded = Signal(object)   # emits the AuthenticationDatabase instance
	failed = Signal(str)         # emits error message

	def __init__(self, config: "DatabaseConfig") -> None:
		super().__init__()
		self._config = config

	def run(self) -> None:
		try:
			db = AuthenticationDatabase(self._config)
			self.succeeded.emit(db)
		except Exception as exc:
			self.failed.emit(str(exc))


class _LoginWorker(QThread):
	"""Runs the (slow) password hash check off the main thread."""

	authenticated = Signal(bool)
	error = Signal(str)

	def __init__(self, database: "AuthenticationDatabase", username: str, password: str) -> None:
		super().__init__()
		self._database = database
		self._username = username
		self._password = password

	def run(self) -> None:
		try:
			result = self._database.authenticate_user(self._username, self._password)
			self.authenticated.emit(result)
		except Exception as exc:
			self.error.emit(str(exc))


@dataclass(frozen=True)
class DatabaseConfig:
	host: str = os.getenv("MYSQL_HOST", "localhost")
	port: int = int(os.getenv("MYSQL_PORT", "3306"))
	user: str = os.getenv("MYSQL_USER", "root")
	password: str = os.getenv("MYSQL_PASSWORD", "")
	database: str = os.getenv("MYSQL_DATABASE", "inventory_db")


class AuthenticationDatabase:
	def __init__(self, config: DatabaseConfig) -> None:
		self.config = config
		self.db_type = "mysql"
		try:
			self._ensure_schema()
		except Exception as exc:
			print(f"MySQL connection failed ({exc}). Falling back to SQLite.")
			self.db_type = "sqlite"
			try:
				self._ensure_schema()
			except Exception as sqlite_exc:
				raise RuntimeError(f"Unable to initialize SQLite database: {sqlite_exc}") from sqlite_exc

	def _connect(self, with_database: bool = True):
		if self.db_type == "sqlite":
			return sqlite3.connect("inventory.db")
		connection_kwargs = {
			"host": self.config.host,
			"port": self.config.port,
			"user": self.config.user,
			"password": self.config.password,
		}
		if with_database:
			connection_kwargs["database"] = self.config.database
		return mysql.connector.connect(**connection_kwargs)

	def _format_query(self, query: str) -> str:
		if self.db_type == "sqlite":
			return query.replace("%s", "?")
		return query

	def _ensure_schema(self) -> None:
		if self.db_type == "sqlite":
			connection = self._connect()
			cursor = connection.cursor()
			
			# Schema safety migration: Drop users if it does not contain full_name
			cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
			if cursor.fetchone():
				cursor.execute("PRAGMA table_info(users)")
				columns = [row[1] for row in cursor.fetchall()]
				if "full_name" not in columns:
					cursor.execute("DROP TABLE users")
					print("SQLite: Dropped legacy users table to update schema.")
			
			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS users (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					username VARCHAR(150) NOT NULL UNIQUE,
					full_name VARCHAR(150) NOT NULL,
					mobile_number VARCHAR(15) NOT NULL,
					password_salt CHAR(32) NOT NULL,
					password_hash CHAR(128) NOT NULL,
					role VARCHAR(20) DEFAULT 'user',
					created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
				)
				"""
			)
			# Schema safety migration: Add role column if missing
			cursor.execute("PRAGMA table_info(users)")
			columns = [row[1] for row in cursor.fetchall()]
			if "role" not in columns:
				cursor.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'")

			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS products (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					sku VARCHAR(100) NOT NULL UNIQUE,
					name VARCHAR(255) NOT NULL,
					category VARCHAR(100),
					quantity INTEGER DEFAULT 0,
					unit_price REAL DEFAULT 0.0,
					min_stock INTEGER DEFAULT 5,
					supplier VARCHAR(255),
					updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
				)
				"""
			)
			connection.commit()
			cursor.close()
			connection.close()
			return

		# MySQL schema initialization
		try:
			connection = self._connect(with_database=False)
			cursor = connection.cursor()
			cursor.execute(
				f"CREATE DATABASE IF NOT EXISTS `{self.config.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
			)
			cursor.close()
			connection.close()

			connection = self._connect(with_database=True)
			cursor = connection.cursor()
			
			# Schema safety migration: Drop users if it does not contain full_name
			try:
				cursor.execute("SHOW COLUMNS FROM users")
				columns = [row[0] for row in cursor.fetchall()]
				if "full_name" not in columns:
					cursor.execute("DROP TABLE users")
					print("MySQL: Dropped legacy users table to update schema.")
			except Exception:
				pass

			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS users (
					id INT AUTO_INCREMENT PRIMARY KEY,
					username VARCHAR(150) NOT NULL UNIQUE,
					full_name VARCHAR(150) NOT NULL,
					mobile_number VARCHAR(15) NOT NULL,
					password_salt CHAR(32) NOT NULL,
					password_hash CHAR(128) NOT NULL,
					role VARCHAR(20) DEFAULT 'user',
					created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
				) ENGINE=InnoDB
				"""
			)
			# Schema safety migration: Add role column if missing
			try:
				cursor.execute("SHOW COLUMNS FROM users")
				columns = [row[0] for row in cursor.fetchall()]
				if "role" not in columns:
					cursor.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'")
			except Exception:
				pass
			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS products (
					id INT AUTO_INCREMENT PRIMARY KEY,
					sku VARCHAR(100) NOT NULL UNIQUE,
					name VARCHAR(255) NOT NULL,
					category VARCHAR(100),
					quantity INT DEFAULT 0,
					unit_price DECIMAL(10, 2) DEFAULT 0.0,
					min_stock INT DEFAULT 5,
					supplier VARCHAR(255),
					updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
				) ENGINE=InnoDB
				"""
			)
			connection.commit()
			cursor.close()
			connection.close()
		except Error as exc:
			raise RuntimeError(
				"Unable to initialize the MySQL database. Check host, port, credentials, and permissions."
			) from exc

	@staticmethod
	def _hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
		salt_value = salt or secrets.token_hex(16)
		password_hash = hashlib.pbkdf2_hmac(
			"sha256",
			password.encode("utf-8"),
			bytes.fromhex(salt_value),
			200_000,
		).hex()
		return salt_value, password_hash  

	def register_user(self, username: str, full_name: str, mobile_number: str, password: str, role: str = 'user') -> None:
		salt, password_hash = self._hash_password(password)
		connection = self._connect()
		cursor = connection.cursor()
		try:
			# Automatically make the first user an admin
			cursor.execute("SELECT COUNT(*) FROM users")
			count = cursor.fetchone()[0]
			if count == 0:
				role = 'admin'

			query = self._format_query(
				"INSERT INTO users (username, full_name, mobile_number, password_salt, password_hash, role) VALUES (%s, %s, %s, %s, %s, %s)"
			)
			cursor.execute(
				query,
				(username, full_name, mobile_number, salt, password_hash, role),
			)
			connection.commit()
		except Exception as exc:
			connection.rollback()
			if self.db_type == "sqlite":
				if isinstance(exc, sqlite3.IntegrityError) or "unique" in str(exc).lower():
					raise ValueError("That username already exists.") from exc
			else:
				if getattr(exc, "errno", None) == 1062:
					raise ValueError("That username already exists.") from exc
			raise RuntimeError("Unable to create the user account.") from exc
		finally:
			cursor.close()
			connection.close()

	def check_username_exists(self, username: str) -> bool:
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("SELECT id FROM users WHERE username = %s")
			cursor.execute(query, (username,))
			row = cursor.fetchone()
			return row is not None
		except Exception:
			return False
		finally:
			cursor.close()
			connection.close()

	def authenticate_user(self, username: str, password: str) -> bool:
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("SELECT password_salt, password_hash FROM users WHERE username = %s")
			cursor.execute(
				query,
				(username,),
			)
			row = cursor.fetchone()
			if not row:
				return False

			stored_salt, stored_hash = row
			_, computed_hash = self._hash_password(password, stored_salt)
			return secrets.compare_digest(computed_hash, stored_hash)
		finally:
			cursor.close()
			connection.close()

	def get_user_role(self, username: str) -> str:
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("SELECT role FROM users WHERE username = %s")
			cursor.execute(query, (username,))
			row = cursor.fetchone()
			return row[0] if row else "user"
		except Exception:
			return "user"
		finally:
			cursor.close()
			connection.close()

	def update_user_role(self, user_id: int, new_role: str) -> None:
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("UPDATE users SET role = %s WHERE id = %s")
			cursor.execute(query, (new_role, user_id))
			connection.commit()
		finally:
			cursor.close()
			connection.close()

	def delete_user_by_id(self, user_id: int) -> None:
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("DELETE FROM users WHERE id = %s")
			cursor.execute(query, (user_id,))
			connection.commit()
		finally:
			cursor.close()
			connection.close()

	def reset_user_password(self, user_id: int, new_password: str) -> None:
		salt, password_hash = self._hash_password(new_password)
		connection = self._connect()
		cursor = connection.cursor()
		try:
			query = self._format_query("UPDATE users SET password_salt = %s, password_hash = %s WHERE id = %s")
			cursor.execute(query, (salt, password_hash, user_id))
			connection.commit()
		finally:
			cursor.close()
			connection.close()


class AuthenticationWindow(QMainWindow):
	def __init__(self) -> None:
		super().__init__()
		self.setWindowTitle("PySide Inventory Management")
		self.setMinimumSize(480, 520)
		self.database: Optional[AuthenticationDatabase] = None
		self._db_worker: Optional[_DatabaseInitWorker] = None
		self._login_worker: Optional[_LoginWorker] = None
		self._loading: Optional[LoadingScreen] = None
		self.settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
		self.settings = QSettings(self.settings_path, QSettings.IniFormat)
		self._build_ui()

		# Apply focus glow filter
		self.focus_glow_filter = FocusGlowFilter(self)
		apply_focus_glow(self, self.focus_glow_filter)

		# Kick off async DB initialisation — loading screen shown via timer
		# so the window can paint first.
		QTimer.singleShot(50, self._start_db_init)
	# ------------------------------------------------------------------
	# Database init (async)
	# ------------------------------------------------------------------
	def _start_db_init(self) -> None:
		self._loading = LoadingScreen(self, "Connecting to database…")
		self._loading.resize(360, 260)
		self._loading.show()
		config = DatabaseConfig()
		self._db_worker = _DatabaseInitWorker(config)
		self._db_worker.succeeded.connect(self._on_db_ready)
		self._db_worker.failed.connect(self._on_db_failed)
		self._db_worker.start()
	def _on_db_ready(self, db: AuthenticationDatabase) -> None:
		self.database = db
		if self._loading:
			self._loading.set_message("Ready!")
			QTimer.singleShot(300, self._close_loading)
	def _on_db_failed(self, message: str) -> None:
		print(f"Database Initialization Error: {message}")
		if self._loading:
			self._loading.set_message("DB unavailable — offline mode")
			QTimer.singleShot(1200, self._close_loading)
	def _close_loading(self) -> None:
		if self._loading:
			self._loading.finish()
			self._loading = None
	def _build_ui(self) -> None:
		central = QWidget()
		self.setCentralWidget(central)
		root_layout = QVBoxLayout(central)
		root_layout.setContentsMargins(0, 0, 0, 0)
		root_layout.setSpacing(0)
		# Stacked layout for switching between Login and Register views
		self.stacked_widget = QStackedWidget()
		self.stacked_widget.addWidget(self._build_login_page())
		self.stacked_widget.addWidget(self._build_register_page())
		root_layout.addWidget(self.stacked_widget)
		self.setStyleSheet(
			"""
			QMainWindow {
				background-color: #0f172a;
			}
			QWidget {
				font-family: "Segoe UI", -apple-system, Roboto, Helvetica, sans-serif;
			}
			QLabel {
				color: #cbd5e1;
				font-size: 14px;
			}
			#titleLabel {
				font-size: 28px;
				font-weight: 800;
				color: #ffffff;
				letter-spacing: -0.5px;
			}
			#subtitleLabel {
				color: #94a3b8;
				font-size: 13px;
			}
			#formFrame {
				background-color: #1e293b;
				border: 1px solid #334155;
				border-radius: 12px;
			}
			QLineEdit {
				background-color: #0f172a;
				color: #f8fafc;
				border: 1px solid #334155;
				border-radius: 6px;
				padding: 8px 12px;
				font-size: 13px;
				min-height: 20px;
			}
			QLineEdit:focus {
				border: 1px solid #3b82f6;
				background-color: #0f172a;
			}
			QCheckBox {
				color: #cbd5e1;
				font-size: 13px;
			}
			QPushButton {
				border: none;
				border-radius: 6px;
				padding: 10px 18px;
				font-weight: 600;
				font-size: 13px;
			}
			#loginButton {
				background-color: #3b82f6;
				color: #ffffff;
			}
			#loginButton:hover {
				background-color: #2563eb;
			}
			#loginButton:pressed {
				background-color: #1d4ed8;
			}
			#registerNavButton {
				background-color: transparent;
				color: #3b82f6;
				border: 1px solid #3b82f6;
			}
			#registerNavButton:hover {
				background-color: #3b82f6;
				color: #ffffff;
			}
			#registerNavButton:pressed {
				background-color: #2563eb;
				color: #ffffff;
			}
			#saveButton {
				background-color: #10b981;
				color: #ffffff;
			}
			#saveButton:hover {
				background-color: #059669;
			}
			#saveButton:pressed {
				background-color: #047857;
			}
			#clearButton {
				background-color: #4b5563;
				color: #ffffff;
			}
			#clearButton:hover {
				background-color: #374151;
			}
			#clearButton:pressed {
				background-color: #1f2937;
			}
			#backLinkButton {
				background-color: transparent;
				color: #3b82f6;
				text-decoration: underline;
				font-size: 13px;
				border: none;
				padding: 4px;
			}
			#backLinkButton:hover {
				color: #60a5fa;
			}
			#errorLabel {
				color: #f43f5e;
				font-size: 11px;
				font-weight: 500;
				padding-top: 1px;
				padding-bottom: 2px;
				background: transparent;
			}
			"""
		)
	def _build_login_page(self) -> QWidget:
		page = QWidget()
		outer = QHBoxLayout(page)
		outer.setContentsMargins(0, 0, 0, 0)
		outer.setAlignment(Qt.AlignCenter)

		container = QWidget()
		container.setMaximumWidth(420)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(24, 24, 24, 24)
		layout.setSpacing(20)
		title = QLabel("Inventory System")
		title.setObjectName("titleLabel")
		subtitle = QLabel("Enter your username and password to log in.")
		subtitle.setObjectName("subtitleLabel")
		header = QVBoxLayout()
		header.addWidget(title)
		header.addWidget(subtitle)
		header.setSpacing(6)
		layout.addLayout(header)
		# Form Container Card
		form_frame = QFrame()
		form_frame.setObjectName("formFrame")
		form_layout = QVBoxLayout(form_frame)
		form_layout.setContentsMargins(20, 20, 20, 20)
		form_layout.setSpacing(16)
		form = QFormLayout()
		form.setVerticalSpacing(12)
		form.setHorizontalSpacing(14)
		self.username_input = QLineEdit()
		self.username_input.setPlaceholderText("Enter username")
		self.err_login_username = QLabel("")
		self.err_login_username.setObjectName("errorLabel")
		username_container = QVBoxLayout()
		username_container.addWidget(self.username_input)
		username_container.addWidget(self.err_login_username)
		username_container.setSpacing(2)
		self.password_input = QLineEdit()
		self.password_input.setPlaceholderText("Enter password")
		self.password_input.setEchoMode(QLineEdit.Password)
		self.password_toggle_btn = QPushButton("Show")
		self.password_toggle_btn.setObjectName("togglePasswordBtn")
		self.password_toggle_btn.setCursor(Qt.PointingHandCursor)
		self.password_toggle_btn.setFixedWidth(60)
		self.password_toggle_btn.setStyleSheet("""
			QPushButton {
				background-color: #1e293b;
				color: #cbd5e1;
				border: 1px solid #334155;
				border-radius: 6px;
				padding: 4px;
				font-size: 11px;
				font-weight: bold;
			}
			QPushButton:hover {
				background-color: #334155;
				color: white;
			}
			QPushButton:focus {
    border: 2px solid #60a5fa;
    background-color: #243244;
    color: white;
}

QPushButton:pressed {
    background-color: #475569;
}
		""")
		self.password_toggle_btn.clicked.connect(self.toggle_login_password)
		password_layout = QHBoxLayout()
		password_layout.setSpacing(6)
		password_layout.addWidget(self.password_input)
		password_layout.addWidget(self.password_toggle_btn)
		self.err_login_password = QLabel("")
		self.err_login_password.setObjectName("errorLabel")
		password_container = QVBoxLayout()
		password_container.addLayout(password_layout)
		password_container.addWidget(self.err_login_password)
		password_container.setSpacing(2)
		username_label = QLabel("Username:")
		username_label.setStyleSheet("font-weight: 600;")
		password_label = QLabel("Password:")
		password_label.setStyleSheet("font-weight: 600;")
		self.remember_me_checkbox = QCheckBox("Remember Me")
		self.remember_me_checkbox.setStyleSheet("color: #cbd5e1; font-size: 13px; font-weight: 500;")
		form.addRow(username_label, username_container)
		form.addRow(password_label, password_container)
		form.addRow("", self.remember_me_checkbox)
		form_layout.addLayout(form)
		self.err_login_general = QLabel("")
		self.err_login_general.setObjectName("errorLabel")
		form_layout.addWidget(self.err_login_general)
		button_layout = QHBoxLayout()
		button_layout.setSpacing(12)
		self.login_button = QPushButton("Log In")
		self.login_button.setObjectName("loginButton")
		self.login_button.setCursor(Qt.PointingHandCursor)
		self.login_button.clicked.connect(self.handle_login)
		self.register_btn_nav = QPushButton("Register")
		self.register_btn_nav.setObjectName("registerNavButton")
		self.register_btn_nav.setCursor(Qt.PointingHandCursor)
		self.register_btn_nav.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))
		button_layout.addWidget(self.login_button)
		button_layout.addWidget(self.register_btn_nav)
		form_layout.addLayout(button_layout)
		# Load remembered credentials
		self._load_remembered_credentials()
		layout.addWidget(form_frame)
		layout.addStretch()
		outer.addWidget(container)
		return page
	def _build_register_page(self) -> QWidget:
		page = QWidget()
		outer = QHBoxLayout(page)
		outer.setContentsMargins(0, 0, 0, 0)
		outer.setAlignment(Qt.AlignCenter)

		container = QWidget()
		container.setMaximumWidth(420)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(24, 24, 24, 24)
		layout.setSpacing(16)
		title = QLabel("Create Account")
		title.setObjectName("titleLabel")
		subtitle = QLabel("Please fill in all mandatory fields to register.")
		subtitle.setObjectName("subtitleLabel")
		header = QVBoxLayout()
		header.addWidget(title)
		header.addWidget(subtitle)
		header.setSpacing(4)
		layout.addLayout(header)
		# Form Container Card
		form_frame = QFrame()
		form_frame.setObjectName("formFrame")
		form_layout = QVBoxLayout(form_frame)
		form_layout.setContentsMargins(20, 16, 20, 16)
		form_layout.setSpacing(10)
		form = QFormLayout()
		form.setVerticalSpacing(8)
		form.setHorizontalSpacing(14)
		# Full Name field + error message container
		self.reg_fullname = QLineEdit()
		self.reg_fullname.setPlaceholderText("Enter full name (letters only)")
		self.err_fullname = QLabel("")
		self.err_fullname.setObjectName("errorLabel")
		fullname_container = QVBoxLayout()
		fullname_container.addWidget(self.reg_fullname)
		fullname_container.addWidget(self.err_fullname)
		fullname_container.setSpacing(2)
		# Username field + error message container
		self.reg_username = QLineEdit()
		self.reg_username.setPlaceholderText("Choose unique username")
		self.err_username = QLabel("")
		self.err_username.setObjectName("errorLabel")
		username_container = QVBoxLayout()
		username_container.addWidget(self.reg_username)
		username_container.addWidget(self.err_username)
		username_container.setSpacing(2)
		# Mobile Number field + error message container
		self.reg_mobile = QLineEdit()
		self.reg_mobile.setPlaceholderText("Enter 10-digit mobile")
		self.err_mobile = QLabel("")
		self.err_mobile.setObjectName("errorLabel")
		mobile_container = QVBoxLayout()
		mobile_container.addWidget(self.reg_mobile)
		mobile_container.addWidget(self.err_mobile)
		mobile_container.setSpacing(2)
		# Password field + error message container
		self.reg_password = QLineEdit()
		self.reg_password.setPlaceholderText("Minimum 6 characters")
		self.reg_password.setEchoMode(QLineEdit.Password)
		self.reg_password_toggle_btn = QPushButton("Show")
		self.reg_password_toggle_btn.setObjectName("togglePasswordBtn")
		self.reg_password_toggle_btn.setCursor(Qt.PointingHandCursor)
		self.reg_password_toggle_btn.setFixedWidth(60)
		self.reg_password_toggle_btn.setStyleSheet("""
			QPushButton {
				background-color: #1e293b;
				color: #cbd5e1;
				border: 1px solid #334155;
				border-radius: 6px;
				padding: 4px;
				font-size: 11px;
				font-weight: bold;
			}
			QPushButton:hover {
				background-color: #334155;
				color: white;
			}
		""")
		self.reg_password_toggle_btn.clicked.connect(self.toggle_reg_password)

		reg_password_layout = QHBoxLayout()
		reg_password_layout.setSpacing(6)
		reg_password_layout.addWidget(self.reg_password)
		reg_password_layout.addWidget(self.reg_password_toggle_btn)

		self.err_password = QLabel("")
		self.err_password.setObjectName("errorLabel")
		password_container = QVBoxLayout()
		password_container.addLayout(reg_password_layout)
		password_container.addWidget(self.err_password)
		password_container.setSpacing(2)

		# Confirm Password field + error message container
		self.reg_confirm = QLineEdit()
		self.reg_confirm.setPlaceholderText("Repeat password")
		self.reg_confirm.setEchoMode(QLineEdit.Password)

		self.reg_confirm_toggle_btn = QPushButton("Show")
		self.reg_confirm_toggle_btn.setObjectName("togglePasswordBtn")
		self.reg_confirm_toggle_btn.setCursor(Qt.PointingHandCursor)
		self.reg_confirm_toggle_btn.setFixedWidth(60)
		self.reg_confirm_toggle_btn.setStyleSheet("""
			QPushButton {
				background-color: #1e293b;
				color: #cbd5e1;
				border: 1px solid #334155;
				border-radius: 6px;
				padding: 4px;
				font-size: 11px;
				font-weight: bold;
			}
			QPushButton:hover {
				background-color: #334155;
				color: white;
			}
		""")
		self.reg_confirm_toggle_btn.clicked.connect(self.toggle_reg_confirm)

		reg_confirm_layout = QHBoxLayout()
		reg_confirm_layout.setSpacing(6)
		reg_confirm_layout.addWidget(self.reg_confirm)
		reg_confirm_layout.addWidget(self.reg_confirm_toggle_btn)

		self.err_confirm = QLabel("")
		self.err_confirm.setObjectName("errorLabel")
		confirm_container = QVBoxLayout()
		confirm_container.addLayout(reg_confirm_layout)
		confirm_container.addWidget(self.err_confirm)
		confirm_container.setSpacing(2)

		form.addRow(QLabel("Full Name:"), fullname_container)
		form.addRow(QLabel("Username:"), username_container)
		form.addRow(QLabel("Mobile No:"), mobile_container)
		form.addRow(QLabel("Password:"), password_container)
		form.addRow(QLabel("Confirm:"), confirm_container)
		form_layout.addLayout(form)

		# Save and Clear Buttons
		button_layout = QHBoxLayout()
		button_layout.setSpacing(12)

		self.save_button = QPushButton("Save")
		self.save_button.setObjectName("saveButton")
		self.save_button.setCursor(Qt.PointingHandCursor)
		self.save_button.clicked.connect(self.handle_save_register)

		self.clear_button = QPushButton("Clear")
		self.clear_button.setObjectName("clearButton")
		self.clear_button.setCursor(Qt.PointingHandCursor)
		self.clear_button.clicked.connect(self.handle_clear_register)

		button_layout.addWidget(self.save_button)
		button_layout.addWidget(self.clear_button)
		form_layout.addLayout(button_layout)

		layout.addWidget(form_frame)

		# Back Link
		back_layout = QHBoxLayout()
		self.back_button = QPushButton("Back to Sign In")
		self.back_button.setObjectName("backLinkButton")
		self.back_button.setCursor(Qt.PointingHandCursor)
		self.back_button.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
		back_layout.addStretch()
		back_layout.addWidget(self.back_button)
		back_layout.addStretch()
		layout.addLayout(back_layout)

		layout.addStretch()
		outer.addWidget(container)
		return page

	def _require_database(self) -> Optional[AuthenticationDatabase]:
		if self.database is None:
			QMessageBox.critical(
				self,
				"Database Connection Failed",
				"The database connection is unavailable. Verify that your settings are correct.",
			)
		return self.database

	def handle_clear_register(self) -> None:
		self.reg_fullname.clear()
		self.reg_username.clear()
		self.reg_mobile.clear()
		self.reg_password.clear()
		self.reg_confirm.clear()

		self.err_fullname.setText("")
		self.err_username.setText("")
		self.err_mobile.setText("")
		self.err_password.setText("")
		self.err_confirm.setText("")

	def handle_save_register(self) -> None:
		database = self._require_database()
		if database is None:
			return

		# Clear previous errors
		self.err_fullname.setText("")
		self.err_username.setText("")
		self.err_mobile.setText("")
		self.err_password.setText("")
		self.err_confirm.setText("")

		fullname = self.reg_fullname.text().strip()
		username = self.reg_username.text().strip()
		mobile = self.reg_mobile.text().strip()
		password = self.reg_password.text()
		confirm = self.reg_confirm.text()

		has_errors = False

		# 1. Validation - Full Name
		if not fullname:
			self.err_fullname.setText("Full Name is required.")
			has_errors = True
		elif not all(c.isalpha() or c.isspace() or c == '.' or c == "'" or c == "-" or c == "_" for c in fullname):
			self.err_fullname.setText("Only characters and spaces allowed.")
			has_errors = True

		# 2. Validation - Username
		if not username:
			self.err_username.setText("Username is required.")
			has_errors = True
		elif len(username) < 3:
			self.err_username.setText("Must be at least 3 characters.")
			has_errors = True
		elif database.check_username_exists(username):
			self.err_username.setText("Username already exists.")
			has_errors = True

		# 3. Validation - Mobile Number
		if not mobile:
			self.err_mobile.setText("Mobile number is required.")
			has_errors = True
		elif not (mobile.isdigit() and len(mobile) == 10):
			self.err_mobile.setText("Invalid mobile number. Must be exactly 10 digits.")
			has_errors = True

		# 4. Validation - Password
		if not password:
			self.err_password.setText("Password is required.")
			has_errors = True
		elif len(password) < 6:
			self.err_password.setText("Password must be minimum 6 characters.")
			has_errors = True

		# 5. Validation - Confirm Password
		if not confirm:
			self.err_confirm.setText("Confirm password is required.")
			has_errors = True
		elif password != confirm:
			self.err_confirm.setText("Passwords do not match.")
			has_errors = True

		if has_errors:
			return

		# Insert user
		try:
			database.register_user(username, fullname, mobile, password)
			QMessageBox.information(
				self,
				"Success",
				"Account registered successfully! You can now sign in."
			)
			self.handle_clear_register()
			self.stacked_widget.setCurrentIndex(0)
			self.username_input.setText(username)
			self.password_input.clear()
		except Exception as exc:
			QMessageBox.critical(self, "Registration Error", f"Unable to save user: {exc}")

	def handle_login(self) -> None:
		database = self._require_database()
		if database is None:
			return

		# Clear previous errors
		self.err_login_username.setText("")
		self.err_login_password.setText("")
		self.err_login_general.setText("")

		username = self.username_input.text().strip()
		password = self.password_input.text()

		has_errors = False
		if not username:
			self.err_login_username.setText("Username is required.")
			has_errors = True
		if not password:
			self.err_login_password.setText("Password is required.")
			has_errors = True

		if has_errors:
			return

		# Disable login button to prevent double-click
		self.login_button.setEnabled(False)
		self.register_btn_nav.setEnabled(False)

		# Show loading overlay while hashing password (can take ~0.5s)
		self._login_loading = LoadingScreen(self, "Verifying credentials…")
		self._login_loading.resize(360, 260)
		self._login_loading.show()

		self._login_username = username
		self._login_password = password

		self._login_worker = _LoginWorker(database, username, password)
		self._login_worker.authenticated.connect(self._on_login_result)
		self._login_worker.error.connect(self._on_login_error)
		self._login_worker.start()

	def _on_login_result(self, is_authenticated: bool) -> None:
		self._login_loading.finish()
		self.login_button.setEnabled(True)
		self.register_btn_nav.setEnabled(True)

		database = self.database
		username = self._login_username
		password = self._login_password

		if is_authenticated:
			# Save remember me credentials
			if self.remember_me_checkbox.isChecked():
				self.settings.setValue("remember_me", True)
				self.settings.setValue("username", username)
				self.settings.setValue("password", password)
			else:
				self.settings.setValue("remember_me", False)
				self.settings.setValue("username", "")
				self.settings.setValue("password", "")
			self.settings.sync()

			try:
				from inventory_management import InventoryWindow
				self.inventory_window = InventoryWindow(database, username)
				if self.isMinimized():
					self.inventory_window.showMinimized()
				else:
					self.inventory_window.showMaximized()
				self.close()
			except Exception as exc:
				import traceback
				traceback.print_exc()
				QMessageBox.critical(self, "Load Error", f"Could not load Inventory Dashboard: {exc}")
		else:
			self.err_login_general.setText("Invalid username or password.")

	def _on_login_error(self, message: str) -> None:
		self._login_loading.finish()
		self.login_button.setEnabled(True)
		self.register_btn_nav.setEnabled(True)
		self.err_login_general.setText(f"Login failed: {message}")

	def toggle_login_password(self) -> None:
		if self.password_input.echoMode() == QLineEdit.Password:
			self.password_input.setEchoMode(QLineEdit.Normal)
			self.password_toggle_btn.setText("Hide")
		else:
			self.password_input.setEchoMode(QLineEdit.Password)
			self.password_toggle_btn.setText("Show")

	def toggle_reg_password(self) -> None:
		if self.reg_password.echoMode() == QLineEdit.Password:
			self.reg_password.setEchoMode(QLineEdit.Normal)
			self.reg_password_toggle_btn.setText("Hide")
		else:
			self.reg_password.setEchoMode(QLineEdit.Password)
			self.reg_password_toggle_btn.setText("Show")

	def toggle_reg_confirm(self) -> None:
		if self.reg_confirm.echoMode() == QLineEdit.Password:
			self.reg_confirm.setEchoMode(QLineEdit.Normal)
			self.reg_confirm_toggle_btn.setText("Hide")
		else:
			self.reg_confirm.setEchoMode(QLineEdit.Password)
			self.reg_confirm_toggle_btn.setText("Show")

	def _load_remembered_credentials(self) -> None:
		remember_val = self.settings.value("remember_me")
		is_remembered = False
		if remember_val is not None:
			if isinstance(remember_val, bool):
				is_remembered = remember_val
			else:
				is_remembered = str(remember_val).lower() == "true"
		
		if is_remembered:
			self.username_input.setText(str(self.settings.value("username", "")))
			self.password_input.setText(str(self.settings.value("password", "")))
			self.remember_me_checkbox.setChecked(True)

def main() -> int:
	app = QApplication(sys.argv)
	window = AuthenticationWindow()
	window.showMaximized()
	return app.exec()

if __name__ == "__main__":
	raise SystemExit(main())