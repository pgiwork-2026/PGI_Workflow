import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import os
import uuid
import datetime
import datetime as dt_module
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import urllib.request
import urllib.error
import io
import random
import re
import time
import threading
import base64
import urllib.parse

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    print("[WARN] APScheduler not installed — scheduled jobs will not run.")

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip().strip('"').strip("'")
GROQ_MODEL   = "llama-3.3-70b-versatile"   # confirmed current, supported Groq model
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"   # confirmed correct endpoint

if not GROQ_API_KEY:
    print("[GROQ][STARTUP] WARNING: GROQ_API_KEY is not set. AI assistant will be disabled.")
else:
    print(f"[GROQ][STARTUP] key_present=True key_len={len(GROQ_API_KEY)} "
          f"key_prefix={GROQ_API_KEY[:8]} model={GROQ_MODEL}")


def groq_chat(messages, tools=None):
    """
    Calls Groq's OpenAI-compatible chat completions endpoint.
    Raises RuntimeError with Groq's actual error message on failure —
    never lets a raw HTTPError/403 bubble up unexplained.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key is missing or invalid.")

    payload = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.3}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",   # exactly "Bearer <key>", no extra chars
        "Content-Type": "application/json",
    }

    print(f"[GROQ] request → model={GROQ_MODEL} endpoint={GROQ_URL} "
          f"key_len={len(GROQ_API_KEY)} key_prefix={GROQ_API_KEY[:8]}")

    try:
        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"[GROQ] network error: {e}")
        raise RuntimeError(f"Could not reach Groq API: {e}")

    print(f"[GROQ] response ← status_code={resp.status_code}")

    if resp.status_code != 200:
        try:
            err_body = resp.json()
            err_msg  = err_body.get("error", {}).get("message", resp.text)
        except ValueError:
            err_msg = resp.text
        print(f"[GROQ] error body: {err_msg}")

        if resp.status_code in (401, 403):
            raise RuntimeError("Groq API key is missing or invalid.")
        elif resp.status_code == 404:
            raise RuntimeError(f"Groq model not found or unavailable: {GROQ_MODEL}")
        elif resp.status_code == 429:
            raise RuntimeError("Groq API rate limit reached. Please try again shortly.")
        else:
            raise RuntimeError(f"Groq API error ({resp.status_code}): {err_msg}")

    return resp.json()

# ─────────────────────────────────────────────
# TIMEZONE — single source of truth for all timestamps.
# Server may run in UTC (Render/production) or IST (local), so every
# date/time used anywhere in this app MUST go through these helpers
# instead of datetime.datetime.now() / .utcnow() / .today().
# ─────────────────────────────────────────────
# Fixed UTC+5:30 offset — no zoneinfo/tzdata dependency.
# Works identically on Windows, macOS, Linux, and Render/production.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def now_ist():
    """Current timezone-aware datetime in IST (UTC+5:30), regardless of server locale."""
    return datetime.datetime.now(IST)

def today_ist():
    """Current date in IST (use instead of datetime.date.today())."""
    return now_ist().date()

app = Flask(__name__)
CORS(app)

# Startup log — verify deployment timezone immediately on import so this
# fires under gunicorn/Render too, not just `python app.py`.
print("Server Timezone:", now_ist())

app.config["JWT_SECRET_KEY"]            = os.getenv("JWT_SECRET", "pgi-secret-key-2025")
app.config["JWT_ACCESS_TOKEN_EXPIRES"]  = datetime.timedelta(hours=24)

db_url = os.getenv("DATABASE_URL", "sqlite:///taskflow.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url

if db_url.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"sslmode": "require"},
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

jwt = JWTManager(app)
db  = SQLAlchemy(app)


# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"
    id            = db.Column(db.String(100), primary_key=True)
    email         = db.Column(db.String(200), unique=True, nullable=False)
    password      = db.Column(db.String(300), nullable=False)
    role          = db.Column(db.String(50),  nullable=False)
    name          = db.Column(db.String(200), nullable=False)
    initials      = db.Column(db.String(10))
    team          = db.Column(db.String(100))
    specialty     = db.Column(db.String(100))
    phone         = db.Column(db.String(30))
    department    = db.Column(db.String(100))
    domain        = db.Column(db.String(100))
    joining_date  = db.Column(db.String(50))
    date_of_birth = db.Column(db.String(20))
    profile_photo = db.Column(db.Text)
    active        = db.Column(db.Boolean, default=True, server_default="1")


class Task(db.Model):
    __tablename__ = "tasks"
    id               = db.Column(db.String(100), primary_key=True)
    title            = db.Column(db.String(300), nullable=False)
    desc             = db.Column(db.Text)
    assignedTo       = db.Column(db.String(100))
    assignedBy       = db.Column(db.String(100))
    status           = db.Column(db.String(50))
    priority         = db.Column(db.String(50))
    due              = db.Column(db.String(50))
    msg              = db.Column(db.Text)
    createdAt        = db.Column(db.String(100))
    proof_text       = db.Column(db.Text)
    proof_link       = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    work_completion_percentage = db.Column(db.Float, default=0.0)
    template_id       = db.Column(db.String(100), nullable=True)
    generated_date    = db.Column(db.String(20), nullable=True)
    is_auto           = db.Column(db.Boolean, nullable=True, default=False)


class DailyTaskUpdate(db.Model):
    __tablename__ = "daily_task_updates"
    id             = db.Column(db.Integer, primary_key=True)
    userId         = db.Column(db.String(100), nullable=False, index=True)
    employee_name  = db.Column(db.String(200))
    department     = db.Column(db.String(100))
    designation    = db.Column(db.String(100))
    date           = db.Column(db.String(20), nullable=False, index=True)
    today_tasks    = db.Column(db.Text)
    achievements   = db.Column(db.Text)
    blockers       = db.Column(db.Text)
    tomorrow_plan  = db.Column(db.Text)
    total_hours    = db.Column(db.Float)
    work_status    = db.Column(db.String(30), default="Submitted")
    founder_review = db.Column(db.Text)
    reviewed       = db.Column(db.Boolean, default=False)
    submitted_at   = db.Column(db.String(100))
    reviewed_at    = db.Column(db.String(100))


class Message(db.Model):
    __tablename__ = "messages"
    id        = db.Column(db.String(100), primary_key=True)
    from_name = db.Column(db.String(200))
    fromId    = db.Column(db.String(100))
    text      = db.Column(db.Text)
    time      = db.Column(db.String(100))
    timestamp = db.Column(db.String(50))
    channel   = db.Column(db.String(100), nullable=False, server_default='all')


class DailyQuote(db.Model):
    __tablename__ = "daily_quotes"
    id         = db.Column(db.Integer, primary_key=True)
    date       = db.Column(db.String(20),  nullable=False, unique=True)
    quote_text = db.Column(db.Text,        nullable=False)
    author     = db.Column(db.String(200), default="Unknown")
    sent_at    = db.Column(db.String(100))


class QuoteDeliveryLog(db.Model):
    __tablename__ = "quote_delivery_log"
    id                = db.Column(db.Integer, primary_key=True)
    date              = db.Column(db.String(20),  nullable=False)
    userId            = db.Column(db.String(100), nullable=False)
    channel           = db.Column(db.String(20),  nullable=False)
    status            = db.Column(db.String(20),  default='sent')
    sent_at           = db.Column(db.String(100))
    error_message     = db.Column(db.Text)
    phone             = db.Column(db.String(30))
    provider_response = db.Column(db.Text)


class WhatsAppWebhookEvent(db.Model):
    """Records failed/undeliverable Meta WhatsApp template statuses received
    via the /api/whatsapp/webhook status callback (e.g. error 131047)."""
    __tablename__ = "whatsapp_webhook_events"
    id            = db.Column(db.Integer, primary_key=True)
    wa_message_id = db.Column(db.String(200))
    recipient     = db.Column(db.String(30))
    status        = db.Column(db.String(50))
    error_code    = db.Column(db.String(20))
    error_title   = db.Column(db.String(300))
    error_message = db.Column(db.Text)
    error_details = db.Column(db.Text)
    received_at   = db.Column(db.String(100))


class Attendance(db.Model):
    __tablename__ = "attendance"
    id            = db.Column(db.Integer, primary_key=True)
    userId        = db.Column(db.String(100), nullable=False)
    status        = db.Column(db.String(50),  nullable=False)
    date          = db.Column(db.String(20),  nullable=False)
    # exact timestamp when attendance was last toggled
    checkin_time  = db.Column(db.String(30))   # ISO datetime string e.g. "2026-06-16T09:14:32"
    checkout_time = db.Column(db.String(30))   # set when toggled absent after being present
    worked_hours  = db.Column(db.Float)        # hours between checkin_time and checkout_time
    # GEO TAGGING — check-in location
    checkin_latitude  = db.Column(db.Float)
    checkin_longitude = db.Column(db.Float)
    # GEO TAGGING — check-out location
    checkout_latitude  = db.Column(db.Float)
    checkout_longitude = db.Column(db.Float)

class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"
    id           = db.Column(db.Integer, primary_key=True)
    userId       = db.Column(db.String(100), nullable=False)
    date         = db.Column(db.String(20), nullable=False)   # YYYY-MM-DD (single day leave)
    reason       = db.Column(db.Text)
    status       = db.Column(db.String(20), default="pending")  # pending | approved | rejected
    requested_at = db.Column(db.String(100))
    decided_at   = db.Column(db.String(100))
    decided_by   = db.Column(db.String(100))


class MorningMessage(db.Model):
    __tablename__ = "morning_messages"
    id        = db.Column(db.Integer, primary_key=True)
    text      = db.Column(db.Text)
    from_name = db.Column(db.String(200))
    time      = db.Column(db.String(100))
    date      = db.Column(db.String(20))


class Notification(db.Model):
    __tablename__ = "notifications"
    id        = db.Column(db.Integer, primary_key=True)
    userId    = db.Column(db.String(100), nullable=False)
    ntype     = db.Column("type",  db.String(50))
    title     = db.Column(db.String(300))
    body      = db.Column(db.Text)
    is_read   = db.Column("read",  db.Boolean, default=False)
    createdAt = db.Column(db.String(100))


class BirthdayAlert(db.Model):
    __tablename__ = "birthday_alerts"
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(100), nullable=False)
    alert_year  = db.Column(db.Integer,     nullable=False)
    sent_at     = db.Column(db.String(100))
    __table_args__ = (
        db.UniqueConstraint("employee_id", "alert_year", name="uq_birthday_alert"),
    )


class Todo(db.Model):
    __tablename__ = "todos"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(100), nullable=False, index=True)
    title      = db.Column(db.Text, nullable=False)
    completed  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String(100))


class AIChatMessage(db.Model):
    __tablename__ = "ai_chat_messages"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(100), nullable=False, index=True)
    role       = db.Column(db.String(20),  nullable=False)   # 'user' | 'assistant'
    content    = db.Column(db.Text,        nullable=False)
    created_at = db.Column(db.String(100))


class Rating(db.Model):
    __tablename__ = "ratings"
    id     = db.Column(db.Integer, primary_key=True)
    userId = db.Column(db.String(100), nullable=False)
    score  = db.Column(db.Integer,     nullable=False)
    date   = db.Column(db.String(20),  nullable=False)
    note   = db.Column(db.Text)


class Project(db.Model):
    __tablename__ = "projects"
    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(300), nullable=False)
    category            = db.Column(db.String(50),  nullable=False)
    client_name         = db.Column(db.String(200))
    status              = db.Column(db.String(50))
    start_date          = db.Column(db.String(50))
    team_members        = db.Column(db.Text)
    description         = db.Column(db.Text)
    created_at          = db.Column(db.String(100))
    progress_percentage = db.Column(db.Float, default=0.0)


class Intern(db.Model):
    __tablename__ = "interns_roster"
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(200), nullable=False)
    domain       = db.Column(db.String(100))
    joining_date = db.Column(db.String(50))
    status       = db.Column(db.String(50))
    created_at   = db.Column(db.String(100))


class ProjectTask(db.Model):
    __tablename__ = "project_tasks"
    id           = db.Column(db.Integer, primary_key=True)
    project_name = db.Column(db.String(300), nullable=False, index=True)
    task_name    = db.Column(db.String(300), nullable=False)
    assigned_to  = db.Column(db.String(200))
    status       = db.Column(db.String(50), nullable=False)
    due_date     = db.Column(db.String(50))
    created_at   = db.Column(db.String(100))


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id           = db.Column(db.Integer, primary_key=True)
    action       = db.Column(db.String(400), nullable=False)
    performed_by = db.Column(db.String(200))
    timestamp    = db.Column(db.String(100))


class DashboardGoal(db.Model):
    __tablename__ = "dashboard_goals"
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(100), nullable=False, default="0")


class VisionBoardItem(db.Model):
    __tablename__ = "vision_board_items"
    id             = db.Column(db.Integer, primary_key=True)
    title          = db.Column(db.String(200), nullable=False)
    current_value  = db.Column(db.Float, nullable=False, default=0)
    target_value   = db.Column(db.Float, nullable=False, default=0)
    unit           = db.Column(db.String(30), default="")
    icon           = db.Column(db.String(50), nullable=True)
    display_order  = db.Column(db.Integer, default=0)
    created_by     = db.Column(db.String(100))
    updated_by     = db.Column(db.String(100))
    created_at     = db.Column(db.String(100))
    updated_at     = db.Column(db.String(100))
    active         = db.Column(db.Boolean, default=True)


def visionboard_item_to_dict(v):
    progress = 0
    try:
        if v.target_value and float(v.target_value) != 0:
            progress = round((float(v.current_value) / float(v.target_value)) * 100, 1)
    except (TypeError, ZeroDivisionError):
        progress = 0
    return {
        "id": v.id,
        "title": v.title,
        "current_value": v.current_value,
        "target_value": v.target_value,
        "unit": v.unit,
        "icon": v.icon,
        "display_order": v.display_order,
        "progress": progress,
        "created_by": v.created_by,
        "updated_by": v.updated_by,
        "created_at": v.created_at,
        "updated_at": v.updated_at,
    }

class TaskTemplate(db.Model):
    __tablename__ = "task_templates"
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    priority    = db.Column(db.String(50), default="medium")
    frequency   = db.Column(db.String(20), nullable=False)   # daily | weekly | monthly
    target_type = db.Column(db.String(10), nullable=False)   # team | user | all
    target_id   = db.Column(db.String(100), nullable=True)   # team id or user id (not required for "all")
    due_time    = db.Column(db.String(10))                   # HH:MM  (optional display)
    active      = db.Column(db.Boolean, default=True)
    created_by  = db.Column(db.String(100))
    created_at  = db.Column(db.String(100))
    last_run    = db.Column(db.String(20))                   # YYYY-MM-DD last dispatch


class SelfTask(db.Model):
    __tablename__ = "self_tasks"
    id                = db.Column(db.Integer, primary_key=True)
    userId            = db.Column(db.String(100), nullable=False, index=True)
    title             = db.Column(db.String(300), nullable=False)
    description       = db.Column(db.Text)
    priority          = db.Column(db.String(20), default="medium")
    status            = db.Column(db.String(20), default="pending")
    created_date      = db.Column(db.String(20))
    due_date          = db.Column(db.String(20))
    follow_up_date    = db.Column(db.String(20))
    follow_up_time    = db.Column(db.String(10))
    completed         = db.Column(db.Boolean, default=False)
    completed_at      = db.Column(db.String(100))
    notification_sent = db.Column(db.Boolean, default=False)
    created_at        = db.Column(db.String(100))
    updated_at        = db.Column(db.String(100))


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def now_str():
    return now_ist().strftime("%I:%M %p")

def today_str():
    return now_ist().strftime("%Y-%m-%d")

def now_iso():
    return now_ist().strftime("%Y-%m-%dT%H:%M:%S")

def make_notif(userId, ntype, title, body):
    n = Notification(
        userId=userId, ntype=ntype,
        title=title, body=body,
        is_read=False,
        createdAt=now_ist().strftime("%b %d %I:%M %p")
    )
    db.session.add(n)


def is_founder_like(user):
    return user and user.role in ("founder", "founder_assistant")


# ─────────────────────────────────────────────
# CREATE / MIGRATE TABLES
# ─────────────────────────────────────────────

with app.app_context():
    db.create_all()

    with db.engine.connect() as conn:
        migrations = [
            "ALTER TABLE messages         ADD COLUMN channel      VARCHAR(100) DEFAULT 'all'",
            "ALTER TABLE attendance       ADD COLUMN date         VARCHAR(20)",
            "ALTER TABLE morning_messages ADD COLUMN date         VARCHAR(20)",
            "ALTER TABLE users            ADD COLUMN department   VARCHAR(100)",
            "ALTER TABLE users            ADD COLUMN domain       VARCHAR(100)",
            "ALTER TABLE users            ADD COLUMN joining_date VARCHAR(50)",
            "ALTER TABLE tasks            ADD COLUMN work_completion_percentage FLOAT DEFAULT 0",
            "ALTER TABLE messages         ADD COLUMN timestamp    VARCHAR(50)",
            "ALTER TABLE projects         ADD COLUMN progress_percentage FLOAT DEFAULT 0",
            "ALTER TABLE users            ADD COLUMN date_of_birth VARCHAR(20)",
            "ALTER TABLE users            ADD COLUMN profile_photo TEXT",
            "ALTER TABLE birthday_alerts  ADD COLUMN sent_at VARCHAR(100)",
            # NEW: attendance timestamp columns
            "ALTER TABLE attendance       ADD COLUMN checkin_time  VARCHAR(30)",
            "ALTER TABLE attendance       ADD COLUMN checkout_time VARCHAR(30)",
            # NEW: worked hours (decimal), computed on check-out
            "ALTER TABLE attendance       ADD COLUMN worked_hours FLOAT",
            # NEW: WhatsApp delivery audit trail
            "ALTER TABLE quote_delivery_log ADD COLUMN error_message TEXT",
            "ALTER TABLE quote_delivery_log ADD COLUMN phone VARCHAR(30)",
            "ALTER TABLE quote_delivery_log ADD COLUMN provider_response TEXT",
            # NEW: active/disabled flag for users (controls WhatsApp quote delivery)
            "ALTER TABLE users ADD COLUMN active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE task_templates ADD COLUMN due_time VARCHAR(10)",
            "ALTER TABLE tasks ADD COLUMN template_id VARCHAR(100)",
            "ALTER TABLE tasks ADD COLUMN generated_date VARCHAR(20)",
            "ALTER TABLE tasks ADD COLUMN is_auto BOOLEAN DEFAULT FALSE",           # GEO TAGGING — check-in / check-out coordinates
            "ALTER TABLE attendance ADD COLUMN checkin_latitude  FLOAT",
            "ALTER TABLE attendance ADD COLUMN checkin_longitude FLOAT",
            "ALTER TABLE attendance ADD COLUMN checkout_latitude  FLOAT",
            "ALTER TABLE attendance ADD COLUMN checkout_longitude FLOAT",
        ]
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()

        # Standalone migration: task_templates.target_id must allow NULL
        # (required for Target Type = "all", which has no single team/user).
        try:
            conn.execute(text("""
                ALTER TABLE task_templates
                ALTER COLUMN target_id DROP NOT NULL
            """))
            conn.commit()
        except Exception:
            conn.rollback()

        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()

        # Standalone migration: task_templates.target_id must allow NULL
        # (required for Target Type = "all", which has no single team/user).
        try:
            conn.execute(text("""
                ALTER TABLE task_templates
                ALTER COLUMN target_id DROP NOT NULL
            """))
            conn.commit()
        except Exception:
            conn.rollback()

        # Standalone migration (SQLite): task_templates.target_id must allow
        # NULL. SQLite cannot ALTER COLUMN to drop NOT NULL, so the table is
        # safely recreated only if the current schema still has it NOT NULL.
        try:
            pragma_rows = conn.execute(text("PRAGMA table_info(task_templates)")).fetchall()
            target_id_col = next((row for row in pragma_rows if row[1] == "target_id"), None)
            # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
            if target_id_col is not None and target_id_col[3] == 1:
                conn.execute(text("ALTER TABLE task_templates RENAME TO task_templates_old"))
                conn.execute(text("""
                    CREATE TABLE task_templates (
                        id          INTEGER PRIMARY KEY,
                        title       VARCHAR(300) NOT NULL,
                        description TEXT,
                        priority    VARCHAR(50) DEFAULT 'medium',
                        frequency   VARCHAR(20) NOT NULL,
                        target_type VARCHAR(10) NOT NULL,
                        target_id   VARCHAR(100),
                        due_time    VARCHAR(10),
                        active      BOOLEAN DEFAULT 1,
                        created_by  VARCHAR(100),
                        created_at  VARCHAR(100),
                        last_run    VARCHAR(20)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO task_templates
                    (id,title,description,priority,frequency,target_type,target_id,due_time,active,created_by,created_at,last_run)
                    SELECT
                    id,title,description,priority,frequency,target_type,target_id,due_time,active,created_by,created_at,last_run
                    FROM task_templates_old
                """))
                conn.execute(text("DROP TABLE task_templates_old"))
                conn.commit()
        except Exception:
            conn.rollback()

        # Backfill: any user row created before the 'active' column existed
        # should default to active rather than NULL.
        try:
            conn.execute(text("UPDATE users SET active = TRUE WHERE active IS NULL"))
            conn.commit()
        except Exception:
            conn.rollback()

        # Backfill: any user row created before the 'active' column existed
        # should default to active rather than NULL.
        try:
            conn.execute(text("UPDATE users SET active = TRUE WHERE active IS NULL"))
            conn.commit()
        except Exception:
            conn.rollback()

    os.makedirs(os.path.join(os.path.dirname(__file__), "uploads", "profile_photos"), exist_ok=True)


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "message": "TaskFlow Backend Running"})


# ─────────────────────────────────────────────
# SEED
# ─────────────────────────────────────────────

@app.route("/api/seed-update")
def seed_update():
    founder = db.session.get(User, "founder")
    if not founder:
        founder = User(
            id="founder",
            email="pgiworkflow@gmail.com",
            password=generate_password_hash("founder2025"),
            role="founder",
            name="Laven Lokesh B",
            initials="LL"
        )
        db.session.add(founder)
    else:
        founder.email    = "pgiworkflow@gmail.com"
        founder.password = generate_password_hash("founder2025")
        founder.name     = "Laven Lokesh B"
        founder.initials = "LL"

    employees = [
        dict(id="u_abinash",   email="abinashbolt@gmail.com",     name="Abinash R",        initials="AR", role="employee", team="technical"),
        dict(id="u_rahul",     email="mail2rahul.mk@gmail.com",   name="Rahul M",           initials="RM", role="employee", team="technical"),
        dict(id="u_amitesh",   email="amitesh4122005@gmail.com",  name="Amitesh M",         initials="AM", role="employee", team="technical"),
        dict(id="u_sadhana",   email="trainings.pgi@gmail.com",   name="Sadhana M",         initials="SM", role="employee", team="bizdev"),
        dict(id="u_prassanna", email="kpkkumar1619@gmail.com",    name="Prassanna Kumar K", initials="PK", role="employee", team="content"),
    ]

    created = []
    for emp in employees:
        existing = db.session.get(User, emp["id"])
        if not existing:
            new_user = User(
                id=emp["id"], email=emp["email"],
                password=generate_password_hash("emp2025"),
                role=emp["role"], name=emp["name"],
                initials=emp["initials"], team=emp["team"]
            )
            db.session.add(new_user)
            created.append(emp["name"])

    db.session.commit()
    return jsonify({"success": True, "message": "Database seeded successfully", "created_employees": created})


# ─────────────────────────────────────────────
# AUTH — LOGIN
# ─────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json()
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")
    role     = data.get("role")

    # founder_assistant has full Founder-level access and may log in via the
    # "Founder" tab.  The "Employee" tab also accepts them for backward
    # compatibility with existing bookmarks.
    FOUNDER_LIKE_ROLES = ["founder", "founder_assistant"]
    if role == "founder":
        user = User.query.filter(
            db.func.lower(User.email) == email,
            User.role.in_(FOUNDER_LIKE_ROLES)
        ).first()
    elif role == "employee":
        user = User.query.filter(
            db.func.lower(User.email) == email,
            User.role.in_(["employee", "founder_assistant", "trainer"])
        ).first()
    else:
        user = User.query.filter(
            db.func.lower(User.email) == email,
            User.role == role
        ).first()

    print(f"[LOGIN DEBUG] email={email!r} role={role!r}")
    print(f"[LOGIN DEBUG] user_found={bool(user)} db_role={(user.role if user else None)!r}")
    if not user:
        print("[LOGIN DEBUG] result: 404 User not found")
        return jsonify({"error": "User not found"}), 404
    pw_ok = check_password_hash(user.password, password)
    print(f"[LOGIN DEBUG] password_check_result={pw_ok}")
    if not pw_ok:
        print("[LOGIN DEBUG] result: 401 Invalid password")
        return jsonify({"error": "Invalid password"}), 401
    print("[LOGIN DEBUG] result: success, issuing token")

    token = create_access_token(identity=user.id)
    return jsonify({
        "token": token,
        "user": {
            "id": user.id, "email": user.email, "role": user.role,
            "name": user.name, "initials": user.initials,
            "team": user.team, "specialty": user.specialty,
            "profile_photo": user.profile_photo,
            "active": user.active if user.active is not None else True
        }
    })

# ─────────────────────────────────────────────
# CURRENT USER
# ─────────────────────────────────────────────

@app.route("/api/me")
@jwt_required()
def me():
    user = db.session.get(User, get_jwt_identity())
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "id": user.id, "email": user.email, "role": user.role,
        "name": user.name, "initials": user.initials,
        "team": user.team, "specialty": user.specialty,
        "profile_photo": user.profile_photo,
        "active": user.active if user.active is not None else True
    })

# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

@app.route("/api/users")
@jwt_required()
def get_users():
    return jsonify([{
        "id": u.id, "email": u.email, "role": u.role,
        "name": u.name, "initials": u.initials,
        "team": u.team, "specialty": u.specialty,
        "phone": u.phone, "department": u.department,
        "domain": u.domain, "joining_date": u.joining_date,
        "date_of_birth": u.date_of_birth,
        "profile_photo": u.profile_photo,
        "active": u.active if u.active is not None else True
    } for u in User.query.all()])


@app.route("/api/users", methods=["POST"])
@jwt_required()
def create_user():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Only founder can create users"}), 403

    data  = request.get_json()
    email = data.get("email", "").strip().lower()

    if User.query.filter(db.func.lower(User.email) == email).first():
        return jsonify({"error": "Email already exists"}), 400

    name     = data.get("name", "").strip()
    initials = data.get("initials") or "".join(w[0].upper() for w in name.split()[:2])

    dob = (data.get("date_of_birth") or "").strip() or None
    if dob:
        try:
            dob_date = datetime.datetime.strptime(dob, "%Y-%m-%d").date()
            if dob_date > today_ist():
                return jsonify({"error": "Date of birth cannot be a future date"}), 400
        except ValueError:
            return jsonify({"error": "Invalid date_of_birth format. Use YYYY-MM-DD"}), 400

    role_value = data.get("role", "employee")
    team_value = data.get("team")
    if role_value == "founder_assistant" and not team_value:
        team_value = "Founder Assistant"

    user = User(
        id=data.get("id") or ("u" + str(int(now_ist().timestamp() * 1000))),
        email=email,
        password=generate_password_hash(data.get("password", "emp2025")),
        role=role_value,
        name=name,
        initials=initials,
        team=team_value,
        specialty=data.get("specialty"),
        phone=(data.get("phone") or "").strip() or None,
        department=(data.get("department") or "").strip() or None,
        domain=(data.get("domain") or "").strip() or None,
        joining_date=(data.get("joining_date") or "").strip() or None,
        date_of_birth=dob,
        profile_photo=(data.get("profile_photo") or "").strip() or None,
        active=data.get("active", True)
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({"success": True, "user": {
        "id": user.id, "name": user.name, "email": user.email,
        "role": user.role, "initials": user.initials,
        "team": user.team, "specialty": user.specialty,
        "phone": user.phone, "department": user.department,
        "domain": user.domain, "joining_date": user.joining_date,
        "date_of_birth": user.date_of_birth,
        "profile_photo": user.profile_photo,
        "active": user.active
    }})


@app.route("/api/users/<user_id>", methods=["DELETE"])
@jwt_required()
def delete_user(user_id):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Only founder can delete users"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    db.session.delete(user)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/users/<user_id>", methods=["PUT"])
@jwt_required()
def update_user(user_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can edit users"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json() or {}

    new_email = (data.get("email") or "").strip().lower()
    if new_email and new_email != user.email.lower():
        import re
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new_email):
            return jsonify({"error": "Invalid email format"}), 400
        duplicate = User.query.filter(
            db.func.lower(User.email) == new_email,
            User.id != user_id
        ).first()
        if duplicate:
            return jsonify({"error": "Email already in use by another user"}), 400
        user.email = new_email

    new_name = (data.get("name") or "").strip()
    if new_name:
        user.name = new_name
        words = new_name.split()
        user.initials = "".join(w[0].upper() for w in words[:2])

    if "role"         in data: user.role         = (data["role"]         or "").strip() or user.role
    if "team"         in data: user.team         = (data["team"]         or "").strip() or None
    if "specialty"    in data: user.specialty    = (data["specialty"]    or "").strip() or None
    if "phone"        in data: user.phone        = (data["phone"]        or "").strip() or None
    if "department"   in data: user.department   = (data["department"]   or "").strip() or None
    if "domain"       in data: user.domain       = (data["domain"]       or "").strip() or None
    if "joining_date" in data: user.joining_date = (data["joining_date"] or "").strip() or None
    if "active"       in data: user.active       = bool(data["active"])

    if "date_of_birth" in data:
        dob = (data["date_of_birth"] or "").strip() or None
        if dob:
            try:
                dob_date = datetime.datetime.strptime(dob, "%Y-%m-%d").date()
                if dob_date > today_ist():
                    return jsonify({"error": "Date of birth cannot be a future date"}), 400
            except ValueError:
                return jsonify({"error": "Invalid date_of_birth format. Use YYYY-MM-DD"}), 400
        user.date_of_birth = dob

    if "profile_photo" in data:
        user.profile_photo = (data["profile_photo"] or "").strip() or None

    log_entry = AuditLog(
        action       = f"Founder updated {user.name}'s profile",
        performed_by = caller.name,
        timestamp    = now_ist().strftime("%b %d, %Y %I:%M %p")
    )
    db.session.add(log_entry)
    db.session.commit()

    return jsonify({"success": True, "user": {
        "id":           user.id,
        "name":         user.name,
        "email":        user.email,
        "role":         user.role,
        "initials":     user.initials,
        "team":         user.team,
        "specialty":    user.specialty,
        "phone":        user.phone,
        "department":   user.department,
        "domain":       user.domain,
        "joining_date": user.joining_date,
        "date_of_birth": user.date_of_birth,
        "profile_photo": user.profile_photo,
        "active":       user.active if user.active is not None else True
    }})

@app.route("/api/users/<user_id>/role", methods=["PATCH"])
@jwt_required()
def set_user_role(user_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can change roles"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    data = request.get_json() or {}
    new_role = (data.get("role") or "").strip()
    VALID_ROLES = {"founder", "founder_assistant", "employee", "intern", "trainer"}
    if new_role not in VALID_ROLES:
        return jsonify({"error": f"Invalid role. Must be one of {sorted(VALID_ROLES)}"}), 400
    user.role = new_role
    db.session.add(AuditLog(
        action=f"{caller.name} changed {user.name}'s role to {new_role}",
        performed_by=caller.name,
        timestamp=now_ist().strftime("%b %d, %Y %I:%M %p")
    ))
    db.session.commit()
    return jsonify({"success": True, "user": {
        "id": user.id, "name": user.name, "role": user.role
    }})


@app.route("/api/users/<user_id>/photo", methods=["POST"])
@jwt_required()
def upload_profile_photo(user_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can upload profile photos"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if "photo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    photo = request.files["photo"]
    if not photo or photo.filename == "":
        return jsonify({"error": "Empty file"}), 400

    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
    MAX_SIZE_BYTES = 5 * 1024 * 1024
    ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Invalid file type '.{ext}'. Allowed: jpg, jpeg, png, webp"}), 400

    photo.seek(0, 2)
    file_size = photo.tell()
    photo.seek(0)
    if file_size > MAX_SIZE_BYTES:
        return jsonify({"error": "File too large. Maximum allowed size is 5 MB"}), 400

    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    photo_bytes = photo.read()
    b64_data = base64.b64encode(photo_bytes).decode("utf-8")
    photo_url = f"data:{mime_map.get(ext, 'image/jpeg')};base64,{b64_data}"

    user.profile_photo = photo_url
    db.session.commit()

    return jsonify({"success": True, "profile_photo": photo_url})


@app.route("/api/users/<user_id>/upload-photo", methods=["POST"])
@jwt_required()
def upload_profile_photo_legacy(user_id):
    return upload_profile_photo(user_id)


@app.route("/api/profile-photo/<filename>")
def serve_profile_photo(filename):
    safe = secure_filename(filename)
    path = os.path.join(os.path.dirname(__file__), "uploads", "profile_photos", safe)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    resp = send_file(path)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/uploads/profile_photos/<filename>")
def serve_profile_photo_legacy(filename):
    return serve_profile_photo(filename)

@app.route("/api/audit-logs")
@jwt_required()
def get_audit_logs():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(50).all()
    return jsonify([{
        "id": l.id, "action": l.action,
        "performed_by": l.performed_by, "timestamp": l.timestamp
    } for l in logs])


# ─────────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────────

def task_to_dict(t):
    proof = {"text": t.proof_text, "link": t.proof_link} if (t.proof_text or t.proof_link) else None
    return {
        "id": t.id, "title": t.title, "desc": t.desc,
        "assignedTo": t.assignedTo, "assignedBy": t.assignedBy,
        "status": t.status, "priority": t.priority,
        "due": t.due, "msg": t.msg, "createdAt": t.createdAt,
        "proof": proof,
        "rejection_reason": t.rejection_reason,
        "work_completion_percentage": t.work_completion_percentage or 0
    }


@app.route("/api/tasks")
@jwt_required()
def get_tasks():
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    if not user:
        return jsonify({"error": "User not found"}), 404
    tasks = Task.query.all() if is_founder_like(user) else Task.query.filter_by(assignedTo=uid).all()
    return jsonify([task_to_dict(t) for t in tasks])

def create_task_record(assignee_id, title, desc, priority, due, msg, assigned_by_id, work_completion_percentage=0.0):
    """Single source of truth for creating a Task + its notification.
    Used by both POST /api/tasks and the AI assign_task tool — never duplicated."""
    try:
        wcp = max(0.0, min(100.0, float(work_completion_percentage or 0)))
    except (TypeError, ValueError):
        wcp = 0.0
    task = Task(
        id        = "t" + str(int(now_ist().timestamp() * 1000)),
        title     = title,
        desc      = desc,
        assignedTo= assignee_id,
        assignedBy= assigned_by_id,
        status    = "pending",
        priority  = priority or "medium",
        due       = due or "TBD",
        msg       = msg or "",
        createdAt = now_ist().strftime("%b %d, %Y %I:%M %p"),
        work_completion_percentage = wcp
    )
    db.session.add(task)
    assigner = db.session.get(User, assigned_by_id)
    make_notif(
        userId=assignee_id, ntype="task",
        title="New Task Assigned",
        body='"' + task.title + '" was assigned to you by ' + (assigner.name if assigner else "the founder") + '. Due: ' + (task.due or "TBD")
    )

    # WhatsApp (best-effort — must never make task creation fail)
    try:
        assignee = db.session.get(User, assignee_id)
        employee_phone = (assignee.phone or "").strip() if assignee else ""
        employee_name  = assignee.name if assignee else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "task_assignment",
                [(employee_name or "there").split()[0], task.title, task.due or "TBD"]
            )
    except Exception as e:
        print(f"[WA] task_assignment send failed for {assignee_id}: {e}")

    return task


def find_employee_matches(query):
    """Case-insensitive lookup by full name, partial/first name, email, or initials.
    Never matches founder accounts. Returns a list — 0, 1, or many matches."""
    q = (query or "").strip().lower()
    if not q:
        return []
    candidates = User.query.filter(User.role != "founder").all()

    exact, partial = [], []
    seen_ids = set()

    for u in candidates:
        name_l     = (u.name or "").lower()
        email_l    = (u.email or "").lower()
        initials_l = (u.initials or "").lower()
        words      = name_l.split()

        if name_l == q or email_l == q or (initials_l and initials_l == q):
            if u.id not in seen_ids:
                exact.append(u); seen_ids.add(u.id)
        elif q in name_l or q in email_l or (words and q in words):
            if u.id not in seen_ids:
                partial.append(u); seen_ids.add(u.id)

    return exact if exact else partial


@app.route("/api/tasks", methods=["POST"])
@jwt_required()
def create_task():
    db.session.rollback()
    uid     = get_jwt_identity()
    founder = db.session.get(User, uid)
    if not founder:
        return jsonify({"error": "Founder account not found"}), 404
    if not is_founder_like(founder):
        return jsonify({"error": "Only founder can assign tasks"}), 403

    data        = request.get_json() or {}
    assignee_id = data.get("assignedTo", "").strip()
    if not assignee_id:
        return jsonify({"error": "Please select a team member to assign the task to"}), 400

    assignee = db.session.get(User, assignee_id)
    if not assignee:
        return jsonify({"error": "Selected team member not found."}), 404

    try:
        task = create_task_record(
            assignee_id = assignee_id,
            title       = (data.get("title") or "").strip(),
            desc        = (data.get("desc")  or "").strip(),
            priority    = data.get("priority", "medium"),
            due         = data.get("due") or "TBD",
            msg         = (data.get("msg") or "").strip(),
            assigned_by_id = uid,
            work_completion_percentage = data.get("work_completion_percentage", 0)
        )
        db.session.commit()
        return jsonify({"success": True, "task": task_to_dict(task)})
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Database error while creating task: " + str(e)}), 500


@app.route("/api/tasks/<task_id>/status", methods=["PATCH"])
@jwt_required()
def update_task_status(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    data = request.get_json()
    task.status = data.get("status")
    db.session.commit()

    # WhatsApp (best-effort — must never make the status update fail)
    try:
        assignee = db.session.get(User, task.assignedTo)
        employee_phone = (assignee.phone or "").strip() if assignee else ""
        employee_name  = assignee.name if assignee else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "task_update",
                [employee_name, f'"{task.title}" — {task.status}', ""]
            )
    except Exception as e:
        print(f"[WA] task_update send failed for task {task_id}: {e}")

    return jsonify({"success": True})


@app.route("/api/tasks/<task_id>/proof", methods=["POST"])
@jwt_required()
def submit_proof(task_id):
    uid  = get_jwt_identity()
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    data             = request.get_json()
    task.status      = "submitted"
    task.proof_text  = data.get("text")
    task.proof_link  = data.get("link")

    submitter = db.session.get(User, uid)
    # Notify all Founder-level accounts (founder + founder_assistant)
    founder_accounts = User.query.filter(
        User.role.in_(["founder", "founder_assistant"])
    ).all()
    for f in founder_accounts:
        if f.id != uid:   # don't notify the submitter themselves if they're founder-like
            make_notif(
                userId=f.id, ntype="proof",
                title="Proof Submitted",
                body=submitter.name + ' submitted proof for "' + task.title + '"'
            )
    db.session.commit()

    # WhatsApp (best-effort — must never make proof submission fail).
    # Mirrors the make_notif() recipients above: founders are told a proof came in.
    try:
        for f in founder_accounts:
            if f.id == uid:
                continue
            founder_phone = (f.phone or "").strip()
            if founder_phone:
                send_workflow_whatsapp(
                    founder_phone,
                    "task_update",
                    [f.name, f'"{task.title}" — proof submitted', ""]
                )
    except Exception as e:
        print(f"[WA] task_update (proof) send failed for task {task_id}: {e}")

    return jsonify({"success": True})


@app.route("/api/tasks/<task_id>/verify", methods=["POST"])
@jwt_required()
def verify_task(task_id):
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    if not is_founder_like(user):
        return jsonify({"error": "Only founder can verify"}), 403

    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    action = request.get_json().get("action")
    reason = request.get_json().get("reason", "").strip()

    if action == "approve":
        task.status = "completed"
        task.rejection_reason = None
        make_notif(
            userId=task.assignedTo, ntype="task",
            title="Task Completed!",
            body='Your task "' + task.title + '" was approved and marked complete!'
        )
    elif action == "reject":
        task.status     = "rejected"
        task.proof_text = None
        task.proof_link = None
        task.rejection_reason = reason if reason else None
        notif_body = 'Your proof for "' + task.title + '" was rejected. Please resubmit.'
        if reason:
            notif_body += ' Reason: ' + reason
        make_notif(
            userId=task.assignedTo, ntype="task",
            title="Proof Rejected",
            body=notif_body
        )

    db.session.commit()

    # WhatsApp (best-effort — must never make the verify action fail)
    try:
        assignee = db.session.get(User, task.assignedTo)
        employee_phone = (assignee.phone or "").strip() if assignee else ""
        employee_name  = assignee.name if assignee else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "task_update",
                [employee_name, f'"{task.title}" — {task.status}', task.rejection_reason or ""]
            )
    except Exception as e:
        print(f"[WA] task_update (verify) send failed for task {task_id}: {e}")

    return jsonify({"success": True})


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
@jwt_required()
def delete_task(task_id):
    user = db.session.get(User, get_jwt_identity())
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if not is_founder_like(user) and task.assignedTo != user.id:
        return jsonify({"error": "Forbidden"}), 403
    db.session.delete(task)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/tasks/<task_id>/proof", methods=["DELETE"])
@jwt_required()
def delete_proof(task_id):
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if not is_founder_like(user) and task.assignedTo != uid:
        return jsonify({"error": "Forbidden"}), 403

    if task.proof_link:
        try:
            if task.proof_link.startswith("/uploads/"):
                file_path = os.path.join(os.path.dirname(__file__), task.proof_link.lstrip("/"))
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            print(f"[WARN] Could not delete proof file: {e}")

    task.proof_text = None
    task.proof_link = None
    task.status     = "pending"
    db.session.commit()
    return jsonify({"success": True, "task": task_to_dict(task)})


@app.route("/api/tasks/<task_id>/work-percentage", methods=["PATCH"])
@jwt_required()
def update_work_percentage(task_id):
    user = db.session.get(User, get_jwt_identity())
    if not is_founder_like(user):
        return jsonify({"error": "Only founder can update work percentage"}), 403

    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    data = request.get_json() or {}
    try:
        pct = float(data.get("work_completion_percentage", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid percentage value"}), 400

    if pct < 0 or pct > 100:
        return jsonify({"error": "Percentage must be between 0 and 100"}), 400

    task.work_completion_percentage = pct
    db.session.commit()
    return jsonify({"success": True, "task": task_to_dict(task)})


@app.route("/api/tasks/reset-completed", methods=["DELETE"])
@jwt_required()
def reset_completed_tasks():
    user = db.session.get(User, get_jwt_identity())
    if not is_founder_like(user):
        return jsonify({"error": "Only founder can reset tasks"}), 403
    completed = Task.query.filter_by(status="completed").all()
    count = len(completed)
    for task in completed:
        db.session.delete(task)
    db.session.commit()
    return jsonify({"success": True, "deleted": count})

# ─────────────────────────────────────────────
# DAILY TASK UPDATES
# ─────────────────────────────────────────────

def daily_update_to_dict(d):
    return {
        "id": d.id, "userId": d.userId, "employee_name": d.employee_name,
        "department": d.department, "designation": d.designation,
        "date": d.date, "today_tasks": d.today_tasks,
        "achievements": d.achievements, "blockers": d.blockers,
        "tomorrow_plan": d.tomorrow_plan, "total_hours": d.total_hours,
        "work_status": d.work_status, "founder_review": d.founder_review,
        "reviewed": d.reviewed, "submitted_at": d.submitted_at,
        "reviewed_at": d.reviewed_at
    }


@app.route("/api/daily-task-update", methods=["POST"])
@jwt_required()
def create_daily_task_update():
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    if not user:
        return jsonify({"error": "User not found"}), 404

    data          = request.get_json() or {}
    today_tasks   = (data.get("today_tasks") or "").strip()
    achievements  = (data.get("achievements") or "").strip()
    tomorrow_plan = (data.get("tomorrow_plan") or "").strip()
    if not today_tasks or not tomorrow_plan:
        return jsonify({"error": "Today's Tasks and Tomorrow Plan are required"}), 400
    today    = today_str()
    existing = DailyTaskUpdate.query.filter_by(userId=uid, date=today).first()

    if existing:
        existing.today_tasks   = today_tasks
        existing.achievements  = achievements
        existing.blockers      = (data.get("blockers") or "").strip()
        existing.tomorrow_plan = tomorrow_plan
        existing.total_hours   = data.get("total_hours")
        existing.department    = user.department or existing.department
        existing.designation   = user.specialty or user.role
        record = existing
        is_new = False
    else:
        record = DailyTaskUpdate(
            userId         = uid,
            employee_name  = user.name,
            department     = user.department or "",
            designation    = user.specialty or user.role,
            date           = today,
            today_tasks    = today_tasks,
            achievements   = achievements,
            blockers       = (data.get("blockers") or "").strip(),
            tomorrow_plan  = tomorrow_plan,
            total_hours    = data.get("total_hours"),
            work_status    = "Submitted",
            founder_review = None,
            reviewed       = False,
            submitted_at   = now_ist().strftime("%b %d, %Y %I:%M %p"),
            reviewed_at    = None
        )
        db.session.add(record)
        is_new = True

    db.session.flush()

    if is_new:
        for f in User.query.filter(User.role.in_(["founder", "founder_assistant"])).all():
            if f.id != uid:
                make_notif(
                    userId=f.id, ntype="daily_update",
                    title="Daily Task Update",
                    body=f"{user.name} submitted today's Daily Task Update."
                )

    db.session.commit()
    return jsonify({"success": True, "update": daily_update_to_dict(record)})


@app.route("/api/daily-task-update/my", methods=["GET"])
@jwt_required()
def get_my_daily_task_updates():
    uid  = get_jwt_identity()
    rows = DailyTaskUpdate.query.filter_by(userId=uid).order_by(DailyTaskUpdate.id.desc()).all()
    return jsonify([daily_update_to_dict(r) for r in rows])


@app.route("/api/daily-task-update/all", methods=["GET"])
@jwt_required()
def get_all_daily_task_updates():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    rows = DailyTaskUpdate.query.order_by(DailyTaskUpdate.id.desc()).all()
    return jsonify([daily_update_to_dict(r) for r in rows])


@app.route("/api/daily-task-update/review/<int:update_id>", methods=["PUT"])
@jwt_required()
def review_daily_task_update(update_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    record = db.session.get(DailyTaskUpdate, update_id)
    if not record:
        return jsonify({"error": "Update not found"}), 404

    data = request.get_json() or {}
    record.founder_review = (data.get("founder_review") or "").strip()
    record.work_status    = data.get("work_status") or "Reviewed"
    record.reviewed        = True
    record.reviewed_at     = now_ist().strftime("%b %d, %Y %I:%M %p")

    make_notif(
        userId=record.userId, ntype="daily_update",
        title="Daily Task Update Reviewed",
        body="Your Daily Task Update has been reviewed."
    )

    db.session.commit()
    return jsonify({"success": True, "update": daily_update_to_dict(record)})


@app.route("/api/daily-task-update/<int:update_id>", methods=["DELETE"])
@jwt_required()
def delete_daily_task_update(update_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    record = db.session.get(DailyTaskUpdate, update_id)
    if not record:
        return jsonify({"error": "Update not found"}), 404
    db.session.delete(record)
    db.session.commit()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────

@app.route("/api/messages")
@jwt_required()
def get_messages():
    channel = request.args.get("channel", "all")
    msgs    = Message.query.filter_by(channel=channel).order_by(Message.id.asc()).all()
    return jsonify([{
        "id": m.id, "from": m.from_name, "fromId": m.fromId,
        "text": m.text, "time": m.time, "channel": m.channel,
        "timestamp": m.timestamp or ""
    } for m in msgs])


@app.route("/api/messages", methods=["POST"])
@jwt_required()
def send_message():
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    data = request.get_json()
    channel = data.get("channel", "all")

    now = now_ist()
    msg = Message(
        id        = "m" + str(int(now.timestamp() * 1000)),
        from_name = user.name,
        fromId    = uid,
        text      = data.get("text", "").strip(),
        time      = now.strftime("%I:%M %p"),
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S"),
        channel   = channel
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "message": {
        "id": msg.id, "from": msg.from_name, "fromId": msg.fromId,
        "text": msg.text, "time": msg.time, "channel": msg.channel,
        "timestamp": msg.timestamp or ""
    }})


@app.route("/api/messages/count")
@jwt_required()
def message_counts():
    channels_param = request.args.get("channels", "all")
    channels = channels_param.split(",")
    result = {}
    for ch in channels:
        result[ch.strip()] = Message.query.filter_by(channel=ch.strip()).count()
    return jsonify(result)


# ─────────────────────────────────────────────
# MORNING MESSAGE
# ─────────────────────────────────────────────

def send_morning_email(sender_name, message_text, recipients):
    if not recipients:
        return False, "No recipient email addresses found."

    brevo_key     = os.getenv("BREVO_API_KEY", "").strip()
    smtp_email    = os.getenv("SMTP_EMAIL", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()

    plain_body = (
        f"Good morning, {sender_name} here\n\n"
        f"{message_text}\n\n"
        f"Have a productive day!\n-- {sender_name}, Plant Green Inertia"
    )
    html_body = (
        '''<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;'''
        '''background:#fffdf5;border:1px solid #e5d98b;border-radius:8px">'''
        f'''<h2 style="color:#b8860b;margin-top:0">Good morning, {sender_name} here</h2>'''
        f'''<p style="color:#333;font-size:15px;line-height:1.7">{message_text}</p>'''
        '''<hr style="border:none;border-top:1px solid #e5d98b;margin:20px 0">'''
        f'''<p style="color:#888;font-size:13px">Have a productive day! &mdash;'''
        f''' <strong>{sender_name}</strong>, Plant Green Inertia</p>'''
        '''</div>'''
    )

    if brevo_key:
        sent   = 0
        failed = []
        for recipient in recipients:
            payload = json.dumps({
                "sender":      {"name": "Plant Green Inertia", "email": smtp_email or "pgiworkflow@gmail.com"},
                "to":          [{"email": recipient}],
                "subject":     f"Morning Message from {sender_name} - Plant Green Inertia",
                "textContent": plain_body,
                "htmlContent": html_body,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.brevo.com/v3/smtp/email",
                data    = payload,
                headers = {
                    "api-key":      brevo_key,
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                },
                method = "POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
                    sent += 1
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                failed.append(recipient)
                print(f"[EMAIL/Brevo] HTTPError {e.code} for {recipient}: {body}")
            except Exception as exc:
                failed.append(recipient)
                print(f"[EMAIL/Brevo] Error for {recipient}: {exc}")

        if sent:
            return True, f"Sent via Brevo to {sent} recipient(s). Failed: {len(failed)}."
        return False, f"Brevo: all {len(failed)} sends failed."

    if not smtp_email or not smtp_password:
        return False, "No email service configured."

    sent   = []
    failed = []
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(smtp_email, smtp_password)
            for recipient in recipients:
                try:
                    mime_msg = MIMEMultipart("alternative")
                    mime_msg["Subject"] = f"Morning Message from {sender_name} - Plant Green Inertia"
                    mime_msg["From"]    = f"Plant Green Inertia <{smtp_email}>"
                    mime_msg["To"]      = recipient
                    mime_msg.attach(MIMEText(plain_body, "plain", "utf-8"))
                    mime_msg.attach(MIMEText(html_body,  "html",  "utf-8"))
                    server.sendmail(smtp_email, recipient, mime_msg.as_string())
                    sent.append(recipient)
                except Exception as per_exc:
                    failed.append(recipient)
                    print(f"[EMAIL/SMTP] Failed for {recipient}: {per_exc}")
    except smtplib.SMTPAuthenticationError as auth_err:
        return False, f"Gmail auth failed: {auth_err}."
    except Exception as conn_err:
        return False, f"SMTP connection error: {conn_err}"

    if sent:
        return True, f"Sent via SMTP to {len(sent)} recipient(s). Failed: {len(failed)}."
    return False, f"SMTP: all {len(failed)} sends failed."


@app.route("/api/morning-message", methods=["GET"])
@jwt_required()
def get_morning_message():
    msg = MorningMessage.query.filter_by(date=today_str()).order_by(MorningMessage.id.desc()).first()
    if not msg:
        return jsonify({})
    return jsonify({"text": msg.text, "from": msg.from_name, "time": msg.time})


@app.route("/api/morning-message", methods=["POST"])
@jwt_required()
def post_morning_message():
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    data = request.get_json()

    msg = MorningMessage(
        text=data.get("text"), from_name=user.name,
        time=now_str(), date=today_str()
    )
    db.session.add(msg)

    message_text = str(data.get("text", ""))
    # Send to everyone who is NOT a founder-level account
    non_founders = User.query.filter(
        User.role != "founder"
    ).all()
    for u in non_founders:
        make_notif(
            userId=u.id, ntype="morning",
            title="Morning Message",
            body=message_text[:120]
        )

    db.session.commit()

    recipient_emails = [u.email for u in non_founders if u.email]
    email_sent, email_detail = send_morning_email(user.name, message_text, recipient_emails)

    return jsonify({"success": True, "email_sent": email_sent, "email_detail": email_detail, "recipients": recipient_emails})


@app.route("/api/test-email")
@jwt_required()
def test_email():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    smtp_email    = os.getenv("SMTP_EMAIL", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    brevo_key     = os.getenv("BREVO_API_KEY", "").strip()

    success, detail = send_morning_email(
        sender_name  = caller.name,
        message_text = "TEST: This is a TaskFlow test email confirming that email delivery is working correctly.",
        recipients   = [smtp_email]
    )

    return jsonify({
        "success":       success,
        "detail":        detail,
        "smtp_email":    smtp_email,
        "smtp_pass_set": bool(smtp_password),
        "brevo_key_set": bool(brevo_key),
        "email_service": "Brevo (HTTP)" if brevo_key else "SMTP (Gmail)"
    })


# ─────────────────────────────────────────────
# RATINGS
# ─────────────────────────────────────────────

@app.route("/api/ratings", methods=["GET"])
@jwt_required()
def get_ratings():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    today_ratings = {
        r.userId: {"score": r.score, "note": r.note}
        for r in Rating.query.filter_by(date=today_str()).all()
    }

    thirty_days_ago = (now_ist() - datetime.timedelta(days=29)).strftime("%Y-%m-%d")
    history = [
        {"date": r.date, "userId": r.userId, "score": r.score}
        for r in Rating.query
            .filter(Rating.date >= thirty_days_ago)
            .order_by(Rating.date)
            .all()
    ]

    return jsonify({"today": today_ratings, "history": history})


@app.route("/api/ratings", methods=["POST"])
@jwt_required()
def save_rating():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    data  = request.get_json()
    uid   = data.get("userId")
    score = int(data.get("score", 0))
    note  = data.get("note", "").strip()

    if not uid or score < 1 or score > 10:
        return jsonify({"error": "Invalid data"}), 400

    existing = Rating.query.filter_by(userId=uid, date=today_str()).first()
    if existing:
        existing.score = score
        existing.note  = note or existing.note
    else:
        db.session.add(Rating(userId=uid, score=score, note=note, date=today_str()))

    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/ratings/reset", methods=["POST"])
@jwt_required()
def reset_ratings():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    count = Rating.query.count()
    Rating.query.delete()
    db.session.commit()
    return jsonify({"success": True, "deleted": count})


# ─────────────────────────────────────────────
# ATTENDANCE  (with timestamps)
# ─────────────────────────────────────────────

# Attendance % is never calculated using records older than this date,
# even though older attendance rows stay untouched in the database.
ATTENDANCE_START_DATE = datetime.date(2026, 6, 1)
COMPANY_HOLIDAYS = {
    # "2026-08-15",
}

# ─────────────────────────────────────────────
# STEP 6 — ROLE-BASED WORKING DAY CONFIGURATION
# Single source of truth. Never hardcode Sunday/weekend logic elsewhere —
# every attendance calculation must read from this.
# Keys match User.team values used across the app.
# ─────────────────────────────────────────────
ROLE_WORKING_RULES = {
    "technical": {
        "working_days": [1, 2, 3, 4, 5, 6],
        "weekly_off":   ["Sunday", "Second Saturday", "Fourth Saturday"],
    },
    "content": {
        "working_days": [1, 2, 3, 4, 5, 6],
        "weekly_off":   ["Sunday"],
    },
    "bizdev": {
        "working_days": [0, 1, 2, 3, 4, 5, 6],
        "weekly_off":   [],
    },
    "Founder Assistant": {
        "working_days": [0, 1, 2, 3, 4, 5, 6],
        "weekly_off":   [],
    },
}

# Fallback for teams not in the config above (e.g. interns, trainers with no
# team) — preserves prior Sunday-only-off behavior rather than guessing.
_DEFAULT_WORKING_RULE = {"working_days": [1, 2, 3, 4, 5, 6], "weekly_off": ["Sunday"]}


def _working_rule_for_team(team):
    return ROLE_WORKING_RULES.get(team, _DEFAULT_WORKING_RULE)


def _nth_saturday_label(date_obj):
    if date_obj.weekday() != 5:  # Python: Saturday == 5
        return None
    nth = (date_obj.day - 1) // 7 + 1
    return {1: "First Saturday", 2: "Second Saturday", 3: "Third Saturday",
            4: "Fourth Saturday", 5: "Fifth Saturday"}.get(nth)


def is_weekly_off(date_obj, team):
    off_days = _working_rule_for_team(team)["weekly_off"]
    if not off_days:
        return False
    if date_obj.weekday() == 6 and "Sunday" in off_days:  # Python: Sunday == 6
        return True
    sat_label = _nth_saturday_label(date_obj)
    return bool(sat_label and sat_label in off_days)


def is_holiday(date_obj, team):
    """STEP 3: Sunday/weekly-off → Holiday. Holidays never increase Leave,
    Absent, Working Days, or reduce Attendance %."""
    return date_obj.isoformat() in COMPANY_HOLIDAYS or is_weekly_off(date_obj, team)


def _user_team_key(user):
    if user.role == "founder_assistant":
        return "Founder Assistant"
    return user.team


def _parse_user_date(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def get_working_days(start_date, end_date, team):
    """STEP 6: role-aware working-day count. Excludes weekly-off days per
    team, company holidays, and future dates."""
    if not start_date:
        return 0
    end_date = min(end_date, today_ist())
    if start_date > end_date:
        return 0
    count, cur, one_day = 0, start_date, datetime.timedelta(days=1)
    while cur <= end_date:
        if not is_holiday(cur, team):
            count += 1
        cur += one_day
    return count


def get_present_days(user_id, start_date, end_date):
    """Present = status in ('present','completed') only. Leave is counted
    separately — see get_leave_days()."""
    if not start_date:
        return 0
    end_date = min(end_date, today_ist())
    if start_date > end_date:
        return 0
    rows = Attendance.query.filter(
        Attendance.userId == user_id,
        Attendance.status.in_(["present", "completed"]),
        Attendance.date >= start_date.isoformat(),
        Attendance.date <= end_date.isoformat(),
    ).all()
    return len({r.date for r in rows})


def get_leave_days(user_id, start_date, end_date, team):
    """STEP 4: Leave increases ONLY for approved leave (status == 'leave',
    which is only ever set by decide_leave_request on approval). Any date
    that's a weekly-off/holiday for this team is excluded — holidays never
    inflate the leave count."""
    if not start_date:
        return 0
    end_date = min(end_date, today_ist())
    if start_date > end_date:
        return 0
    rows = Attendance.query.filter(
        Attendance.userId == user_id,
        Attendance.status == "leave",
        Attendance.date >= start_date.isoformat(),
        Attendance.date <= end_date.isoformat(),
    ).all()
    leave_dates = set()
    for r in rows:
        d = _parse_user_date(r.date)
        if d and not is_holiday(d, team):
            leave_dates.add(r.date)
    return len(leave_dates)


def _attendance_window(user, scope="month"):
    team = _user_team_key(user)
    today = today_ist()
    join_date  = _parse_user_date(getattr(user, "joining_date", None))
    floor_date = ATTENDANCE_START_DATE if not join_date else max(ATTENDANCE_START_DATE, join_date)
    start_date = floor_date if scope == "cumulative" else max(floor_date, today.replace(day=1))
    return team, start_date, today


def _attendance_stats_for_range(user, team, start_date, end_date):

    
    """STEP 7: Attendance % = (Present + ApprovedLeave) / WorkingDaysTillToday × 100.
    Only Absent reduces attendance. Leave and Holiday never reduce it."""
    working_days = get_working_days(start_date, end_date, team)
    present_days = get_present_days(user.id, start_date, end_date)
    leave_days   = get_leave_days(user.id, start_date, end_date, team)
    absent_days  = max(working_days - present_days - leave_days, 0)
    percentage   = 0 if working_days == 0 else round(((present_days + leave_days) / working_days) * 100, 2)
    return {
        "percentage":         percentage,
        "present_days":       present_days,
        "leave_days":         leave_days,
        "absent_days":        absent_days,
        "total_working_days": working_days,
    }

def _month_bounds(year, month):
    start = datetime.date(year, month, 1)
    end   = (datetime.date(year+1, 1, 1) if month == 12 else datetime.date(year, month+1, 1)) - datetime.timedelta(days=1)
    return start, end


def get_monthly_attendance_rows(user):
    team = _user_team_key(user)
    join_date  = _parse_user_date(getattr(user, "joining_date", None))
    floor_date = ATTENDANCE_START_DATE if not join_date else max(ATTENDANCE_START_DATE, join_date)
    today = today_ist()
    rows, y, m = [], floor_date.year, floor_date.month
    while (y, m) <= (today.year, today.month):
        m_start, m_end = _month_bounds(y, m)
        r_start, r_end = max(m_start, floor_date), min(m_end, today)
        if r_start <= r_end:
            stats = _attendance_stats_for_range(user, team, r_start, r_end)
            rows.append({"month": f"{y:04d}-{m:02d}", "month_label": r_start.strftime("%B %Y"), **stats})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return rows


@app.route("/api/attendance/monthly-report", methods=["GET"])
@jwt_required()
def attendance_monthly_report():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    users = User.query.filter(User.role != "founder").all()
    return jsonify([
        {"userId": u.id, "name": u.name, "team": u.team, "role": u.role,
         "months": get_monthly_attendance_rows(u)}
        for u in users
    ])


@app.route("/api/attendance/daily-report", methods=["GET"])
@jwt_required()
def attendance_daily_report():
    """
    Returns one row per employee per day (Attendance Start Date -> today),
    for the Export CSV feature only. Does NOT aggregate by month and does
    NOT touch attendance percentage / cumulative report calculations.
    """
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    users = User.query.filter(User.role != "founder").all()
    today = today_ist()

    # Preload attendance rows once for efficiency
    all_att = Attendance.query.filter(Attendance.date >= ATTENDANCE_START_DATE.isoformat()).all()
    att_map = {(a.userId, a.date): a for a in all_att}

    result = []
    one_day = datetime.timedelta(days=1)

    for u in users:
        team = _user_team_key(u)
        join_date  = _parse_user_date(getattr(u, "joining_date", None))
        floor_date = ATTENDANCE_START_DATE if not join_date else max(ATTENDANCE_START_DATE, join_date)
        if floor_date > today:
            continue

        cur = floor_date
        while cur <= today:
            date_str = cur.isoformat()
            day_name = cur.strftime("%A")

            if is_holiday(cur, team):
                result.append({
                    "employee_name": u.name, "role": u.role, "team": team or "",
                    "date": date_str, "day": day_name, "status": "Holiday",
                    "checkin_time": "", "checkout_time": "", "worked_hours": ""
                })
            else:
                att = att_map.get((u.id, date_str))
                if att and att.status in ("present", "completed"):
                    result.append({
                        "employee_name": u.name, "role": u.role, "team": team or "",
                        "date": date_str, "day": day_name, "status": "Present",
                        "checkin_time":  att.checkin_time  or "",
                        "checkout_time": att.checkout_time or "",
                        "worked_hours":  att.worked_hours if att.worked_hours is not None else ""
                    })
                elif att and att.status == "leave":
                    result.append({
                        "employee_name": u.name, "role": u.role, "team": team or "",
                        "date": date_str, "day": day_name, "status": "Approved Leave",
                        "checkin_time": "", "checkout_time": "", "worked_hours": ""
                    })
                else:
                    result.append({
                        "employee_name": u.name, "role": u.role, "team": team or "",
                        "date": date_str, "day": day_name, "status": "Absent",
                        "checkin_time": "", "checkout_time": "", "worked_hours": ""
                    })
            cur += one_day

    return jsonify(result)


def attendance_stats_for_user(user, scope="month"):
    """
    scope="month" (default): STEP 5 — resets on the 1st of every month.
    Used by the sidebar, the employee's own attendance card, and the AI assistant.
    scope="cumulative": STEP 8 — the Cumulative Attendance Report table, spanning
    every month since ATTENDANCE_START_DATE / joining date. Never resets.
    """
    team, start_date, today = _attendance_window(user, scope)
    stats = _attendance_stats_for_range(user, team, start_date, today)
    stats["calculation_start_date"] = start_date.isoformat()
    stats["month"]  = today.strftime("%Y-%m")
    stats["scope"]  = scope
    return stats


def calculate_attendance_percentage(user):
    return attendance_stats_for_user(user, scope="month")["percentage"]


@app.route("/api/attendance/percentage", methods=["GET"])
@jwt_required()
def attendance_percentage():
    caller = db.session.get(User, get_jwt_identity())
    if not caller:
        return jsonify([])
    scope = request.args.get("scope", "month")
    if scope not in ("month", "cumulative"):
        scope = "month"
    users = User.query.filter(User.role != "founder").all() if is_founder_like(caller) else [caller]
    return jsonify([
        {"userId": u.id, "name": u.name, **attendance_stats_for_user(u, scope=scope)}
        for u in users
    ])


def attendance_to_dict(att_row, user_obj=None):
    """Serialize an Attendance row, enriching with user info if provided."""
    d = {
        "userId":             att_row.userId,
        "status":             att_row.status,
        "date":               att_row.date,
        "checkin_time":       att_row.checkin_time  or "",
        "checkout_time":      att_row.checkout_time or "",
        "worked_hours":       att_row.worked_hours,
        "checkin_latitude":   att_row.checkin_latitude,
        "checkin_longitude":  att_row.checkin_longitude,
        "checkout_latitude":  att_row.checkout_latitude,
        "checkout_longitude": att_row.checkout_longitude,
    }
    if user_obj:
        d.update({
            "name":     user_obj.name,
            "initials": user_obj.initials,
            "role":     user_obj.role,
            "team":     user_obj.team,
        })
    return d


@app.route("/api/attendance", methods=["GET"])
@jwt_required()
def get_attendance():
    today   = today_str()
    records = {a.userId: a for a in Attendance.query.filter_by(date=today).all()}
    users   = User.query.filter(
        User.role != "founder"
    ).all()
    result  = []
    for u in users:
        att = records.get(u.id)
        result.append({
            "userId":             u.id,
            "name":               u.name,
            "initials":           u.initials,
            "role":               u.role,
            "team":               u.team,
            "profile_photo":      u.profile_photo,
            "status":             att.status             if att else "absent",
            "date":               today,
            "checkin_time":       att.checkin_time       if att else "",
            "checkout_time":      att.checkout_time      if att else "",
            "worked_hours":       att.worked_hours       if att else None,
            "checkin_latitude":   att.checkin_latitude   if att else None,
            "checkin_longitude":  att.checkin_longitude  if att else None,
            "checkout_latitude":  att.checkout_latitude  if att else None,
            "checkout_longitude": att.checkout_longitude if att else None,
        })
    return jsonify(result)


@app.route("/api/attendance", methods=["PATCH"])
@jwt_required()
def mark_attendance():
    data      = request.get_json()
    uid       = data.get("userId")
    status    = data.get("status")
    today     = today_str()
    now_ts    = now_iso()

    att = Attendance.query.filter_by(userId=uid, date=today).first()
    if att:
        prev_status = att.status
        att.status  = status
        # Record check-in time when toggling to present
        if status == "present" and prev_status != "present":
            att.checkin_time  = now_ts
            att.checkout_time = None   # reset checkout on re-check-in
        # Record checkout time when toggling to absent after being present
        elif status == "absent" and prev_status == "present":
            att.checkout_time = now_ts
    else:
        checkin  = now_ts if status == "present" else None
        checkout = None
        att = Attendance(
            userId=uid, status=status, date=today,
            checkin_time=checkin, checkout_time=checkout
        )
        db.session.add(att)

    db.session.commit()
    return jsonify({
        "success":       True,
        "status":        att.status,
        "date":          today,
        "checkin_time":  att.checkin_time  or "",
        "checkout_time": att.checkout_time or "",
        "worked_hours":  att.worked_hours,
    })


def _compute_worked_hours(checkin_iso, checkout_iso):
    """Return hours (float, 2dp) between two ISO datetime strings, or None."""
    try:
        ci = datetime.datetime.fromisoformat(checkin_iso)
        co = datetime.datetime.fromisoformat(checkout_iso)
        return round((co - ci).total_seconds() / 3600.0, 2)
    except (ValueError, TypeError):
        return None


@app.route("/api/attendance/checkin", methods=["POST"])
@jwt_required()
def checkin_attendance():
    """
    Explicit Check-In action.
    - Enabled once per day: if a check-in already exists for today, reject.
    - Sets status -> 'present', records checkin_time and optional geo coords.
    """
    data  = request.get_json() or {}
    uid   = data.get("userId") or get_jwt_identity()
    today = today_str()
    now_ts = now_iso()

    # Parse optional geo coords — float or None
    def _parse_coord(val):
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    lat = _parse_coord(data.get("latitude"))
    lng = _parse_coord(data.get("longitude"))

    att = Attendance.query.filter_by(userId=uid, date=today).first()
    if att and att.checkin_time:
        return jsonify({"error": "Already checked in today"}), 400

    if att:
        att.status              = "present"
        att.checkin_time        = now_ts
        att.checkout_time       = None
        att.worked_hours        = None
        att.checkin_latitude    = lat
        att.checkin_longitude   = lng
        att.checkout_latitude   = None
        att.checkout_longitude  = None
    else:
        att = Attendance(
            userId=uid, status="present", date=today,
            checkin_time=now_ts, checkout_time=None, worked_hours=None,
            checkin_latitude=lat, checkin_longitude=lng,
            checkout_latitude=None, checkout_longitude=None
        )
        db.session.add(att)

    db.session.commit()

    # WhatsApp (best-effort — must never make check-in fail)
    try:
        emp = db.session.get(User, uid)
        employee_phone = (emp.phone or "").strip() if emp else ""
        employee_name  = emp.name if emp else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "attendance",
                [employee_name, att.status, att.checkin_time or ""]
            )
    except Exception as e:
        print(f"[WA] attendance (checkin) send failed for {uid}: {e}")

    return jsonify({
        "success":            True,
        "status":             att.status,
        "date":               today,
        "checkin_time":       att.checkin_time  or "",
        "checkout_time":      att.checkout_time or "",
        "worked_hours":       att.worked_hours,
        "checkin_latitude":   att.checkin_latitude,
        "checkin_longitude":  att.checkin_longitude,
    })


@app.route("/api/attendance/checkout", methods=["POST"])
@jwt_required()
def checkout_attendance():
    """
    Explicit Check-Out action.
    - Enabled only after check-in: requires an existing checkin_time today.
    - Does NOT overwrite checkin_time.
    - Records checkout_time, geo coords, computes worked_hours, status -> 'completed'.
    """
    data  = request.get_json() or {}
    uid   = data.get("userId") or get_jwt_identity()
    today = today_str()
    now_ts = now_iso()

    def _parse_coord(val):
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    lat = _parse_coord(data.get("latitude"))
    lng = _parse_coord(data.get("longitude"))

    att = Attendance.query.filter_by(userId=uid, date=today).first()
    if not att or not att.checkin_time:
        return jsonify({"error": "You must check in before checking out"}), 400
    if att.checkout_time:
        return jsonify({"error": "Already checked out today"}), 400

    att.checkout_time      = now_ts
    att.worked_hours       = _compute_worked_hours(att.checkin_time, att.checkout_time)
    att.status             = "completed"
    att.checkout_latitude  = lat
    att.checkout_longitude = lng

    db.session.commit()

    # WhatsApp (best-effort — must never make check-out fail)
    try:
        emp = db.session.get(User, uid)
        employee_phone = (emp.phone or "").strip() if emp else ""
        employee_name  = emp.name if emp else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "attendance",
                [employee_name, att.status, att.checkout_time or ""]
            )
    except Exception as e:
        print(f"[WA] attendance (checkout) send failed for {uid}: {e}")

    return jsonify({
        "success":             True,
        "status":              att.status,
        "date":                today,
        "checkin_time":        att.checkin_time  or "",
        "checkout_time":       att.checkout_time or "",
        "worked_hours":        att.worked_hours,
        "checkin_latitude":    att.checkin_latitude,
        "checkin_longitude":   att.checkin_longitude,
        "checkout_latitude":   att.checkout_latitude,
        "checkout_longitude":  att.checkout_longitude,
    })


@app.route("/api/attendance/map")
@jwt_required()
def attendance_map():
    """
    Founder-only: return today's checked-in employees with their geo coordinates.
    Only includes employees who have a checkin_latitude/longitude recorded.
    """
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    today   = today_str()
    records = Attendance.query.filter_by(date=today).all()

    result = []
    for a in records:
        if a.checkin_latitude is None or a.checkin_longitude is None:
            continue
        user = db.session.get(User, a.userId)
        if not user:
            continue
        result.append({
            "userId":             a.userId,
            "name":               user.name,
            "initials":           user.initials,
            "role":               user.role,
            "team":               user.team,
            "profile_photo":      user.profile_photo,
            "status":             a.status,
            "checkin_time":       a.checkin_time  or "",
            "checkout_time":      a.checkout_time or "",
            "checkin_latitude":   a.checkin_latitude,
            "checkin_longitude":  a.checkin_longitude,
            "checkout_latitude":  a.checkout_latitude,
            "checkout_longitude": a.checkout_longitude,
        })
    return jsonify(result)


@app.route("/api/attendance/history")
@jwt_required()
def attendance_history():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    records = Attendance.query.all()
    return jsonify([{
        "userId":             a.userId,
        "status":             a.status,
        "date":               a.date,
        "checkin_time":       a.checkin_time  or "",
        "checkout_time":      a.checkout_time or "",
        "worked_hours":       a.worked_hours,
        "checkin_latitude":   a.checkin_latitude,
        "checkin_longitude":  a.checkin_longitude,
        "checkout_latitude":  a.checkout_latitude,
        "checkout_longitude": a.checkout_longitude,
    } for a in records])

# ─────────────────────────────────────────────
# LEAVE REQUESTS
# ─────────────────────────────────────────────

@app.route("/api/leave-requests", methods=["GET"])
@jwt_required()
def get_leave_requests():
    caller = db.session.get(User, get_jwt_identity())
    if caller.role == "founder":
        rows = LeaveRequest.query.order_by(LeaveRequest.id.desc()).all()
    else:
        rows = LeaveRequest.query.filter_by(userId=caller.id).order_by(LeaveRequest.id.desc()).all()
    return jsonify([{
        "id": r.id, "userId": r.userId, "date": r.date, "reason": r.reason,
        "status": r.status, "requested_at": r.requested_at,
        "decided_at": r.decided_at, "decided_by": r.decided_by,
    } for r in rows])


@app.route("/api/leave-requests", methods=["POST"])
@jwt_required()
def create_leave_request():
    uid  = get_jwt_identity()
    data = request.get_json() or {}
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"error": "date is required"}), 400
    lr = LeaveRequest(
        userId=uid, date=date,
        reason=(data.get("reason") or "").strip(),
        status="pending",
        requested_at=now_ist().strftime("%b %d, %Y %I:%M %p")
    )
    db.session.add(lr)
    db.session.commit()
    requester = db.session.get(User, uid)
    for f in User.query.filter(User.role.in_(["founder", "founder_assistant"])).all():
        make_notif(userId=f.id, ntype="leave", title="📝 Leave Request",
                   body=f"{requester.name} requested leave for {date}")
    db.session.commit()

    # WhatsApp (best-effort — must never make the leave request fail).
    # Mirrors the make_notif() recipients above: founders are told a request came in.
    try:
        for f in User.query.filter(User.role.in_(["founder", "founder_assistant"])).all():
            founder_phone = (f.phone or "").strip()
            if founder_phone:
                send_workflow_whatsapp(
                    founder_phone,
                    "leave",
                    [requester.name, "requested", lr.reason or ""]
                )
    except Exception as e:
        print(f"[WA] leave (request) send failed for {uid}: {e}")

    return jsonify({"success": True, "id": lr.id})


@app.route("/api/leave-requests/<int:lid>/decision", methods=["POST"])
@jwt_required()
def decide_leave_request(lid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Only founder can approve/reject leave"}), 403

    lr = db.session.get(LeaveRequest, lid)
    if not lr:
        return jsonify({"error": "Leave request not found"}), 404

    action = (request.get_json() or {}).get("action")
    if action not in ("approve", "reject"):
        return jsonify({"error": "action must be 'approve' or 'reject'"}), 400

    lr.status     = "approved" if action == "approve" else "rejected"
    lr.decided_at = now_ist().strftime("%b %d, %Y %I:%M %p")
    lr.decided_by = caller.name

    if action == "approve":
        att = Attendance.query.filter_by(userId=lr.userId, date=lr.date).first()
        if att:
            att.status = "leave"
        else:
            att = Attendance(userId=lr.userId, status="leave", date=lr.date)
            db.session.add(att)

    make_notif(
        userId=lr.userId, ntype="leave",
        title="Leave " + ("Approved ✅" if action == "approve" else "Rejected ❌"),
        body=f"Your leave for {lr.date} was {lr.status} by {caller.name}."
    )
    db.session.commit()

    # WhatsApp (best-effort — must never make the leave decision fail)
    try:
        employee = db.session.get(User, lr.userId)
        employee_phone = (employee.phone or "").strip() if employee else ""
        employee_name  = employee.name if employee else ""
        if employee_phone:
            send_workflow_whatsapp(
                employee_phone,
                "leave",
                [employee_name, lr.status, lr.reason or ""]
            )
    except Exception as e:
        print(f"[WA] leave (decision) send failed for leave {lid}: {e}")

    return jsonify({"success": True, "status": lr.status})

@app.route("/api/leave-requests/<int:lid>", methods=["DELETE"])
@jwt_required()
def cancel_leave_request(lid):
    uid = get_jwt_identity()
    caller = db.session.get(User, uid)
    lr = db.session.get(LeaveRequest, lid)
    if not lr:
        return jsonify({"error": "Leave request not found"}), 404
    if lr.userId != uid and not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    if lr.status != "pending":
        return jsonify({"error": "Only pending leave requests can be cancelled"}), 400
    db.session.delete(lr)
    db.session.commit()
    return jsonify({"success": True})

# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────

@app.route("/api/notifications")
@jwt_required()
def get_notifications():
    uid    = get_jwt_identity()
    notifs = Notification.query.filter_by(userId=uid, is_read=False)\
                               .order_by(Notification.id.desc())\
                               .limit(30).all()
    return jsonify([{
        "id": n.id, "type": n.ntype,
        "title": n.title, "body": n.body,
        "createdAt": n.createdAt
    } for n in notifs])


@app.route("/api/notifications/read-all", methods=["POST"])
@jwt_required()
def mark_all_read():
    uid = get_jwt_identity()
    Notification.query.filter_by(userId=uid, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
@jwt_required()
def mark_one_read(nid):
    uid   = get_jwt_identity()
    notif = db.session.get(Notification, nid)
    if notif and notif.userId == uid:
        notif.is_read = True
        db.session.commit()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# PROJECTS
# ─────────────────────────────────────────────

def project_to_dict(p):
    return {
        "id": p.id, "name": p.name, "category": p.category,
        "client_name": p.client_name, "status": p.status,
        "start_date": p.start_date, "team_members": p.team_members,
        "description": p.description, "created_at": p.created_at,
        "progress_percentage": float(p.progress_percentage or 0)
    }


@app.route("/api/projects")
@jwt_required()
def get_projects():
    category = request.args.get("category")
    q = Project.query
    if category:
        q = q.filter_by(category=category)
    return jsonify([project_to_dict(p) for p in q.order_by(Project.id.desc()).all()])


@app.route("/api/projects", methods=["POST"])
@jwt_required()
def create_project():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    cat  = data.get("category", "").strip()
    if cat not in ("Government", "Private", "B2C"):
        return jsonify({"error": "category must be Government, Private or B2C"}), 400

    try:
        pct = float(str(data.get("progress_percentage", 0)).replace("%", "").strip())
    except (TypeError, ValueError):
        pct = 0.0
    if not (0 <= pct <= 100):
        return jsonify({"error": "Progress must be between 0 and 100."}), 400

    p = Project(
        name=data.get("name", "").strip(), category=cat,
        client_name=data.get("client_name", "").strip(),
        status=data.get("status", "Active").strip(),
        start_date=data.get("start_date", "").strip(),
        team_members=data.get("team_members", "").strip(),
        description=data.get("description", "").strip(),
        progress_percentage=pct,
        created_at=now_ist().strftime("%b %d, %Y")
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"success": True, "project": project_to_dict(p)})


@app.route("/api/projects/bulk", methods=["POST"])
@jwt_required()
def bulk_projects():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json() or {}
    rows = data.get("projects", [])
    if not isinstance(rows, list):
        return jsonify({"error": "projects must be a list"}), 400

    try:
        Project.query.delete()
        created = 0
        skipped = 0
        for row in rows:
            cat  = (row.get("category") or "").strip()
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            valid_cats = {"government": "Government", "private": "Private", "b2c": "B2C"}
            cat_norm   = valid_cats.get(cat.lower(), "Government")
            try:
                pct = float(str(row.get("progress_percentage", row.get("progress", 0))).replace("%", "").strip())
                pct = max(0.0, min(100.0, pct))
            except (TypeError, ValueError):
                pct = 0.0
            db.session.add(Project(
                name=name, category=cat_norm,
                client_name=(row.get("client_name") or "").strip(),
                status=(row.get("status") or "Active").strip(),
                start_date=(row.get("start_date") or "").strip(),
                team_members=(row.get("team_members") or "").strip(),
                description=(row.get("description") or "").strip(),
                progress_percentage=pct,
                created_at=now_ist().strftime("%b %d, %Y")
            ))
            created += 1

        db.session.commit()
        return jsonify({"success": True, "created": created, "skipped": skipped})

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/api/projects/upload-xlsx", methods=["POST"])
@jwt_required()
def upload_projects_xlsx():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "Empty file received"}), 400

    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ("xlsx", "xls", "csv"):
        return jsonify({"error": f"Unsupported file type: .{ext}"}), 400

    try:
        import pandas as pd

        file_bytes = f.read()
        if not file_bytes:
            return jsonify({"error": "File is empty"}), 400

        if ext == "csv":
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl" if ext == "xlsx" else "xlrd")

        df.columns = [str(c).strip() for c in df.columns]

        if "Project Name" not in df.columns and "Name" not in df.columns:
            return jsonify({"error": f"Missing 'Project Name' column. Found: {', '.join(df.columns)}"}), 400

        VALID_CATS = {"government": "Government", "private": "Private", "b2c": "B2C"}

        def col(row, *names):
            for n in names:
                v = row.get(n)
                if v and str(v).strip() and str(v).strip().lower() not in ("nan", ""):
                    return str(v).strip()
            return ""

        rows    = []
        skipped = 0
        for _, row in df.iterrows():
            row  = row.to_dict()
            name = col(row, "Project Name", "project name", "Name")
            if not name:
                skipped += 1
                continue
            cat_raw = col(row, "Category", "category") or "Government"
            cat     = VALID_CATS.get(cat_raw.lower(), "Government")

            raw_pct = row.get("Progress", row.get("progress", 0))
            try:
                pct = float(str(raw_pct).replace("%", "").strip())
                if 0 < pct <= 1.0:
                    pct = pct * 100
                pct = max(0.0, min(100.0, pct))
            except (TypeError, ValueError):
                pct = 0.0

            rows.append({
                "name": name, "category": cat,
                "client_name":       col(row, "Client Name",  "client name",  "Client"),
                "status":            col(row, "Status",       "status")       or "Active",
                "start_date":        col(row, "Start Date",   "start date",   "Date"),
                "team_members":      col(row, "Team Members", "team members", "Team"),
                "description":       col(row, "Description",  "description"),
                "progress_percentage": pct,
            })

        if not rows:
            return jsonify({"error": "No valid project rows found"}), 400

        Project.query.delete()
        created = 0
        for row in rows:
            db.session.add(Project(
                name=row["name"], category=row["category"],
                client_name=row["client_name"], status=row["status"],
                start_date=row["start_date"], team_members=row["team_members"],
                description=row["description"],
                progress_percentage=row["progress_percentage"],
                created_at=now_ist().strftime("%b %d, %Y")
            ))
            created += 1

        db.session.commit()
        return jsonify({"success": True, "created": created, "skipped": skipped})

    except ImportError as e:
        return jsonify({"error": f"Server missing library: {e}"}), 500
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"File processing error: {str(e)}"}), 500


@app.route("/api/projects/<int:pid>", methods=["PUT"])
@jwt_required()
def update_project(pid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    p = db.session.get(Project, pid)
    if not p:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    for field in ("name", "category", "client_name", "status", "start_date", "team_members", "description"):
        if field in data:
            setattr(p, field, (data[field] or "").strip())
    if "progress_percentage" in data:
        try:
            pct = float(str(data["progress_percentage"]).replace("%", "").strip())
            pct = max(0.0, min(100.0, pct))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid progress value"}), 400
        p.progress_percentage = pct
    db.session.commit()
    return jsonify({"success": True, "project": project_to_dict(p)})


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@jwt_required()
def delete_project(pid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    p = db.session.get(Project, pid)
    if not p:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(p)
    db.session.commit()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# INTERNS ROSTER
# ─────────────────────────────────────────────

def intern_to_dict(i):
    return {
        "id": i.id, "name": i.name, "domain": i.domain,
        "joining_date": i.joining_date, "status": i.status,
        "created_at": i.created_at
    }


@app.route("/api/interns-roster")
@jwt_required()
def get_interns_roster():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    interns = Intern.query.order_by(Intern.id.desc()).all()
    domain_counts = {}
    for i in interns:
        d = i.domain or "Unknown"
        domain_counts[d] = domain_counts.get(d, 0) + 1
    return jsonify({
        "interns": [intern_to_dict(i) for i in interns],
        "total": len(interns),
        "domain_counts": domain_counts
    })


@app.route("/api/interns-roster/bulk", methods=["POST"])
@jwt_required()
def bulk_interns():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    rows = data.get("interns", [])
    if not isinstance(rows, list):
        return jsonify({"error": "interns must be a list"}), 400

    try:
        Intern.query.delete()
        created = 0
        skipped = 0
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            db.session.add(Intern(
                name=name,
                domain=(row.get("domain") or "").strip(),
                joining_date=(row.get("joining_date") or "").strip(),
                status=(row.get("status") or "Active").strip(),
                created_at=now_ist().strftime("%b %d, %Y")
            ))
            created += 1
        db.session.commit()
        return jsonify({"success": True, "created": created, "skipped": skipped})
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/api/interns-roster", methods=["POST"])
@jwt_required()
def add_intern():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    i = Intern(
        name=name,
        domain=(data.get("domain") or "").strip(),
        joining_date=(data.get("joining_date") or "").strip(),
        status=(data.get("status") or "Active").strip(),
        created_at=now_ist().strftime("%b %d, %Y")
    )
    db.session.add(i)
    db.session.commit()
    return jsonify({"success": True, "intern": intern_to_dict(i)})


@app.route("/api/interns-roster/<int:iid>", methods=["DELETE"])
@jwt_required()
def delete_intern_roster(iid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    i = db.session.get(Intern, iid)
    if not i:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(i)
    db.session.commit()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# DASHBOARD STATS
# ─────────────────────────────────────────────

@app.route("/api/dashboard-stats")
@jwt_required()
def dashboard_stats():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    gov_count     = Project.query.filter_by(category="Government").count()
    private_count = Project.query.filter_by(category="Private").count()
    b2c_count     = Project.query.filter_by(category="B2C").count()

    employees = User.query.filter(User.role.in_(["employee", "founder_assistant"])).all()
    interns   = User.query.filter_by(role="intern").all()

    intern_roster = Intern.query.all()
    domain_counts = {}
    for i in intern_roster:
        d = i.domain or "Unknown"
        domain_counts[d] = domain_counts.get(d, 0) + 1

    today         = today_str()
    today_ratings = Rating.query.filter_by(date=today).all()
    emp_ids       = {u.id for u in employees}
    intern_ids    = {u.id for u in interns}

    emp_ratings    = [r for r in today_ratings if r.userId in emp_ids]
    intern_ratings = [r for r in today_ratings if r.userId in intern_ids]

    def avg(lst): return round(sum(r.score for r in lst) / len(lst), 1) if lst else 0

    return jsonify({
        "projects": {
            "government": gov_count, "private": private_count,
            "b2c": b2c_count, "total": gov_count + private_count + b2c_count
        },
        "users": {
            "total_employees": len(employees), "total_interns": len(interns)
        },
        "interns_roster": {
            "total": len(intern_roster), "domain_counts": domain_counts
        },
        "ratings": {
            "emp_avg": avg(emp_ratings), "intern_avg": avg(intern_ratings),
            "emp_rated_today": len(emp_ratings), "intern_rated_today": len(intern_ratings)
        }
    })


# ─────────────────────────────────────────────
# DASHBOARD GOALS
# ─────────────────────────────────────────────

_GOAL_DEFAULTS = {
    "emp_goal":     15,
    "intern_goal":  40,
    "intship_goal": 200,
    "intship_cur":  0,
    "cert_goal":    150,
    "cert_cur":     0,
}

GOAL_KEYS    = ["emp_goal", "intern_goal", "intship_goal", "intship_cur", "cert_goal", "cert_cur"]
GOAL_DEFAULTS = {"emp_goal": "15", "intern_goal": "40", "intship_goal": "200",
                 "intship_cur": "0", "cert_goal": "150", "cert_cur": "0"}


def _load_goals_dict():
    rows = {r.key: r.value for r in DashboardGoal.query.all()}
    result = {}
    for k in GOAL_KEYS:
        try:
            result[k] = int(rows.get(k, GOAL_DEFAULTS.get(k, "0")))
        except (ValueError, TypeError):
            result[k] = int(GOAL_DEFAULTS.get(k, "0"))
    return result

# ─────────────────────────────────────────────
# TASK TEMPLATES
# ─────────────────────────────────────────────

def template_to_dict(t):
    return {
        "id":          t.id,
        "title":       t.title,
        "description": t.description,
        "priority":    t.priority,
        "frequency":   t.frequency,
        "target_type": t.target_type,
        "target_id":   t.target_id,
        "due_time":    t.due_time,
        "active":      t.active,
        "created_by":  t.created_by,
        "created_at":  t.created_at,
        "last_run":    t.last_run,
    }


@app.route("/api/task-templates", methods=["GET"])
@jwt_required()
def get_task_templates():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    templates = TaskTemplate.query.order_by(TaskTemplate.id.desc()).all()
    return jsonify([template_to_dict(t) for t in templates])


@app.route("/api/task-templates", methods=["POST"])
@jwt_required()
def create_task_template():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    title       = (data.get("title") or "").strip()
    frequency   = (data.get("frequency") or "").strip().lower()
    target_type = (data.get("target_type") or "").strip().lower()
    target_id   = (data.get("target_id") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    if frequency not in ("daily", "weekly", "monthly"):
        return jsonify({"error": "Frequency must be daily, weekly, or monthly"}), 400
    if target_type not in ("team", "user", "all"):
        return jsonify({"error": "target_type must be 'team', 'user', or 'all'"}), 400
    if target_type in ("team", "user") and not target_id:
        return jsonify({"error": "target_id is required"}), 400
    tpl = TaskTemplate(
        title       = title,
        description = (data.get("description") or "").strip(),
        priority    = data.get("priority", "medium"),
        frequency   = frequency,
        target_type = target_type,
        target_id   = (
            data.get("target_id")
            if data.get("target_type") != "all"
            else None
        ),
        due_time    = (data.get("due_time") or "").strip() or None,
        active      = bool(data.get("active", True)),
        created_by  = caller.id,
        created_at  = now_ist().strftime("%b %d, %Y %I:%M %p"),
        last_run    = None,
    )
    db.session.add(tpl)
    db.session.commit()
    return jsonify({"success": True, "template": template_to_dict(tpl)})


@app.route("/api/task-templates/<int:tid>", methods=["PUT"])
@jwt_required()
def update_task_template(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    tpl = db.session.get(TaskTemplate, tid)
    if not tpl:
        return jsonify({"error": "Template not found"}), 404
    data = request.get_json() or {}
    if "title"       in data: tpl.title       = (data["title"] or "").strip() or tpl.title
    if "description" in data: tpl.description = (data["description"] or "").strip()
    if "priority"    in data: tpl.priority    = data["priority"] or tpl.priority
    if "due_time"    in data: tpl.due_time    = (data["due_time"] or "").strip() or None
    if "target_type" in data:
        tt = (data["target_type"] or "").strip().lower()
        if tt in ("team", "user", "all"):
            tpl.target_type = tt
    if "target_id"   in data: tpl.target_id   = (data["target_id"] or "").strip() or tpl.target_id
    if tpl.target_type == "all":
        tpl.target_id = None
    if "frequency"   in data:
        freq = (data["frequency"] or "").strip().lower()
        if freq in ("daily", "weekly", "monthly"): tpl.frequency = freq
    if "active"      in data: tpl.active      = bool(data["active"])
    db.session.commit()
    return jsonify({"success": True, "template": template_to_dict(tpl)})


@app.route("/api/task-templates/<int:tid>", methods=["DELETE"])
@jwt_required()
def delete_task_template(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    tpl = db.session.get(TaskTemplate, tid)
    if not tpl:
        return jsonify({"error": "Not found"}), 404
    Task.query.filter(
        Task.template_id == str(tpl.id),
        Task.is_auto == True,
    ).delete(synchronize_session=False)
    db.session.delete(tpl)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/task-templates/<int:tid>/toggle", methods=["PATCH"])
@jwt_required()
def toggle_task_template(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    tpl = db.session.get(TaskTemplate, tid)
    if not tpl:
        return jsonify({"error": "Not found"}), 404
    tpl.active = not tpl.active
    db.session.commit()
    return jsonify({"success": True, "active": tpl.active})

def resolve_template_target_users(tpl):
    """
    Resolve the list of User objects a TaskTemplate should dispatch to,
    based on its target_type ('team' | 'user' | 'all').
    """
    if tpl.target_type == "user":
        return [u for u in User.query.all() if u.id == tpl.target_id]
    elif tpl.target_type == "all":
        # All active users across every role (Founder Assistant, Employees,
        # Interns, Trainers, and any future roles), excluding the Founder.
        return User.query.filter(
            User.active == True,
            User.role.notin_(["founder"])
        ).all()
    else:
        # team match OR founder_assistant role for that pseudo-team
        target_users = User.query.filter(
            User.team == tpl.target_id,
            User.role.notin_(["founder"])
        ).all()
        if not target_users and tpl.target_id == "founder_assistant":
            target_users = User.query.filter_by(role="founder_assistant").all()
        return target_users


@app.route("/api/task-templates/<int:tid>/dispatch", methods=["POST"])
@jwt_required()
def dispatch_single_template(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    tpl = db.session.get(TaskTemplate, tid)
    if not tpl:
        return jsonify({"error": "Template not found"}), 404

    today     = today_str()
    now_iso_v = now_ist().strftime("%Y-%m-%dT%H:%M:%S")

    target_users = resolve_template_target_users(tpl)

    created_count = 0
    for user in target_users:
        dedup_key = f"auto_{tpl.id}_{user.id}_{today}"
        if Task.query.filter_by(id=dedup_key).first():
            continue

        # Remove yesterday's (or any older) unfinished reminder from this
        # template for this user, so only today's reminder ever exists.
        Task.query.filter(
            Task.template_id == str(tpl.id),
            Task.assignedTo == user.id,
            Task.is_auto == True,
            Task.status != "completed",
        ).delete(synchronize_session=False)

        task = Task(
            id         = dedup_key,
            title      = tpl.title,
            desc       = tpl.description or "",
            assignedTo = user.id,
            assignedBy = caller.id,
            status     = "pending",
            priority   = tpl.priority or "medium",
            due        = today,
            msg        = f"[Auto Task — {tpl.frequency.capitalize()}]",
            createdAt  = now_iso_v,
            work_completion_percentage = 0.0,
            template_id = tpl.id,
            generated_date = today,
            is_auto = True,
        )
        db.session.add(task)
        make_notif(
            userId = user.id,
            ntype  = "task",
            title  = "📋 Auto Task Assigned",
            body   = f"Auto task: {tpl.title}"
        )
        created_count += 1

    if created_count:
        tpl.last_run = today

    db.session.commit()
    return jsonify({"success": True, "created": created_count})


def dispatch_task_templates():
    """
    Called by the scheduler. Iterates all active templates and creates tasks
    for eligible users based on frequency + dedup logic.
    """
    with app.app_context():
        today     = today_str()
        now_iso_v = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
        templates = TaskTemplate.query.filter_by(active=True).all()

        for tpl in templates:
            # ── Frequency gate ──────────────────────────────────────
            if tpl.last_run == today:
                continue  # already dispatched today

            now_date = today_ist()
            if tpl.frequency == "weekly" and tpl.last_run:
                try:
                    lr = datetime.datetime.strptime(tpl.last_run, "%Y-%m-%d").date()
                    if (now_date - lr).days < 7:
                        continue
                except ValueError:
                    pass
            elif tpl.frequency == "monthly" and tpl.last_run:
                try:
                    lr = datetime.datetime.strptime(tpl.last_run, "%Y-%m-%d").date()
                    if (now_date - lr).days < 28:
                        continue
                except ValueError:
                    pass

            # ── Resolve target users ────────────────────────────────
            target_users = resolve_template_target_users(tpl)

            created_count = 0
            for user in target_users:
                # ── Dedup: same template + same user + today ────────
                dedup_key = f"auto_{tpl.id}_{user.id}_{today}"
                exists = Task.query.filter_by(id=dedup_key).first()
                if exists:
                    continue

                # Remove yesterday's (or any older) unfinished reminder from
                # this template for this user, so only today's reminder exists.
                Task.query.filter(
                    Task.template_id == str(tpl.id),
                    Task.assignedTo == user.id,
                    Task.is_auto == True,
                    Task.status != "completed",
                ).delete(synchronize_session=False)

                task = Task(
                    id         = dedup_key,
                    title      = tpl.title,
                    desc       = tpl.description or "",
                    assignedTo = user.id,
                    assignedBy = tpl.created_by or "system",
                    status     = "pending",
                    priority   = tpl.priority or "medium",
                    due        = today,
                    msg        = f"[Auto Task — {tpl.frequency.capitalize()}]",
                    createdAt  = now_iso_v,
                    work_completion_percentage = 0.0,
                    template_id = tpl.id,
                    generated_date = today,
                    is_auto = True,
                )
                db.session.add(task)
                make_notif(
                    userId = user.id,
                    ntype  = "task",
                    title  = "📋 Auto Task Assigned",
                    body   = f"Auto task: {tpl.title}"
                )
                created_count += 1

            if created_count:
                tpl.last_run = today
                print(f"[AUTO TASK] Template '{tpl.title}' → {created_count} task(s) created for {today}")

        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"[AUTO TASK] Commit error: {exc}")

def generate_daily_tasks():
    """
    Runs every day at 09:00 AM IST.
    Loops through every active TaskTemplate with frequency == "daily".
    For each one not yet processed today (last_run != today), creates a
    Task per resolved target user with status/fields taken exactly from
    the template, plus a "New Daily Reminder" notification, then marks
    the template as processed for today (last_run = today) and commits.
    Duplicate-safe: task id is deterministic per template+user+day.
    """
    with app.app_context():
        today     = today_str()
        now_iso_v = now_ist().strftime("%Y-%m-%dT%H:%M:%S")

        templates = TaskTemplate.query.filter_by(active=True, frequency="daily").all()

        for tpl in templates:
            if tpl.last_run == today:
                continue  # already generated today — prevents duplicate generation

            target_users = resolve_template_target_users(tpl)

            for user in target_users:
                dedup_key = f"daily9_{tpl.id}_{user.id}_{today}"
                if Task.query.filter_by(id=dedup_key).first():
                    continue

                # Remove yesterday's (or any older) unfinished reminder from
                # this template for this user, so only today's reminder exists.
                Task.query.filter(
                    Task.template_id == str(tpl.id),
                    Task.assignedTo == user.id,
                    Task.is_auto == True,
                    Task.status != "completed",
                ).delete(synchronize_session=False)

                task = Task(
                    id         = dedup_key,
                    title      = tpl.title,
                    desc       = tpl.description or "",
                    assignedTo = user.id,
                    assignedBy = tpl.created_by or "system",
                    status     = "pending",
                    priority   = tpl.priority or "medium",
                    due        = today,
                    msg        = "[Daily Task Template — 9AM]",
                    createdAt  = now_iso_v,
                    work_completion_percentage = 0.0,
                    template_id = tpl.id,
                    generated_date = today,
                    is_auto = True,
                )
                db.session.add(task)

                make_notif(
                    userId = user.id,
                    ntype  = "task",
                    title  = "New Daily Reminder",
                    body   = f"Today's reminder:\n{tpl.title}"
                )

            tpl.last_run = today

        try:
            db.session.commit()
            print(f"[DAILY TASK 9AM] Processed {len(templates)} daily template(s) for {today}")
        except Exception as exc:
            db.session.rollback()
            print(f"[DAILY TASK 9AM] Commit error: {exc}")

@app.route("/api/visionboard", methods=["GET"])
@jwt_required()
def get_visionboard_items():
    rows = VisionBoardItem.query.filter_by(active=True).order_by(VisionBoardItem.display_order.asc(), VisionBoardItem.id.asc()).all()
    return jsonify([visionboard_item_to_dict(v) for v in rows])


@app.route("/api/visionboard", methods=["POST"])
@jwt_required()
def create_visionboard_item():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can add Vision Board data"}), 403
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Goal Name is required"}), 400
    try:
        current_value = float(data.get("current_value", 0))
        target_value  = float(data.get("target_value", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Current/Target value must be numbers"}), 400
    try:
        display_order = int(data.get("display_order", 0))
    except (TypeError, ValueError):
        display_order = 0

    item = VisionBoardItem(
        title=title,
        current_value=current_value,
        target_value=target_value,
        unit=(data.get("unit") or "").strip(),
        icon=(data.get("icon") or "").strip() or None,
        display_order=display_order,
        created_by=caller.name,
        updated_by=caller.name,
        created_at=now_ist().strftime("%b %d, %Y %I:%M %p"),
        updated_at=now_ist().strftime("%b %d, %Y %I:%M %p"),
        active=True
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({"success": True, "item": visionboard_item_to_dict(item)})


@app.route("/api/visionboard/<int:item_id>", methods=["PUT"])
@jwt_required()
def update_visionboard_item(item_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can edit Vision Board data"}), 403
    item = db.session.get(VisionBoardItem, item_id)
    if not item or not item.active:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json() or {}
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "Goal Name cannot be empty"}), 400
        item.title = title
    if "current_value" in data:
        try:
            item.current_value = float(data["current_value"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid current_value"}), 400
    if "target_value" in data:
        try:
            item.target_value = float(data["target_value"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid target_value"}), 400
    if "unit" in data:
        item.unit = (data["unit"] or "").strip()
    if "icon" in data:
        item.icon = (data["icon"] or "").strip() or None
    if "display_order" in data:
        try:
            item.display_order = int(data["display_order"])
        except (TypeError, ValueError):
            pass

    item.updated_by = caller.name
    item.updated_at = now_ist().strftime("%b %d, %Y %I:%M %p")
    db.session.commit()
    return jsonify({"success": True, "item": visionboard_item_to_dict(item)})


@app.route("/api/visionboard/<int:item_id>", methods=["DELETE"])
@jwt_required()
def delete_visionboard_item(item_id):
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Only founder can delete Vision Board data"}), 403
    item = db.session.get(VisionBoardItem, item_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    item.active = False
    item.updated_by = caller.name
    item.updated_at = now_ist().strftime("%b %d, %Y %I:%M %p")
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/dashboard/goals", methods=["GET"])
@jwt_required()
def get_dashboard_goals():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(_load_goals_dict())


@app.route("/api/dashboard/goals", methods=["PUT"])
@jwt_required()
def put_dashboard_goals():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    for k in GOAL_KEYS:
        if k in data:
            try:
                val = str(int(data[k]))
            except (ValueError, TypeError):
                continue
            row = db.session.get(DashboardGoal, k)
            if row:
                row.value = val
            else:
                db.session.add(DashboardGoal(key=k, value=val))
    db.session.commit()
    return jsonify({"success": True, "goals": _load_goals_dict()})


# ─────────────────────────────────────────────
# UPCOMING BIRTHDAYS
# ─────────────────────────────────────────────

@app.route("/api/upcoming-birthdays")
@jwt_required()
def get_upcoming_birthdays():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    today   = today_ist()
    results = []

    candidates = User.query.filter(
        User.role != "founder",
        User.date_of_birth != None,
        User.date_of_birth != ""
    ).all()

    for emp in candidates:
        try:
            dob = datetime.datetime.strptime(emp.date_of_birth, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        for delta in range(0, 8):
            check_date = today + datetime.timedelta(days=delta)
            try:
                this_year_bday = dob.replace(year=check_date.year)
            except ValueError:
                continue
            if this_year_bday == check_date:
                results.append({
                    "id":         emp.id,
                    "name":       emp.name or emp.id,
                    "role":       emp.role,
                    "department": emp.department or "",
                    "days_away":  delta,
                    "birthday":   this_year_bday.strftime("%B %d"),
                })
                break

    results.sort(key=lambda x: x["days_away"])
    return jsonify({"alerts": results})


# ─────────────────────────────────────────────
# PROJECT TASKS & PROGRESS
# ─────────────────────────────────────────────

@app.route("/api/project-tasks/upload", methods=["POST"])
@jwt_required()
def upload_project_tasks():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv"):
        return jsonify({"error": "Unsupported file type. Upload .xlsx, .xls, or .csv"}), 400

    try:
        import pandas as pd

        file_bytes = file.read()
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

        df.columns = [str(c).strip() for c in df.columns]

        cols_lower = [c.lower() for c in df.columns]
        has_project = any("project" in c for c in cols_lower)
        has_task    = any("task" in c for c in cols_lower)
        if not has_project or not has_task:
            return jsonify({
                "error": (
                    "Missing required columns. Expected: 'Project Name', 'Task Name'. "
                    f"Found: {', '.join(df.columns)}"
                )
            }), 400

        def col(row, *names):
            for n in names:
                v = row.get(n)
                if v and str(v).strip() and str(v).strip().lower() not in ("nan", "none", ""):
                    return str(v).strip()
            return ""

        VALID_STATUSES = {
            "pending":     "Pending",
            "in progress": "In Progress",
            "completed":   "Completed"
        }

        new_rows = []
        skipped  = 0

        for _, row in df.iterrows():
            row_dict     = row.to_dict()
            project_name = col(row_dict, "Project Name", "project name", "Project", "project")
            task_name    = col(row_dict, "Task Name",    "task name",    "Task",    "task")

            if not project_name or not task_name:
                skipped += 1
                continue

            status_raw = col(row_dict, "Status", "status", "STATUS") or "Pending"
            status     = VALID_STATUSES.get(status_raw.lower(), "Pending")

            new_rows.append(ProjectTask(
                project_name = project_name,
                task_name    = task_name,
                assigned_to  = col(row_dict, "Assigned To", "assigned to", "AssignedTo", "Assignee"),
                status       = status,
                due_date     = col(row_dict, "Due Date", "due date", "DueDate", "Due"),
                created_at   = now_ist().strftime("%b %d, %Y %I:%M %p")
            ))

        if not new_rows:
            return jsonify({"error": "No valid task rows found."}), 400

        ProjectTask.query.delete()
        db.session.bulk_save_objects(new_rows)
        db.session.commit()

        return jsonify({"success": True, "created": len(new_rows), "skipped": skipped})

    except ImportError as e:
        return jsonify({"error": f"Server missing library: {e}"}), 500
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"File processing error: {str(e)}"}), 500


@app.route("/api/project-progress")
@jwt_required()
def get_project_progress():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    from sqlalchemy import func, case

    task_rows = (
        db.session.query(
            ProjectTask.project_name,
            func.count(ProjectTask.id).label("total_tasks"),
            func.sum(case((ProjectTask.status == "Completed",   1), else_=0)).label("completed_tasks"),
            func.sum(case((ProjectTask.status == "In Progress", 1), else_=0)).label("in_progress_tasks"),
            func.sum(case((ProjectTask.status == "Pending",     1), else_=0)).label("pending_tasks"),
        )
        .group_by(ProjectTask.project_name)
        .all()
    )
    task_map = {r.project_name: r for r in task_rows}

    projects = Project.query.order_by(Project.name).all()
    result = []
    for p in projects:
        tr  = task_map.get(p.name)
        pct = float(p.progress_percentage or 0)
        result.append({
            "project_name":        p.name,
            "progress_percentage": pct,
            "total_tasks":         int(tr.total_tasks      or 0) if tr else 0,
            "completed_tasks":     int(tr.completed_tasks  or 0) if tr else 0,
            "in_progress_tasks":   int(tr.in_progress_tasks or 0) if tr else 0,
            "pending_tasks":       int(tr.pending_tasks     or 0) if tr else 0,
        })

    return jsonify(result)


# ─────────────────────────────────────────────
# DAILY MOTIVATION QUOTES
# ─────────────────────────────────────────────

MOTIVATION_QUOTES = [
    {"text": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
    {"text": "It always seems impossible until it's done.", "author": "Nelson Mandela"},
    {"text": "Don't watch the clock; do what it does. Keep going.", "author": "Sam Levenson"},
    {"text": "The future depends on what you do today.", "author": "Mahatma Gandhi"},
    {"text": "Success is not final, failure is not fatal: it is the courage to continue that counts.", "author": "Winston Churchill"},
    {"text": "Believe you can and you're halfway there.", "author": "Theodore Roosevelt"},
    {"text": "You are never too old to set another goal or to dream a new dream.", "author": "C.S. Lewis"},
    {"text": "The harder you work for something, the greater you'll feel when you achieve it.", "author": "Unknown"},
    {"text": "Dream big and dare to fail.", "author": "Norman Vaughan"},
    {"text": "Work hard, be kind, and amazing things will happen.", "author": "Conan O'Brien"},
    {"text": "Push yourself, because no one else is going to do it for you.", "author": "Unknown"},
    {"text": "Great things never come from comfort zones.", "author": "Unknown"},
    {"text": "Do something today that your future self will thank you for.", "author": "Sean Patrick Flanery"},
    {"text": "Little things make big days.", "author": "Unknown"},
    {"text": "It's going to be hard, but hard does not mean impossible.", "author": "Unknown"},
    {"text": "Don't stop when you're tired. Stop when you're done.", "author": "Unknown"},
    {"text": "Wake up with determination. Go to bed with satisfaction.", "author": "Unknown"},
    {"text": "Do what you can with all you have, wherever you are.", "author": "Theodore Roosevelt"},
    {"text": "Success usually comes to those who are too busy to be looking for it.", "author": "Henry David Thoreau"},
    {"text": "Opportunities don't happen. You create them.", "author": "Chris Grosser"},
    {"text": "Don't be afraid to give up the good to go for the great.", "author": "John D. Rockefeller"},
    {"text": "I find that the harder I work, the more luck I seem to have.", "author": "Thomas Jefferson"},
    {"text": "There are no secrets to success. It is the result of preparation, hard work, and learning from failure.", "author": "Colin Powell"},
    {"text": "Success is not the key to happiness. Happiness is the key to success.", "author": "Albert Schweitzer"},
    {"text": "The only place where success comes before work is in the dictionary.", "author": "Vidal Sassoon"},
    {"text": "In order to succeed, we must first believe that we can.", "author": "Nikos Kazantzakis"},
    {"text": "If you can dream it, you can do it.", "author": "Walt Disney"},
    {"text": "The best time to plant a tree was 20 years ago. The second best time is now.", "author": "Chinese Proverb"},
    {"text": "Spread love everywhere you go.", "author": "Mother Teresa"},
    {"text": "When you reach the end of your rope, tie a knot in it and hang on.", "author": "Franklin D. Roosevelt"},
    {"text": "Don't judge each day by the harvest you reap but by the seeds that you plant.", "author": "Robert Louis Stevenson"},
    {"text": "It is during our darkest moments that we must focus to see the light.", "author": "Aristotle"},
    {"text": "Do not go where the path may lead, go instead where there is no path and leave a trail.", "author": "Ralph Waldo Emerson"},
    {"text": "The greatest glory in living lies not in never falling, but in rising every time we fall.", "author": "Nelson Mandela"},
    {"text": "Life is either a daring adventure or nothing at all.", "author": "Helen Keller"},
    {"text": "Many of life's failures are people who did not realize how close they were to success when they gave up.", "author": "Thomas A. Edison"},
    {"text": "The only way to do great work is to love what you do.", "author": "Steve Jobs"},
    {"text": "If you can't explain it simply, you don't understand it well enough.", "author": "Albert Einstein"},
    {"text": "Definiteness of purpose is the starting point of all achievement.", "author": "W. Clement Stone"},
    {"text": "If you're going through hell, keep going.", "author": "Winston Churchill"},
    {"text": "Always do your best. What you plant now, you will harvest later.", "author": "Og Mandino"},
    {"text": "The mind is everything. What you think you become.", "author": "Buddha"},
    {"text": "Start where you are. Use what you have. Do what you can.", "author": "Arthur Ashe"},
    {"text": "When the going gets tough, the tough get going.", "author": "Joe Kennedy"},
    {"text": "It does not matter how slowly you go as long as you do not stop.", "author": "Confucius"},
    {"text": "If you want to lift yourself up, lift up someone else.", "author": "Booker T. Washington"},
    {"text": "We must accept finite disappointment, but never lose infinite hope.", "author": "Martin Luther King Jr."},
    {"text": "Every day is a new beginning. Take a deep breath and start again.", "author": "Unknown"},
    {"text": "Motivation is what gets you started. Habit is what keeps you going.", "author": "Jim Ryun"},
    {"text": "The difference between ordinary and extraordinary is that little extra.", "author": "Jimmy Johnson"},
    {"text": "With the new day comes new strength and new thoughts.", "author": "Eleanor Roosevelt"},
    {"text": "Energy and persistence conquer all things.", "author": "Benjamin Franklin"},
    {"text": "Success comes from consistency, focus and never giving up.", "author": "Unknown"},
    {"text": "Your limitation—it's only your imagination.", "author": "Unknown"},
    {"text": "Sometimes later becomes never. Do it now.", "author": "Unknown"},
    {"text": "Dream it. Wish it. Do it.", "author": "Unknown"},
    {"text": "When nothing goes right, go left.", "author": "Unknown"},
    {"text": "You miss 100% of the shots you don't take.", "author": "Wayne Gretzky"},
    {"text": "Whether you think you can or think you can't, you're right.", "author": "Henry Ford"},
    {"text": "I have not failed. I've just found 10,000 ways that won't work.", "author": "Thomas A. Edison"},
    {"text": "A person who never made a mistake never tried anything new.", "author": "Albert Einstein"},
    {"text": "The way to get started is to quit talking and begin doing.", "author": "Walt Disney"},
    {"text": "Reading is to the mind, as exercise is to the body.", "author": "Brian Tracy"},
    {"text": "The most common way people give up their power is by thinking they don't have any.", "author": "Alice Walker"},
    {"text": "Once you choose hope, anything's possible.", "author": "Christopher Reeve"},
]


def get_or_create_daily_quote():
    today    = today_str()
    existing = DailyQuote.query.filter_by(date=today).first()
    if existing:
        return existing

    yesterday = (now_ist() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_q = DailyQuote.query.filter_by(date=yesterday).first()
    avoid_text  = yesterday_q.quote_text if yesterday_q else None

    candidates = [q for q in MOTIVATION_QUOTES if q["text"] != avoid_text]
    if not candidates:
        candidates = MOTIVATION_QUOTES

    chosen = random.choice(candidates)
    dq = DailyQuote(
        date       = today,
        quote_text = chosen["text"],
        author     = chosen["author"],
        sent_at    = None
    )
    db.session.add(dq)
    db.session.commit()
    return dq


def send_daily_quote_email(recipient_email, quote_text, author):
    brevo_key  = os.getenv("BREVO_API_KEY", "").strip()
    smtp_email = os.getenv("SMTP_EMAIL", "").strip()
    smtp_pass  = os.getenv("SMTP_PASSWORD", "").strip()

    today_display = now_ist().strftime("%d %B %Y")
    subject  = f"🌞 Your Daily Motivation | PGI TaskFlow — {today_display}"
    plain    = (
        f"Good Morning,\n\nToday's motivation:\n\n"
        f"\"{quote_text}\"\n— {author}\n\n"
        f"Wishing you a productive day.\n\nRegards,\nPGI TaskFlow"
    )
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:28px;'
        f'background:#fffdf5;border:1px solid #e5d98b;border-radius:10px">'
        f'<h2 style="color:#15803D;margin-top:0">🌞 Good Morning!</h2>'
        f'<div style="background:#f0fff4;border-left:4px solid #22C55E;padding:16px 20px;'
        f'border-radius:6px;margin:18px 0">'
        f'<p style="font-size:17px;font-style:italic;color:#1a3a2a;margin:0 0 8px">"{quote_text}"</p>'
        f'<p style="font-size:13px;color:#5a7a6a;margin:0">— {author}</p></div>'
        f'<p style="color:#555;font-size:14px;line-height:1.7">Wishing you a productive day.</p>'
        f'<hr style="border:none;border-top:1px solid #e5d98b;margin:20px 0">'
        f'<p style="color:#888;font-size:12px">PGI TaskFlow &mdash; Plant Green Inertia</p></div>'
    )

    if brevo_key:
        payload = json.dumps({
            "sender": {"name": "PGI TaskFlow", "email": smtp_email or "pgiworkflow@gmail.com"},
            "to": [{"email": recipient_email}],
            "subject": subject,
            "textContent": plain,
            "htmlContent": html,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": brevo_key, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return True
        except Exception as e:
            print(f"[QUOTE EMAIL/Brevo] Failed for {recipient_email}: {e}")
            return False

    if not smtp_email or not smtp_pass:
        return False
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(smtp_email, smtp_pass)
            mime = MIMEMultipart("alternative")
            mime["Subject"] = subject
            mime["From"]    = f"PGI TaskFlow <{smtp_email}>"
            mime["To"]      = recipient_email
            mime.attach(MIMEText(plain, "plain", "utf-8"))
            mime.attach(MIMEText(html,  "html",  "utf-8"))
            server.sendmail(smtp_email, recipient_email, mime.as_string())
        return True
    except Exception as e:
        print(f"[QUOTE EMAIL/SMTP] Failed for {recipient_email}: {e}")
        return False


def normalize_phone_e164(phone):
    """
    Normalize a raw phone string to E.164 format (+<countrycode><number>).
    Indian-number aware: a bare 10-digit number is assumed to be an Indian
    mobile number and gets +91 prefixed; numbers that already carry a
    country code (with or without '+') are left as-is (never double-prefixed).
    Returns (normalized_number_or_None, error_reason_or_None).
    """
    if not phone or not phone.strip():
        return None, "no_phone"

    # Strip spaces, hyphens, dots and brackets.
    cleaned = re.sub(r"[\s\-\.\(\)]", "", phone.strip())

    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if cleaned.startswith("+"):
        digits = cleaned[1:]
    else:
        digits = cleaned.lstrip("+")
        if re.fullmatch(r"\d{10}", digits):
            # Bare 10-digit number -> assume Indian mobile, prefix +91.
            digits = "91" + digits
        # else: already has a country code (e.g. "919876543210") — leave as-is.

    # Guard: never end up with a duplicated 91 country code (+9191XXXXXXXXXX).
    if digits.startswith("9191") and len(digits) == 14:
        digits = digits[2:]

    cleaned = "+" + digits

    # E.164: '+' followed by 8–15 digits, first digit 1-9
    if not re.fullmatch(r"\+[1-9]\d{7,14}", cleaned):
        return None, "invalid_format"

    return cleaned, None


def build_daily_quote_message(quote_text):
    """Exact WhatsApp message template — the quote is inserted verbatim, untouched."""
    return (
        "🌞 Good Morning!\n\n"
        "✨ Daily Motivation\n\n"
        f"\"{quote_text}\"\n\n"
        "— Plant Green Inertia 🌿\n\n"
        "Have an amazing and productive day!"
    )


# ─────────────────────────────────────────────
# WHATSAPP TEMPLATE CONFIG (Meta approved templates — all workflow WhatsApp
# messages go out as "type": "template", never free-text "type": "text")
# ─────────────────────────────────────────────

# Central map: internal template_key -> exact Meta-approved template name.
# Do not duplicate these names anywhere else in the file.
WHATSAPP_TEMPLATES = {
    "task_assignment": "task_assignment",
    "task_update":      "task_update",
    "attendance":        "attendance_notification",
    "leave":              "leave_notification",
    "meeting":             "meeting_remainder",
    "birthday":             "birthday_wish",
    "daily_quote":           "daily_quote",
    "announcement":           "announcement"
}

# Documents the ordered {{1}}, {{2}}, ... body parameters each Meta template
# expects, so every call site builds its `parameters` list the same way.
# CONFIRMED = given explicitly in the change request. TODO = the exact Meta
# template body text/order was not provided, so the list below is this
# integration's best-guess ordering — verify against the live template in
# Meta Business Manager before relying on it in production.
WHATSAPP_TEMPLATE_PARAMS = {
    # CONFIRMED: {{1}} employee first name, {{2}} task name, {{3}} due date
    "task_assignment":        ["employee_first_name", "task_name", "due_date"],
    # CONFIRMED: {{1}} employee name, {{2}} task/status update, {{3}} remarks
    "task_update":             ["employee_name", "task_status_or_update", "remarks"],
    # CONFIRMED: {{1}} employee name, {{2}} attendance status, {{3}} recorded time
    "attendance_notification":  ["employee_name", "attendance_status", "recorded_time"],
    # CONFIRMED: {{1}} employee name, {{2}} Approved/Rejected/etc, {{3}} leave reason
    "leave_notification":        ["employee_name", "leave_status", "leave_reason"],
    # CONFIRMED: {{1}} employee name, {{2}} meeting topic, {{3}} meeting time
    # TODO: no existing meeting-reminder backend found in app.py — not wired to any route
    "meeting_remainder":          ["employee_name", "meeting_topic", "meeting_time"],
    # CONFIRMED: {{1}} employee name only
    "birthday_wish":                ["employee_name"],
    # CONFIRMED: {{1}} employee name only
    "daily_quote": ["employee_name", "daily_quote_text"],
    # CONFIRMED: {{1}} employee name only — do not pass announcement text as {{2}}
    # TODO: no existing announcement/broadcast backend found in app.py — not wired to any route
    "announcement":                     ["employee_name"]
}


def send_workflow_whatsapp(phone, template_key, parameters=None):
    """Single entry point every workflow (task/attendance/leave/etc.) uses to
    send a WhatsApp template message. Never raises — always returns a dict,
    so callers can fire-and-forget without risking the original operation."""
    template_name = WHATSAPP_TEMPLATES.get(template_key)
    if not template_name:
        return {"success": False, "reason": "unknown_template"}

    return send_whatsapp_template(
        phone,
        template_name,
        parameters or []
    )


def send_whatsapp_template(phone, template_name, parameters=None, language_code="en"):
    """
    Send an APPROVED Meta WhatsApp Cloud API template message (never free text).
    Reads the existing WHATSAPP_API_TOKEN / WHATSAPP_PHONE_NUMBER_ID /
    WHATSAPP_API_VERSION env vars — nothing is hard-coded.
    Returns {"success": bool, "reason": str, "response": str|None}.
    """
    phone_clean, err = normalize_phone_e164(phone)
    if err:
        return {"success": False, "reason": err, "response": None}

    wa_token        = os.getenv("WHATSAPP_API_TOKEN", "").strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    wa_url          = os.getenv("WHATSAPP_API_URL", "").strip()

    if not wa_url:
        if not phone_number_id:
            return {"success": False, "reason": "not_configured", "response": None}
        api_version = os.getenv("WHATSAPP_API_VERSION", "v18.0").strip() or "v18.0"
        wa_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    if not wa_token:
        return {"success": False, "reason": "not_configured", "response": None}

    template_obj = {
        "name": template_name,
        "language": {"code": language_code}
    }
    # Only add the body component when parameters exist.
    if parameters:
        template_obj["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in parameters]
        }]

    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to":       phone_clean,
        "type":     "template",
        "template": template_obj
    }).encode("utf-8")

    req = urllib.request.Request(
        wa_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {wa_token}",
            "Content-Type":  "application/json",
        },
        method="POST"
    )
    print(f"[WHATSAPP TEMPLATE] Template: {template_name}")
    print(f"[WHATSAPP TEMPLATE] Recipient: {phone_clean}")
    print(f"[WHATSAPP TEMPLATE] Variables: {parameters or []}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode(errors="replace")
            print(f"[WHATSAPP TEMPLATE] HTTP status: {resp.status}")
            print(f"[WHATSAPP TEMPLATE] Meta response: {resp_body[:500]}")
        return {"success": True, "reason": "sent", "response": resp_body[:500]}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        reason = f"http_{e.code}"
        # Meta error 131047 = "re-engagement message" / outside the 24h
        # customer-service window. Approved templates are the correct fix
        # (never retry, never fall back to a free-text message).
        try:
            err_json = json.loads(err_body)
            meta_code = err_json.get("error", {}).get("code")
            if meta_code == 131047:
                reason = "outside_24h_window"
        except (ValueError, TypeError):
            pass
        print(f"[WHATSAPP TEMPLATE] HTTP status: {e.code}")
        print(f"[WHATSAPP TEMPLATE] Meta response: {err_body[:500]}")
        return {"success": False, "reason": reason, "response": err_body[:500]}
    except Exception as e:
        print(f"[WHATSAPP TEMPLATE] Error for {phone_clean} (template={template_name}): {e}")
        return {"success": False, "reason": str(e), "response": None}


def _send_whatsapp_via_twilio(phone_clean, message):
    """
    Send via Twilio's WhatsApp Business API, using the Founder's approved
    WhatsApp-enabled Twilio sender number (TWILIO_WHATSAPP_FROM).
    """
    sid         = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token       = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

    if not sid or not token or not from_number:
        return False, "not_configured", None

    if not from_number.startswith("whatsapp:"):
        from_number = "whatsapp:" + from_number

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    body = urllib.parse.urlencode({
        "From": from_number,
        "To":   f"whatsapp:{phone_clean}",
        "Body": message
    }).encode("utf-8")

    creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode(errors="replace")
        return True, "sent", resp_body[:500]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(f"[QUOTE WA/twilio] HTTPError {e.code} for {phone_clean}: {err_body}")
        return False, f"http_{e.code}", err_body[:500]
    except Exception as e:
        print(f"[QUOTE WA/twilio] Error for {phone_clean}: {e}")
        return False, str(e), None


def _send_whatsapp_attempt(phone_clean, message, employee_name=None, quote_text=None):
    """Single attempt, routed to the configured provider. No retries here.
    Meta always sends the approved `daily_quote` template (employee_name is
    the single {{1}} body parameter); Twilio is unaffected and keeps sending
    the pre-built free-text `message` as before."""
    provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
    if provider == "twilio":
        return _send_whatsapp_via_twilio(phone_clean, message)
    elif provider == "meta":
        result = send_workflow_whatsapp(phone_clean, "daily_quote", [employee_name])
        return result.get("success", False), result.get("reason", "unknown_error"), result.get("response")
    else:
        return False, "unknown_provider", None


# Failure reasons that will never succeed on retry — no point burning attempts on them.
_NON_RETRYABLE_WA_REASONS = ("no_phone", "not_configured", "invalid_format", "unknown_provider", "outside_24h_window")


def send_daily_quote_whatsapp(phone, quote_text, author=None, employee_name=None, max_retries=3):
    """
    Send motivational quote via the configured WhatsApp provider (Meta Cloud API
    or Twilio), from the Founder's WhatsApp Business number.
    Validates/normalizes the number to E.164, then retries transient failures
    up to `max_retries` times with exponential backoff (1s, 2s, 4s ...).
    Permanent failures (bad number, missing config) are not retried.
    Returns (success: bool, reason: str, provider_response: str|None).
    """
    phone_clean, err = normalize_phone_e164(phone)
    if err:
        return False, err, None

    message = build_daily_quote_message(quote_text)

    reason, provider_response = "unknown_error", None
    for attempt in range(max_retries):
        ok, reason, provider_response = _send_whatsapp_attempt(phone_clean, message, employee_name, quote_text)
        if ok:
            return True, "sent", provider_response
        if reason in _NON_RETRYABLE_WA_REASONS:
            return False, reason, provider_response
        if attempt < max_retries - 1:
            backoff = 2 ** attempt  # 1s, 2s, 4s
            print(f"[QUOTE WA] Retry {attempt + 1}/{max_retries} for {phone_clean} in {backoff}s ({reason})")
            time.sleep(backoff)

    return False, reason, provider_response


def dispatch_daily_quote():
    """
    Core job: pick today's quote, send to all users via email, WhatsApp and chat.
    Deduplication prevents duplicate sends even if the scheduler fires multiple times.
    """
    with app.app_context():
        today   = today_str()
        now     = now_ist()
        now_str_val = now.strftime("%I:%M %p")
        now_iso_val = now.strftime("%Y-%m-%dT%H:%M:%S")

        dq = get_or_create_daily_quote()
        quote_text = dq.quote_text
        author     = dq.author

        users = User.query.all()

        # ── 1. Insert system chat message (once per day) ──────────
        already_in_chat = QuoteDeliveryLog.query.filter_by(
            date=today, userId="__system__", channel="chat"
        ).first()
        if not already_in_chat:
            today_display = now.strftime("%d %B %Y")
            chat_text = (
                f"🌞 Daily Motivation\n\n"
                f"📅 {today_display}\n\n"
                f"\"{quote_text}\"\n"
                f"— {author}"
            )
            sys_msg = Message(
                id        = "qm" + str(int(now.timestamp() * 1000)),
                from_name = "🌱 PGI TaskFlow",
                fromId    = "system",
                text      = chat_text,
                time      = now_str_val,
                timestamp = now_iso_val,
                channel   = "all"
            )
            db.session.add(sys_msg)
            db.session.add(QuoteDeliveryLog(
                date=today, userId="__system__", channel="chat",
                status="sent", sent_at=now_iso_val
            ))

        # ── 2. In-app notification per user ───────────────────────
        for user in users:
            already_notif = QuoteDeliveryLog.query.filter_by(
                date=today, userId=user.id, channel="notification"
            ).first()
            if not already_notif:
                make_notif(
                    userId=user.id,
                    ntype="motivation",
                    title="✨ Daily Motivation",
                    body=f'"{quote_text}" — {author}'
                )
                db.session.add(QuoteDeliveryLog(
                    date=today, userId=user.id, channel="notification",
                    status="sent", sent_at=now_iso_val
                ))

        # ── 3. Email delivery ─────────────────────────────────────
        for user in users:
            if not user.email:
                continue
            already_email = QuoteDeliveryLog.query.filter_by(
                date=today, userId=user.id, channel="email"
            ).first()
            if already_email:
                continue
            ok = send_daily_quote_email(user.email, quote_text, author)
            db.session.add(QuoteDeliveryLog(
                date=today, userId=user.id, channel="email",
                status="sent" if ok else "failed", sent_at=now_iso_val
            ))

        # ── 4. WhatsApp delivery ──────────────────────────────────
        wa_sent = wa_failed = wa_skipped = 0
        for user in users:
            already_wa = QuoteDeliveryLog.query.filter_by(
                date=today, userId=user.id, channel="whatsapp"
            ).first()
            if already_wa:
                if already_wa.status == "sent":
                    wa_sent += 1
                elif already_wa.status == "failed":
                    wa_failed += 1
                else:
                    wa_skipped += 1
                continue

            phone = (user.phone or "").strip()
            ok, reason, provider_response = send_daily_quote_whatsapp(phone, quote_text, author, user.name)

            if ok:
                status, err_msg = "sent", None
                wa_sent += 1
            elif reason in ("no_phone", "not_configured", "invalid_format"):
                status, err_msg = "skipped_" + reason, reason
                wa_skipped += 1
            else:
                status, err_msg = "failed", reason
                wa_failed += 1
                print(f"[QUOTE WA] Delivery failed for {user.name} ({phone}): {reason}")

            db.session.add(QuoteDeliveryLog(
                date=today, userId=user.id, channel="whatsapp",
                status=status, sent_at=now_iso_val, error_message=err_msg
            ))

        dq.sent_at = now_iso_val
        db.session.commit()
        print(f"[DAILY QUOTE] Dispatched for {today} to {len(users)} users "
              f"(WA sent={wa_sent} failed={wa_failed} skipped={wa_skipped})")

        # ── 5. Founder summary notification ────────────────────────
        already_summary = QuoteDeliveryLog.query.filter_by(
            date=today, userId="__system__", channel="founder_summary"
        ).first()
        if not already_summary:
            founders = User.query.filter(User.role.in_(["founder", "founder_assistant"])).all()
            for f in founders:
                make_notif(
                    userId=f.id,
                    ntype="quote_summary",
                    title="✅ Daily motivation quotes sent",
                    body=(
                        f"WhatsApp delivery — Success: {wa_sent}, "
                        f"Failed: {wa_failed}, Skipped: {wa_skipped}"
                    )
                )
            db.session.add(QuoteDeliveryLog(
                date=today, userId="__system__", channel="founder_summary",
                status="sent", sent_at=now_iso_val
            ))
            db.session.commit()


# ─────────────────────────────────────────────
# DAILY QUOTE APIs
# ─────────────────────────────────────────────

@app.route("/api/daily-quote")
@jwt_required()
def get_daily_quote():
    dq = get_or_create_daily_quote()
    return jsonify({
        "date":    dq.date,
        "quote":   dq.quote_text,
        "author":  dq.author,
        "sent_at": dq.sent_at
    })


@app.route("/api/daily-quote/send-now", methods=["POST"])
@jwt_required()
def send_quote_now():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    try:
        dispatch_daily_quote()
        return jsonify({"success": True, "message": "Daily quote dispatched to all users."})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily-quote/stats")
@jwt_required()
def daily_quote_stats():
    """Founder-only dashboard: last quote, recipients, success/failed/skipped, last run time."""
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    today = today_str()
    dq = DailyQuote.query.filter_by(date=today).first()

    wa_logs = QuoteDeliveryLog.query.filter_by(date=today, channel="whatsapp").all()
    sent_count    = sum(1 for l in wa_logs if l.status == "sent")
    failed_count  = sum(1 for l in wa_logs if l.status == "failed")
    skipped_count = sum(1 for l in wa_logs if l.status.startswith("skipped_"))

    total_recipients = User.query.filter(User.phone.isnot(None), User.phone != "").count()

    failures = [
        {"user_id": l.userId, "error": l.error_message}
        for l in wa_logs if l.status == "failed"
    ][:20]

    return jsonify({
        "date":             today,
        "last_quote":       dq.quote_text if dq else None,
        "last_quote_author": dq.author if dq else None,
        "last_execution":   dq.sent_at if dq else None,
        "total_recipients": total_recipients,
        "successful":       sent_count,
        "failed":           failed_count,
        "skipped":          skipped_count,
        "recent_failures":  failures,
    })


# ─────────────────────────────────────────────
# WHATSAPP WEBHOOK (Meta status callbacks)
# ─────────────────────────────────────────────

@app.route("/api/whatsapp/webhook", methods=["GET"])
def whatsapp_webhook_verify():
    """Meta's webhook verification handshake. Reads WHATSAPP_VERIFY_TOKEN —
    never hard-coded, never echoed back except the challenge Meta expects."""
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and verify_token and token == verify_token:
        return challenge, 200
    return jsonify({"error": "Verification failed"}), 403


@app.route("/api/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook_receive():
    """Records failed/undeliverable Meta template statuses (e.g. error 131047)
    so they're visible for follow-up instead of silently disappearing."""
    payload = request.get_json(silent=True) or {}
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for status in value.get("statuses", []):
                    if status.get("status") != "failed":
                        continue
                    for err in status.get("errors", [{}]):
                        db.session.add(WhatsAppWebhookEvent(
                            wa_message_id = status.get("id"),
                            recipient     = status.get("recipient_id"),
                            status        = status.get("status"),
                            error_code    = str(err.get("code", "")),
                            error_title   = err.get("title", ""),
                            error_message = err.get("message", ""),
                            error_details = (err.get("error_data") or {}).get("details", ""),
                            received_at   = now_iso()
                        ))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[WA WEBHOOK] Error processing callback: {e}")

    return jsonify({"success": True}), 200


@app.route("/api/whatsapp/test-template", methods=["POST"])
@jwt_required()
def test_whatsapp_template():
    """TEMPORARY test endpoint — sends the caller their own task_assignment
    template so template delivery can be verified end-to-end. Remove once
    the integration is confirmed working in production."""
    uid = get_jwt_identity()
    user = db.session.get(User, uid)
    phone = (user.phone or "").strip() if user else ""
    if not phone:
        return jsonify({"success": False, "reason": "no_phone"}), 400

    result = send_workflow_whatsapp(
        phone,
        "task_assignment",
        [
            (user.name or "there").split()[0],
            "WhatsApp Integration Test",
            today_str()
        ]
    )
    return jsonify({"success": result.get("success", False), "reason": result.get("reason")})


# ─────────────────────────────────────────────
# TODOS
# ─────────────────────────────────────────────

@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():
    uid = get_jwt_identity()
    todos = Todo.query.filter_by(user_id=uid).order_by(Todo.id.desc()).all()
    return jsonify([{
        "id": t.id, "title": t.title,
        "completed": t.completed, "created_at": t.created_at
    } for t in todos])


@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo():
    uid  = get_jwt_identity()
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    todo = Todo(
        user_id    = uid,
        title      = title,
        completed  = False,
        created_at = now_ist().strftime("%b %d, %Y %I:%M %p")
    )
    db.session.add(todo)
    db.session.commit()
    return jsonify({
        "id": todo.id, "title": todo.title,
        "completed": todo.completed, "created_at": todo.created_at
    }), 201


@app.route("/api/todos/<int:todo_id>", methods=["PUT"])
@jwt_required()
def update_todo(todo_id):
    uid  = get_jwt_identity()
    todo = Todo.query.filter_by(id=todo_id, user_id=uid).first()
    if not todo:
        return jsonify({"error": "Todo not found"}), 404
    data = request.get_json() or {}
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "Title cannot be empty"}), 400
        todo.title = title
    if "completed" in data:
        todo.completed = bool(data["completed"])
    db.session.commit()
    return jsonify({
        "id": todo.id, "title": todo.title,
        "completed": todo.completed, "created_at": todo.created_at
    })


@app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
@jwt_required()
def delete_todo(todo_id):
    uid  = get_jwt_identity()
    todo = Todo.query.filter_by(id=todo_id, user_id=uid).first()
    if not todo:
        return jsonify({"error": "Todo not found"}), 404
    db.session.delete(todo)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/todos/clear-completed", methods=["DELETE"])
@jwt_required()
def clear_completed_todos():
    uid = get_jwt_identity()
    deleted = Todo.query.filter_by(user_id=uid, completed=True).all()
    count   = len(deleted)
    for t in deleted:
        db.session.delete(t)
    db.session.commit()
    return jsonify({"success": True, "deleted": count})


# ─────────────────────────────────────────────
# SELF TASKS (Employee / Founder Assistant — private personal tasks)
# ─────────────────────────────────────────────

def self_task_to_dict(t):
    return {
        "id": t.id, "userId": t.userId, "title": t.title, "description": t.description,
        "priority": t.priority, "status": t.status, "created_date": t.created_date,
        "due_date": t.due_date, "follow_up_date": t.follow_up_date, "follow_up_time": t.follow_up_time,
        "completed": t.completed, "completed_at": t.completed_at,
        "notification_sent": t.notification_sent,
        "created_at": t.created_at, "updated_at": t.updated_at
    }


def _self_task_status(t):
    if t.completed:
        return "completed"
    if t.due_date:
        try:
            due = datetime.datetime.strptime(t.due_date, "%Y-%m-%d").date()
            if due < today_ist():
                return "overdue"
        except ValueError:
            pass
    return t.status or "pending"


def _self_task_permitted(user):
    return user and user.role in ("employee", "founder_assistant")


@app.route("/api/self-tasks", methods=["GET"])
@jwt_required()
def get_self_tasks():
    caller = db.session.get(User, get_jwt_identity())
    if not _self_task_permitted(caller):
        return jsonify({"error": "Forbidden"}), 403
    rows = SelfTask.query.filter_by(userId=caller.id).order_by(SelfTask.id.desc()).all()
    out = []
    for t in rows:
        d = self_task_to_dict(t)
        d["status"] = _self_task_status(t)
        out.append(d)
    return jsonify(out)


@app.route("/api/self-tasks", methods=["POST"])
@jwt_required()
def create_self_task():
    caller = db.session.get(User, get_jwt_identity())
    if not _self_task_permitted(caller):
        return jsonify({"error": "Forbidden"}), 403
    data  = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Task Title is required"}), 400
    now_iso_v = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
    t = SelfTask(
        userId=caller.id, title=title,
        description=(data.get("description") or "").strip(),
        priority=(data.get("priority") or "medium").strip().lower(),
        status="pending",
        created_date=today_str(),
        due_date=(data.get("due_date") or "").strip() or None,
        follow_up_date=(data.get("follow_up_date") or "").strip() or None,
        follow_up_time=(data.get("follow_up_time") or "").strip() or None,
        completed=False,
        notification_sent=False,
        created_at=now_iso_v,
        updated_at=now_iso_v
    )
    db.session.add(t)
    db.session.commit()
    d = self_task_to_dict(t); d["status"] = _self_task_status(t)
    return jsonify({"success": True, "task": d})


@app.route("/api/self-tasks/<int:tid>", methods=["PUT"])
@jwt_required()
def update_self_task(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not _self_task_permitted(caller):
        return jsonify({"error": "Forbidden"}), 403
    t = db.session.get(SelfTask, tid)
    if not t or t.userId != caller.id:
        return jsonify({"error": "Task not found"}), 404
    data = request.get_json() or {}
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "Task Title is required"}), 400
        t.title = title
    if "description" in data: t.description = (data["description"] or "").strip()
    if "priority"    in data: t.priority    = (data["priority"] or "medium").strip().lower()
    if "due_date"    in data: t.due_date    = (data["due_date"] or "").strip() or None

    old_fu_date = t.follow_up_date
    old_fu_time = t.follow_up_time
    if "follow_up_date" in data: t.follow_up_date = (data["follow_up_date"] or "").strip() or None
    if "follow_up_time" in data: t.follow_up_time = (data["follow_up_time"] or "").strip() or None
    if t.follow_up_date != old_fu_date or t.follow_up_time != old_fu_time:
        t.notification_sent = False

    t.updated_at = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
    db.session.commit()
    d = self_task_to_dict(t); d["status"] = _self_task_status(t)
    return jsonify({"success": True, "task": d})


@app.route("/api/self-tasks/<int:tid>", methods=["DELETE"])
@jwt_required()
def delete_self_task(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not _self_task_permitted(caller):
        return jsonify({"error": "Forbidden"}), 403
    t = db.session.get(SelfTask, tid)
    if not t or t.userId != caller.id:
        return jsonify({"error": "Task not found"}), 404
    db.session.delete(t)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/self-tasks/<int:tid>/complete", methods=["PATCH"])
@jwt_required()
def complete_self_task(tid):
    caller = db.session.get(User, get_jwt_identity())
    if not _self_task_permitted(caller):
        return jsonify({"error": "Forbidden"}), 403
    t = db.session.get(SelfTask, tid)
    if not t or t.userId != caller.id:
        return jsonify({"error": "Task not found"}), 404
    data = request.get_json() or {}
    completed = bool(data.get("completed", True))
    t.completed = completed
    t.status = "completed" if completed else "pending"
    t.completed_at = now_ist().strftime("%Y-%m-%dT%H:%M:%S") if completed else None
    t.updated_at = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
    db.session.commit()
    d = self_task_to_dict(t); d["status"] = _self_task_status(t)
    return jsonify({"success": True, "task": d})


def check_self_task_reminders():
    """Runs every minute (existing APScheduler). Checks all SelfTasks with a
    follow-up date/time whose time has passed and notification_sent == False,
    creates a reminder using the existing Notification table, then marks
    notification_sent = True."""
    with app.app_context():
        now = now_ist()
        pending = SelfTask.query.filter_by(notification_sent=False, completed=False).filter(
            SelfTask.follow_up_date.isnot(None)
        ).all()
        fired = 0
        for t in pending:
            try:
                time_str = t.follow_up_time or "00:00"
                due_dt = datetime.datetime.strptime(f"{t.follow_up_date} {time_str}", "%Y-%m-%d %H:%M")
                due_dt = due_dt.replace(tzinfo=IST)
            except ValueError:
                continue
            if now >= due_dt:
                make_notif(
                    userId=t.userId, ntype="self_task",
                    title="Self Task Reminder",
                    body=f"Follow up for: {t.title}\nDate: {t.follow_up_date}  Time: {t.follow_up_time or ''}"
                )
                t.notification_sent = True
                fired += 1

                # WhatsApp (best-effort — must never break the reminder scheduler)
                try:
                    owner = db.session.get(User, t.userId)
                    owner_phone = (owner.phone or "").strip() if owner else ""
                    owner_name  = owner.name if owner else ""
                    if owner_phone:
                        send_workflow_whatsapp(
                            owner_phone,
                            "task_update",
                            [owner_name, f'"{t.title}" — reminder', ""]
                        )
                except Exception as e:
                    print(f"[WA] task_update (self-task reminder) send failed for {t.userId}: {e}")
        if fired:
            try:
                db.session.commit()
                print(f"[SELF TASK] Fired {fired} reminder(s)")
            except Exception as exc:
                db.session.rollback()
                print(f"[SELF TASK] Commit error: {exc}")


# ─────────────────────────────────────────────
# BIRTHDAY ALERTS
# ─────────────────────────────────────────────

def check_birthday_alerts():
    with app.app_context():
        today     = today_ist()
        target    = today + datetime.timedelta(days=7)
        now_str_v = now_ist().strftime("%b %d %I:%M %p")
        year      = today.year

        candidates = User.query.filter(
            User.role.notin_(["founder", "founder_assistant"]),
            User.date_of_birth != None,
            User.date_of_birth != ""
        ).all()

        founders = User.query.filter(
            User.role.in_(["founder", "founder_assistant"])
        ).all()
        if not founders:
            return

        alerts_created = 0
        for emp in candidates:
            try:
                dob = datetime.datetime.strptime(emp.date_of_birth, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            try:
                this_year_bday = dob.replace(year=target.year)
            except ValueError:
                continue

            if this_year_bday != target:
                continue

            already = BirthdayAlert.query.filter_by(
                employee_id=emp.id,
                alert_year=year
            ).first()
            if already:
                continue

            first_name   = (emp.name or emp.id).split()[0]
            bday_display = this_year_bday.strftime("%B %d")
            title = f"🎂 Upcoming Birthday — {first_name}"
            body  = (
                f"{first_name}'s birthday is in 7 days ({bday_display}). "
                f"Wish preparation reminder."
            )

            for founder in founders:
                make_notif(
                    userId=founder.id,
                    ntype="birthday",
                    title=title,
                    body=body
                )

            # WhatsApp (best-effort — must never break the scheduler).
            # TODO CONFIRM: this alert fires 7 DAYS BEFORE the birthday as a
            # founder "wish preparation" heads-up, not a same-day greeting to
            # the employee. Sending the "birthday_wish" template to the
            # employee here means they'd get "Happy birthday!" a week early.
            # Left wired to employee.phone per the change request, but this
            # trigger point needs confirmation before going live.
            try:
                employee_phone = (emp.phone or "").strip()
                if employee_phone:
                    send_workflow_whatsapp(
                        employee_phone,
                        "birthday",
                        [first_name]
                    )
            except Exception as e:
                print(f"[WA] birthday_wish send failed for {emp.id}: {e}")

            db.session.add(BirthdayAlert(
                employee_id=emp.id,
                alert_year=year,
                sent_at=now_str_v
            ))
            alerts_created += 1
            print(f"[BIRTHDAY] Alert created for {emp.name} (birthday {bday_display})")

        if alerts_created:
            db.session.commit()
            print(f"[BIRTHDAY] {alerts_created} birthday alert(s) committed.")
        else:
            print("[BIRTHDAY] No upcoming birthdays in 7 days.")


@app.route("/api/birthday-alerts")
@jwt_required()
def get_birthday_alerts():
    caller = db.session.get(User, get_jwt_identity())
    if not caller or not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403

    today   = today_ist()
    results = []

    candidates = User.query.filter(
        User.role != "founder",
        User.date_of_birth != None,
        User.date_of_birth != ""
    ).all()

    for emp in candidates:
        try:
            dob = datetime.datetime.strptime(emp.date_of_birth, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        for delta in range(0, 8):
            check_date = today + datetime.timedelta(days=delta)
            try:
                this_year_bday = dob.replace(year=check_date.year)
            except ValueError:
                continue
            if this_year_bday == check_date:
                results.append({
                    "id":         emp.id,
                    "name":       emp.name or emp.id,
                    "role":       emp.role,
                    "department": emp.department or "",
                    "days_away":  delta,
                    "birthday":   this_year_bday.strftime("%B %d"),
                })
                break

    results.sort(key=lambda x: x["days_away"])
    return jsonify({"alerts": results})


# ─────────────────────────────────────────────
# DAILY TASK ASSIGNMENT (12:00 AM IST)
# ─────────────────────────────────────────────

_DAILY_TASK_TEMPLATES = [
    {
        "match_field": "team",
        "match_value": "content",
        "title":       "Full video edit and post on YouTube",
        "desc":        "Avoid mistakes that occurred previously.\nEnsure quality check before publishing.",
    },
    {
        "match_field": "team",
        "match_value": "bizdev",
        "title":       "Manage the sales work",
        "desc":        "Follow up leads, customer communication and sales activities.",
    },
    {
        "match_field": "team",
        "match_value": "technical",
        "title":       "Manage the technical works",
        "desc":        "Complete assigned development and maintenance work.",
    },
    {
        "match_field": "founder_assistant",
        "match_value": "founder_assistant",
        "title":       "Report work done by the team by EOD to Sir",
        "desc":        "Prepare and submit the daily work summary before end of day.",
    },
    {
        "match_field": "founder_assistant",
        "match_value": "founder_assistant",
        "title":       "Corporate leads follow-up daily for MOU",
        "desc":        "Follow up all corporate leads and maintain MOU progress updates.",
    },
]


def assign_daily_tasks():
    with app.app_context():
        today   = today_str()
        now_iso_v = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
        created = 0

        for tpl in _DAILY_TASK_TEMPLATES:
            mf = tpl["match_field"]
            mv = tpl["match_value"]

            if mf == "founder_assistant":
                targets = User.query.filter(
                    db.or_(
                        User.role == "founder_assistant",
                        User.team == "founder_assistant"
                    )
                ).all()
            else:
                targets = User.query.filter(
                    User.team == mv,
                    User.role.notin_(["founder", "founder_assistant"])
                ).all()

            for user in targets:
                exists = Task.query.filter_by(
                    assignedTo=user.id,
                    title=tpl["title"],
                    due=today
                ).first()
                if exists:
                    continue

                task_id = "dt_" + user.id + "_" + today.replace("-", "") + \
                          "_" + str(abs(hash(tpl["title"])))[-6:]
                task = Task(
                    id         = task_id,
                    title      = tpl["title"],
                    desc       = tpl["desc"],
                    assignedTo = user.id,
                    assignedBy = "system",
                    status     = "Pending",
                    priority   = "High",
                    due        = today,
                    createdAt  = now_iso_v,
                )
                db.session.add(task)

                # Automatically generated daily task — notify immediately after creation
                make_notif(
                    userId = user.id,
                    ntype  = "task",
                    title  = "New Daily Reminder",
                    body   = f"Today's reminder: {tpl['title']}"
                )
                created += 1

        try:
            db.session.commit()
            print(f"[SCHEDULER] assign_daily_tasks: created {created} task(s) for {today}")
        except Exception as exc:
            db.session.rollback()
            print(f"[SCHEDULER] assign_daily_tasks ERROR: {exc}")


# ─────────────────────────────────────────────
# APSCHEDULER  — all jobs including auto daily quote at 12:00 AM
# ─────────────────────────────────────────────

if APSCHEDULER_AVAILABLE:
    _scheduler = BackgroundScheduler(timezone=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))

    # Daily motivation quote — fires at 12:00 AM IST (midnight) every day
    _scheduler.add_job(
        dispatch_daily_quote,
        trigger="cron",
        hour=0,
        minute=0,
        id="auto_daily_quote_midnight",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Daily quote also sent at 7:00 AM as a reminder (won't duplicate — dedup prevents it)
    _scheduler.add_job(
        dispatch_daily_quote,
        trigger="cron",
        hour=7,
        minute=0,
        id="daily_quote_morning",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Birthday alerts — 8:00 AM IST
    _scheduler.add_job(
        check_birthday_alerts,
        trigger="cron",
        hour=8,
        minute=0,
        id="birthday_alert_job",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Daily task assignment — 12:00 AM IST (immediately after quote)
    _scheduler.add_job(
        assign_daily_tasks,
        trigger="cron",
        hour=0,
        minute=1,
        id="daily_task_assignment_job",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Auto Task Templates dispatcher — 12:02 AM IST daily (weekly/monthly templates)
    _scheduler.add_job(
        dispatch_task_templates,
        trigger="cron",
        hour=0,
        minute=2,
        id="auto_task_templates_job",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Daily Task Templates dispatcher — 09:00 AM IST every day (frequency == "daily")
    _scheduler.add_job(
        generate_daily_tasks,
        trigger="cron",
        hour=9,
        minute=0,
        id="daily_task_templates_9am_job",
        replace_existing=True,
        misfire_grace_time=3600
    )

# Self Task reminder checker — every minute
    _scheduler.add_job(
        check_self_task_reminders,
        trigger="interval",
        minutes=1,
        id="self_task_reminder_job",
        replace_existing=True,
        misfire_grace_time=55
    )

    _scheduler.start()
    print("[SCHEDULER] Auto daily quote: 12:00 AM IST (midnight) + 7:00 AM IST")
    print("[SCHEDULER] Birthday alerts:  08:00 AM IST")
    print("[SCHEDULER] Daily task assign: 12:01 AM IST")
    print("[SCHEDULER] Auto task templates: 12:02 AM IST")
    print("[SCHEDULER] Daily task templates (9AM): 09:00 AM IST")

    # ── Restart recovery ────────────────────────────────────────
    # If the app restarts after midnight and the cron fire was missed
    # (e.g. downtime longer than misfire_grace_time), make sure today's
    # quote still goes out once. dispatch_daily_quote() is itself
    # idempotent per user/channel/day, so this never duplicates sends.
    def _startup_quote_recovery():
        try:
            with app.app_context():
                dq = DailyQuote.query.filter_by(date=today_str()).first()
                if not dq or not dq.sent_at:
                    print("[QUOTE RECOVERY] No completed dispatch found for today — sending now.")
                    dispatch_daily_quote()
        except Exception as exc:
            print(f"[QUOTE RECOVERY] Error: {exc}")

    threading.Thread(target=_startup_quote_recovery, daemon=True).start()


# ─────────────────────────────────────────────
# AI HELP ASSISTANT — Groq-powered chat with tool calling
# ─────────────────────────────────────────────

def _safe_uname(uid):
    u = db.session.get(User, uid)
    return u.name if u else uid


def build_help_system_prompt(user):
    prompt = f"""You are the TaskFlow Assistant for Plant Green Inertia (PGI). You help {user.name} ({user.role}) use the TaskFlow dashboard.

TaskFlow has these areas:
- Vision Board: overview of tasks, projects, and goals
- My Tasks: tasks assigned to the user (pending, in progress, submitted, completed)
- Attendance: check in/out, view attendance percentage, request leave
- Teams: browse teams and members
- Messages: company-wide and team chat channels
- Calendar: shared company calendar
- My Notepad: private personal to-do list
"""
    if is_founder_like(user):
        prompt += """
As founder-level staff, this user can also: Assign Task, Verify Proofs, Manage Users, Rate Employees, Auto Tasks (task templates), Attendance Map, and PGI Mastermind (projects & interns).

TASK ASSIGNMENT — MANDATORY BEHAVIOR:
Whenever the user's message means "assign a task", "create a task for someone", "give X work to Y",
or "allocate a task", you MUST call the assign_task tool — never just say you'll do it or chat about it.
- If the tool result has "multiple_matches": true, relay its "message" field verbatim and ask the
  user to clarify. Do NOT guess which employee they meant.
- If the tool result has an "error", relay that exact error message back to the user.
- If the title of the task is missing from the user's message, ask: "What task would you like to assign?"
  before calling the tool.
- Only call assign_task with confirm=true after the user has explicitly confirmed (e.g. "yes", "confirm", "go ahead").
"""
    else:
        prompt += """
This user does NOT have permission to assign tasks. If they ask to assign, create, give, or allocate
a task to someone else, respond with exactly: "You don't have permission to assign tasks."
Do not attempt to perform or promise the assignment.
"""
    prompt += """
Use the available tools to look up real data or perform actions when asked — never invent numbers or statuses.

For any action that changes data (check in, check out, requesting leave, assigning a task), first explain in plain language exactly what you're about to do, then ask the user to explicitly confirm. Only call the tool with confirm=true after the user has clearly said yes/confirm/go ahead in the conversation. Never set confirm=true on the first attempt.

Keep replies concise, warm, and specific to TaskFlow. If asked something unrelated to TaskFlow, politely redirect to what you can help with here.
"""
    return prompt


def build_help_tools(user):
    tools = [
        {"type": "function", "function": {
            "name": "get_attendance_status",
            "description": "Get the current user's attendance status for today, including check-in/check-out time.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_attendance_percentage",
            "description": "Get the current user's attendance percentage and working-day statistics.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_my_tasks_summary",
            "description": "Get a count of the current user's tasks grouped by status.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "check_in",
            "description": "Check the current user in for today's attendance. Only executes when confirm=true.",
            "parameters": {"type": "object", "properties": {
                "confirm": {"type": "boolean", "description": "Set true only after the user has explicitly confirmed."}
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "check_out",
            "description": "Check the current user out for today. Only executes when confirm=true.",
            "parameters": {"type": "object", "properties": {
                "confirm": {"type": "boolean"}
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "request_leave",
            "description": "Submit a leave request for the current user for a specific date. Only executes when confirm=true.",
            "parameters": {"type": "object", "properties": {
                "date":    {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "reason":  {"type": "string", "description": "Reason for leave"},
                "confirm": {"type": "boolean"}
            }, "required": ["date"]}
        }},
    ]
    if is_founder_like(user):
        tools.append({"type": "function", "function": {
            "name": "get_pending_leave_requests",
            "description": "List all pending leave requests awaiting founder approval.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }})
        tools.append({"type": "function", "function": {
            "name": "assign_task",
            "description": (
                "Assign a new task to a team member — identical to using the manual "
                "'Assign Task' form. Only founder-level users may call this. "
                "Always call this tool when the user asks to assign/create/give/allocate "
                "a task to someone; never just reply conversationally. "
                "Requires confirm=true to actually create the task, after a preview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee":    {"type": "string", "description": "Name, partial name, email, or initials of the employee."},
                    "title":       {"type": "string", "description": "Short task title."},
                    "description": {"type": "string", "description": "Task details/description."},
                    "priority":    {"type": "string", "enum": ["high", "medium", "low"]},
                    "due":         {"type": "string", "description": "Due date YYYY-MM-DD, optional."},
                    "confirm":     {"type": "boolean"}
                },
                "required": ["employee", "title"]
            }
        }})
    return tools


def execute_help_tool(fname, fargs, user):
    """Returns (result_dict, needs_confirmation: bool)."""
    uid = user.id

    if fname == "get_attendance_status":
        att = Attendance.query.filter_by(userId=uid, date=today_str()).first()
        return ({
            "status": att.status if att else "absent",
            "checkin_time": att.checkin_time if att else None,
            "checkout_time": att.checkout_time if att else None,
        }, False)

    if fname == "get_attendance_percentage":
        return (attendance_stats_for_user(user), False)

    if fname == "get_my_tasks_summary":
        tasks = Task.query.filter_by(assignedTo=uid).all()
        summary = {}
        for t in tasks:
            summary[t.status] = summary.get(t.status, 0) + 1
        return ({"total": len(tasks), "by_status": summary}, False)

    if fname == "check_in":
        att_existing = Attendance.query.filter_by(userId=uid, date=today_str()).first()
        if att_existing and att_existing.checkin_time:
            return ({"already_checked_in": True, "checkin_time": att_existing.checkin_time}, False)
        if not fargs.get("confirm"):
            return ({"preview": "Ready to check you in for today. Awaiting confirmation."}, True)
        now_ts = now_iso()
        if att_existing:
            att_existing.status = "present"
            att_existing.checkin_time = now_ts
            att_existing.checkout_time = None
            att_existing.worked_hours = None
        else:
            db.session.add(Attendance(userId=uid, status="present", date=today_str(), checkin_time=now_ts))
        db.session.add(AuditLog(action=f"{user.name} checked in via AI assistant",
                                 performed_by=user.name, timestamp=now_ist().strftime("%b %d, %Y %I:%M %p")))
        db.session.commit()
        return ({"success": True, "checkin_time": now_ts}, False)

    if fname == "check_out":
        att = Attendance.query.filter_by(userId=uid, date=today_str()).first()
        if not att or not att.checkin_time:
            return ({"error": "You must check in before checking out"}, False)
        if att.checkout_time:
            return ({"already_checked_out": True, "checkout_time": att.checkout_time}, False)
        if not fargs.get("confirm"):
            return ({"preview": "Ready to check you out for today. Awaiting confirmation."}, True)
        now_ts = now_iso()
        att.checkout_time = now_ts
        att.worked_hours  = _compute_worked_hours(att.checkin_time, att.checkout_time)
        att.status = "completed"
        db.session.add(AuditLog(action=f"{user.name} checked out via AI assistant",
                                 performed_by=user.name, timestamp=now_ist().strftime("%b %d, %Y %I:%M %p")))
        db.session.commit()
        return ({"success": True, "checkout_time": now_ts, "worked_hours": att.worked_hours}, False)

    if fname == "request_leave":
        date   = (fargs.get("date") or "").strip()
        reason = (fargs.get("reason") or "").strip()
        if not date:
            return ({"error": "date is required, e.g. 2026-07-10"}, False)
        if not fargs.get("confirm"):
            preview = f"Ready to submit a leave request for {date}"
            if reason:
                preview += f" (reason: {reason})"
            preview += ". Awaiting confirmation."
            return ({"preview": preview}, True)
        lr = LeaveRequest(userId=uid, date=date, reason=reason, status="pending",
                           requested_at=now_ist().strftime("%b %d, %Y %I:%M %p"))
        db.session.add(lr)
        db.session.add(AuditLog(action=f"{user.name} requested leave for {date} via AI assistant",
                                 performed_by=user.name, timestamp=now_ist().strftime("%b %d, %Y %I:%M %p")))
        db.session.commit()
        for f in User.query.filter(User.role.in_(["founder", "founder_assistant"])).all():
            make_notif(userId=f.id, ntype="leave", title="📝 Leave Request",
                       body=f"{user.name} requested leave for {date}")
        db.session.commit()
        return ({"success": True, "id": lr.id}, False)

    if fname == "get_pending_leave_requests" and is_founder_like(user):
        rows = LeaveRequest.query.filter_by(status="pending").all()
        return ({"pending": [{"id": r.id, "user": _safe_uname(r.userId), "date": r.date, "reason": r.reason} for r in rows]}, False)

    if fname == "assign_task":
        if not is_founder_like(user):
            return ({"error": "You don't have permission to assign tasks."}, False)

        employee_query = (fargs.get("employee") or "").strip()
        title          = (fargs.get("title") or "").strip()
        if not employee_query or not title:
            return ({"error": "What task would you like to assign, and to whom?"}, False)

        matches = find_employee_matches(employee_query)
        if not matches:
            return ({"error": "I couldn't find that employee."}, False)
        if len(matches) > 1:
            names = [m.name for m in matches]
            return ({
                "multiple_matches": True,
                "candidates": names,
                "message": "Did you mean " + " or ".join(names) + "?"
            }, False)

        assignee = matches[0]
        desc     = (fargs.get("description") or "").strip() or title
        priority = fargs.get("priority") or "medium"
        due      = (fargs.get("due") or "").strip() or "TBD"

        if not fargs.get("confirm"):
            return ({"preview": f'Ready to assign "{title}" to {assignee.name}. Awaiting confirmation.'}, True)

        task = create_task_record(
            assignee_id=assignee.id, title=title, desc=desc,
            priority=priority, due=due, msg="",
            assigned_by_id=uid
        )
        db.session.add(AuditLog(
            action=f"{user.name} assigned task '{title}' to {assignee.name} via AI assistant",
            performed_by=user.name,
            timestamp=now_ist().strftime("%b %d, %Y %I:%M %p")
        ))
        db.session.commit()
        return ({"success": True, "assignee": assignee.name, "title": title, "task_id": task.id}, False)

    return ({"error": f"Unknown or unauthorized tool: {fname}"}, False)


# ─────────────────────────────────────────────
# AI CHAT — private, per-user history
# ─────────────────────────────────────────────

@app.route("/api/chat/history", methods=["GET"])
@jwt_required()
def get_chat_history():
    uid = get_jwt_identity()
    rows = (AIChatMessage.query
            .filter_by(user_id=uid)
            .order_by(AIChatMessage.id.asc())
            .all())
    return jsonify([{
        "role": r.role, "content": r.content, "created_at": r.created_at
    } for r in rows])


@app.route("/api/chat/history", methods=["DELETE"])
@jwt_required()
def clear_chat_history():
    uid = get_jwt_identity()
    deleted = AIChatMessage.query.filter_by(user_id=uid).delete()
    db.session.commit()
    return jsonify({"success": True, "deleted": deleted})


@app.route("/api/chat", methods=["POST"])
@jwt_required()
def post_chat():
    uid  = get_jwt_identity()
    user = db.session.get(User, uid)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not GROQ_API_KEY:
        return jsonify({"success": False, "error": "Groq API key is missing or invalid."}), 503

    data = request.get_json() or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "No message provided"}), 400

    history_rows = (AIChatMessage.query
                     .filter_by(user_id=uid)
                     .order_by(AIChatMessage.id.asc())
                     .all())
    history = [{"role": r.role, "content": r.content} for r in history_rows][-30:]

    now_ts = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
    db.session.add(AIChatMessage(user_id=uid, role="user", content=user_text, created_at=now_ts))
    db.session.commit()

    messages = [{"role": "system", "content": build_help_system_prompt(user)}] + history + \
               [{"role": "user", "content": user_text}]

    try:
        result = groq_chat(messages, tools=build_help_tools(user))
    except Exception as e:
        return jsonify({"success": False, "error": f"AI service error: {e}"}), 502

    choice     = result.get("choices", [{}])[0].get("message", {})
    tool_calls = choice.get("tool_calls")
    pending    = []
    final_msg  = choice.get("content", "")

    if tool_calls:
        messages.append(choice)
        for tc in tool_calls:
            fname = tc["function"]["name"]
            try:
                fargs = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                fargs = {}
            tool_result, needs_confirm = execute_help_tool(fname, fargs, user)
            if needs_confirm:
                pending.append({"tool": fname, "args": fargs, "summary": tool_result.get("preview", "")})
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "name": fname,
                "content": json.dumps(tool_result)
            })
        try:
            followup  = groq_chat(messages)
            final_msg = followup.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception:
            final_msg = "I hit an issue completing that — please try again."

    db.session.add(AIChatMessage(
        user_id=uid, role="assistant", content=final_msg,
        created_at=now_ist().strftime("%Y-%m-%dT%H:%M:%S")
    ))
    db.session.commit()

    return jsonify({"reply": final_msg, "pending_confirmations": pending})

# ─────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────

@app.route("/api/debug-groq")
@jwt_required()
def debug_groq():
    caller = db.session.get(User, get_jwt_identity())
    if not is_founder_like(caller):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({
        "key_present": bool(GROQ_API_KEY),
        "key_length": len(GROQ_API_KEY),
        "key_prefix": GROQ_API_KEY[:6] if GROQ_API_KEY else None,
    })

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return jsonify({"error": "Internal server error: " + str(e)}), 500

@app.errorhandler(Exception)
def unhandled_exception(e):
    db.session.rollback()
    import traceback
    traceback.print_exc()
    return jsonify({"error": "Unexpected error: " + str(e)}), 500

@app.route("/manifest.json")
def manifest():
    return send_file("manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    resp = send_file("sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_file(f"static/{filename}")

@app.route("/")
def home():
    return send_file("taskflow_v3.html")


if __name__ == "__main__":
    print("=" * 50)
    print("  TaskFlow — Plant Green Inertia")
    print("  http://localhost:5000")
    print("Server Timezone:", now_ist())
    print("=" * 50)
    app.run(debug=True, host="0.0.0.0", port=5000)
