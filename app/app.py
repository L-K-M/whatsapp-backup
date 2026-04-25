from __future__ import annotations

import datetime as dt
import smtplib
import ssl
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from collections import deque
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, Optional

from flask import Flask, abort, jsonify, request, send_file, send_from_directory


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
WACLI_STORE_DIR = Path(os.environ.get("WACLI_STORE_DIR", str(DATA_DIR / "wacli"))).resolve()
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", str(DATA_DIR / "archive"))).resolve()
MESSAGE_DIR = ARCHIVE_DIR / "messages"
WACLI_BIN = os.environ.get("WACLI_BIN", "/usr/local/bin/wacli")
WEB_ROOT = Path(os.environ.get("WEB_ROOT", "/app/web")).resolve()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

EXPORT_INTERVAL_SECONDS = int(os.environ.get("EXPORT_INTERVAL_SECONDS", "60"))
SUPERVISOR_INTERVAL_SECONDS = int(os.environ.get("SUPERVISOR_INTERVAL_SECONDS", "15"))
STATUS_CACHE_SECONDS = int(os.environ.get("STATUS_CACHE_SECONDS", "10"))
AUTH_IDLE_EXIT = os.environ.get("AUTH_IDLE_EXIT", "30s")
AUTO_SYNC = os.environ.get("AUTO_SYNC", "1").strip().lower() not in {"0", "false", "no", "off"}
MAX_PROCESS_LOG_LINES = int(os.environ.get("MAX_PROCESS_LOG_LINES", "600"))
MAX_REQUEST_LIMIT = int(os.environ.get("MAX_REQUEST_LIMIT", "500"))
SYNC_MAX_RECONNECT = os.environ.get("SYNC_MAX_RECONNECT", "30m")
SYNC_RESTART_MIN_SECONDS = max(60, int(os.environ.get("SYNC_RESTART_MIN_SECONDS", "300")))
SYNC_RESTART_MAX_SECONDS = max(
    SYNC_RESTART_MIN_SECONDS,
    int(os.environ.get("SYNC_RESTART_MAX_SECONDS", "3600")),
)
SYNC_STABLE_SECONDS = max(60, int(os.environ.get("SYNC_STABLE_SECONDS", "300")))
NOTIFICATION_STATE_FILE = Path(
    os.environ.get("NOTIFICATION_STATE_FILE", str(DATA_DIR / ".whatsapp-backup-state.json"))
).resolve()

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME).strip()
SMTP_TO = os.environ.get("SMTP_TO", "").strip()
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1").strip().lower() not in {"0", "false", "no", "off"}
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}
SMTP_TIMEOUT_SECONDS = int(os.environ.get("SMTP_TIMEOUT_SECONDS", "20"))
SMTP_SUBJECT_PREFIX = os.environ.get("SMTP_SUBJECT_PREFIX", "[WhatsApp Backup]").strip()

ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[ -/]*[@-~]")
SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
HEADER_RE = re.compile(r"^([^:]+):\s?(.*)$")


app = Flask(__name__)

state_lock = threading.RLock()
index_lock = threading.RLock()
shutdown_requested = threading.Event()

auth_process: Optional[subprocess.Popen[str]] = None
sync_process: Optional[subprocess.Popen[str]] = None
auth_lines: deque[str] = deque(maxlen=MAX_PROCESS_LOG_LINES)
sync_lines: deque[str] = deque(maxlen=MAX_PROCESS_LOG_LINES)

auth_status_cache: dict[str, Any] = {
    "authenticated": False,
    "checked_at": None,
    "error": None,
}
auth_status_checked_monotonic = 0.0

export_state: dict[str, Any] = {
    "last_export_at": None,
    "last_export_error": None,
    "last_exported_count": 0,
    "last_indexed_count": 0,
}

notification_state_loaded = False
notification_state: dict[str, Any] = {
    "ever_restored": False,
    "outage_active": False,
    "email_sent_for_outage": False,
    "last_failure_at": None,
    "last_failure_reason": "",
    "last_restored_at": None,
    "last_email_attempt_at": None,
    "last_email_sent_at": None,
    "last_email_error": None,
}

sync_process_started_at: Optional[str] = None
auth_process_started_at: Optional[str] = None
sync_started_monotonic: Optional[float] = None
next_sync_start_monotonic = 0.0
sync_stop_expected = False
sync_control_state: dict[str, Any] = {
    "restart_attempts": 0,
    "next_start_at": None,
    "last_start_at": None,
    "last_exit_at": None,
    "last_exit_code": None,
    "last_failure": None,
    "stable_after_seconds": SYNC_STABLE_SECONDS,
    "restart_min_seconds": SYNC_RESTART_MIN_SECONDS,
    "restart_max_seconds": SYNC_RESTART_MAX_SECONDS,
    "max_reconnect": SYNC_MAX_RECONNECT,
}

message_index: list[dict[str, Any]] = []
chat_index: list[dict[str, Any]] = []
index_built_at: Optional[str] = None


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def future_iso(seconds: float) -> str:
    value = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=max(0, seconds))
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def unix_to_iso(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\x00", "")


def sanitize_segment(value: str, fallback: str = "item") -> str:
    cleaned = SAFE_SEGMENT_RE.sub("_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or fallback


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WACLI_STORE_DIR.mkdir(parents=True, exist_ok=True)
    MESSAGE_DIR.mkdir(parents=True, exist_ok=True)
    load_notification_state()


def smtp_enabled() -> bool:
    return bool(SMTP_HOST and SMTP_TO and SMTP_FROM)


def public_notification_state() -> dict[str, Any]:
    load_notification_state()
    with state_lock:
        state = dict(notification_state)
    state["smtp"] = {
        "enabled": smtp_enabled(),
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "from": SMTP_FROM,
        "to": SMTP_TO,
        "use_tls": SMTP_USE_TLS,
        "use_ssl": SMTP_USE_SSL,
    }
    return state


def load_notification_state() -> None:
    global notification_state_loaded
    with state_lock:
        if notification_state_loaded:
            return
        notification_state_loaded = True
        if not NOTIFICATION_STATE_FILE.exists():
            return
        try:
            data = json.loads(NOTIFICATION_STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            app.logger.warning("Failed to load notification state: %s", exc)
            return
        if isinstance(data, dict):
            for key in notification_state:
                if key in data:
                    notification_state[key] = data[key]


def persist_notification_state_locked() -> None:
    try:
        NOTIFICATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = NOTIFICATION_STATE_FILE.with_suffix(NOTIFICATION_STATE_FILE.suffix + ".tmp")
        temp_file.write_text(json.dumps(notification_state, ensure_ascii=True), encoding="utf-8")
        temp_file.replace(NOTIFICATION_STATE_FILE)
    except Exception as exc:
        app.logger.warning("Failed to persist notification state: %s", exc)


def send_connection_failure_email(reason: str, failed_at: str) -> Optional[str]:
    if not smtp_enabled():
        return "SMTP is not fully configured; set SMTP_HOST, SMTP_FROM, and SMTP_TO."

    subject_prefix = SMTP_SUBJECT_PREFIX or "[WhatsApp Backup]"
    message = EmailMessage()
    message["Subject"] = f"{subject_prefix} WhatsApp connection needs attention"
    message["From"] = SMTP_FROM
    message["To"] = SMTP_TO
    message.set_content(
        "WhatsApp Backup can no longer maintain the WhatsApp sync connection.\n\n"
        f"Failure time: {failed_at}\n"
        f"Reason: {reason}\n\n"
        "Open the WhatsApp Backup web UI and restore the linked-device connection. "
        "No further email will be sent for this outage until the connection is restored.\n"
    )

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                if SMTP_USERNAME or SMTP_PASSWORD:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.ehlo()
                if SMTP_USE_TLS:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if SMTP_USERNAME or SMTP_PASSWORD:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        return None
    except Exception as exc:
        return str(exc)


def mark_connection_failure(reason: str) -> None:
    load_notification_state()
    failed_at = now_iso()
    should_send = False
    with state_lock:
        notification_state["last_failure_at"] = failed_at
        notification_state["last_failure_reason"] = reason

        if notification_state.get("ever_restored"):
            if not notification_state.get("outage_active"):
                notification_state["outage_active"] = True
                notification_state["email_sent_for_outage"] = False

            if not notification_state.get("email_sent_for_outage"):
                notification_state["email_sent_for_outage"] = True
                notification_state["last_email_attempt_at"] = failed_at
                notification_state["last_email_error"] = None
                should_send = True
        persist_notification_state_locked()

    if not should_send:
        return

    error = send_connection_failure_email(reason, failed_at)
    with state_lock:
        if error:
            notification_state["last_email_error"] = error
        else:
            notification_state["last_email_sent_at"] = now_iso()
            notification_state["last_email_error"] = None
        persist_notification_state_locked()


def mark_connection_restored() -> None:
    load_notification_state()
    restored_at = now_iso()
    with state_lock:
        changed = (
            not notification_state.get("ever_restored")
            or notification_state.get("outage_active")
            or notification_state.get("email_sent_for_outage")
        )
        notification_state["ever_restored"] = True
        notification_state["outage_active"] = False
        notification_state["email_sent_for_outage"] = False
        notification_state["last_failure_reason"] = ""
        notification_state["last_email_error"] = None
        notification_state["last_restored_at"] = restored_at
        if changed:
            persist_notification_state_locked()


def process_running(proc: Optional[subprocess.Popen[str]]) -> bool:
    return proc is not None and proc.poll() is None


def append_process_line(lines: deque[str], line: str) -> None:
    cleaned = strip_ansi(line.rstrip("\n\r"))
    if cleaned:
        lines.append(cleaned)


def process_snapshot(
    proc: Optional[subprocess.Popen[str]],
    lines: deque[str],
    started_at: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "running": process_running(proc),
        "return_code": None if proc is None or proc.poll() is None else proc.returncode,
        "started_at": started_at,
        "lines": list(lines),
    }


def sync_backoff_remaining_seconds() -> int:
    return max(0, int(next_sync_start_monotonic - time.monotonic()))


def sync_can_start_locked() -> bool:
    return sync_backoff_remaining_seconds() <= 0


def record_sync_exit(exit_code: Optional[int]) -> None:
    global next_sync_start_monotonic, sync_stop_expected, sync_started_monotonic
    with state_lock:
        expected = sync_stop_expected
        sync_stop_expected = False
        runtime_seconds = 0
        if sync_started_monotonic is not None:
            runtime_seconds = int(max(0, time.monotonic() - sync_started_monotonic))
        sync_started_monotonic = None

        sync_control_state["last_exit_at"] = now_iso()
        sync_control_state["last_exit_code"] = exit_code

        if expected or shutdown_requested.is_set():
            sync_control_state["last_failure"] = None
            sync_control_state["next_start_at"] = None
            next_sync_start_monotonic = 0.0
            return

        if runtime_seconds >= SYNC_STABLE_SECONDS:
            sync_control_state["restart_attempts"] = 0

        sync_control_state["restart_attempts"] = int(sync_control_state.get("restart_attempts") or 0) + 1
        attempts = int(sync_control_state["restart_attempts"])
        delay = min(SYNC_RESTART_MAX_SECONDS, SYNC_RESTART_MIN_SECONDS * (2 ** (attempts - 1)))
        next_sync_start_monotonic = time.monotonic() + delay
        sync_control_state["next_start_at"] = future_iso(delay)
        sync_control_state["last_failure"] = f"wacli sync exited with code {exit_code}"
        append_process_line(
            sync_lines,
            f"Sync restart delayed for {delay} seconds after exit code {exit_code} (attempt {attempts}).",
        )


def wacli_base_cmd() -> list[str]:
    return [WACLI_BIN, "--store", str(WACLI_STORE_DIR)]


def wacli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WACLI_STORE_DIR"] = str(WACLI_STORE_DIR)
    env.setdefault("WACLI_DEVICE_LABEL", "WhatsApp Backup")
    return env


def start_logged_process(
    name: str,
    command: list[str],
    lines: deque[str],
) -> subprocess.Popen[str]:
    lines.clear()
    append_process_line(lines, f"Starting {name}.")
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=wacli_env(),
    )

    def reader() -> None:
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                with state_lock:
                    append_process_line(lines, raw_line)
        finally:
            proc.wait()
            with state_lock:
                append_process_line(lines, f"{name} exited with code {proc.returncode}")
            if name == "sync":
                record_sync_exit(proc.returncode)
            elif name == "auth":
                handle_auth_exit(proc.returncode)

    threading.Thread(target=reader, name=f"{name}-log-reader", daemon=True).start()
    return proc


def handle_auth_exit(return_code: Optional[int]) -> None:
    invalidate_auth_status()
    if return_code != 0:
        return

    def export_after_auth() -> None:
        try:
            written = export_messages_once()
            with state_lock:
                append_process_line(auth_lines, f"Exported {written} message text files after login.")
        except Exception as exc:
            with state_lock:
                append_process_line(auth_lines, f"Post-login export failed: {exc}")

    threading.Thread(target=export_after_auth, name="post-auth-export", daemon=True).start()


def stop_process(proc: Optional[subprocess.Popen[str]], name: str, timeout: int = 8) -> None:
    global sync_stop_expected
    if proc is None or proc.poll() is not None:
        return
    if name == "sync":
        sync_stop_expected = True
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


def run_wacli_json(args: list[str], timeout_seconds: int = 30) -> dict[str, Any]:
    command = wacli_base_cmd() + ["--json", "--timeout", f"{timeout_seconds}s"] + args
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds + 5,
        env=wacli_env(),
        check=False,
    )
    stdout = completed.stdout.strip()
    stderr = strip_ansi(completed.stderr.strip())
    payload: dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            payload = {}
    if completed.returncode != 0:
        error = payload.get("error") or stderr or stdout or f"wacli exited with {completed.returncode}"
        raise RuntimeError(str(error))
    return payload


def refresh_auth_status(force: bool = False) -> dict[str, Any]:
    global auth_status_checked_monotonic
    with state_lock:
        age = time.monotonic() - auth_status_checked_monotonic
        if not force and age < STATUS_CACHE_SECONDS:
            return dict(auth_status_cache)

    status: dict[str, Any] = {
        "authenticated": False,
        "checked_at": now_iso(),
        "error": None,
    }
    try:
        payload = run_wacli_json(["auth", "status"], timeout_seconds=20)
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            status.update(data)
            status["authenticated"] = bool(data.get("authenticated"))
    except Exception as exc:
        status["error"] = str(exc)

    with state_lock:
        auth_status_cache.clear()
        auth_status_cache.update(status)
        auth_status_checked_monotonic = time.monotonic()
        return dict(auth_status_cache)


def invalidate_auth_status() -> None:
    global auth_status_checked_monotonic
    with state_lock:
        auth_status_checked_monotonic = 0.0


def start_sync_locked() -> bool:
    global sync_process, sync_process_started_at, sync_started_monotonic, sync_stop_expected
    if process_running(sync_process):
        return False
    if process_running(auth_process):
        return False
    if not sync_can_start_locked():
        remaining = sync_backoff_remaining_seconds()
        append_process_line(sync_lines, f"Sync restart backoff active; next attempt in {remaining} seconds.")
        return False
    command = wacli_base_cmd() + [
        "--lock-wait",
        "30s",
        "sync",
        "--follow",
        "--download-media",
        "--refresh-contacts",
        "--refresh-groups",
        "--max-reconnect",
        SYNC_MAX_RECONNECT,
    ]
    sync_process_started_at = now_iso()
    sync_started_monotonic = time.monotonic()
    sync_stop_expected = False
    sync_control_state["last_start_at"] = sync_process_started_at
    sync_control_state["next_start_at"] = None
    sync_control_state["last_failure"] = None
    sync_process = start_logged_process("sync", command, sync_lines)
    return True


def start_auth_locked() -> bool:
    global auth_process, auth_process_started_at
    if process_running(auth_process):
        return False
    stop_process(sync_process, "sync")
    command = wacli_base_cmd() + [
        "--lock-wait",
        "30s",
        "auth",
        "--download-media",
        "--idle-exit",
        AUTH_IDLE_EXIT,
    ]
    auth_process_started_at = now_iso()
    auth_process = start_logged_process("auth", command, auth_lines)
    invalidate_auth_status()
    return True


def start_sync_if_authenticated() -> None:
    if not AUTO_SYNC:
        return
    status = refresh_auth_status()
    update_connection_health(status)
    if not status.get("authenticated"):
        return
    with state_lock:
        start_sync_locked()
    update_connection_health(status)


def update_connection_health(status: dict[str, Any]) -> None:
    if not AUTO_SYNC:
        return

    if status.get("error"):
        mark_connection_failure(f"Unable to check WhatsApp authentication: {status['error']}")
        return

    if not status.get("authenticated"):
        mark_connection_failure("WhatsApp is no longer authenticated. Re-link the device from the web UI.")
        return

    with state_lock:
        running = process_running(sync_process)
        stable = (
            running
            and sync_started_monotonic is not None
            and (time.monotonic() - sync_started_monotonic) >= SYNC_STABLE_SECONDS
        )
        last_failure = sync_control_state.get("last_failure")
        backoff_remaining = sync_backoff_remaining_seconds()
        if stable:
            sync_control_state["restart_attempts"] = 0
            sync_control_state["next_start_at"] = None
            sync_control_state["last_failure"] = None

    if stable:
        mark_connection_restored()
        return

    if last_failure and backoff_remaining > 0:
        mark_connection_failure(f"{last_failure}; next reconnect attempt in {backoff_remaining} seconds.")


def message_file_path(row: sqlite3.Row) -> Path:
    chat = sanitize_segment(row["chat_jid"], "chat")
    msg_id = sanitize_segment(row["msg_id"], "message")
    timestamp = unix_to_iso(row["ts"]) or "unknown-time"
    date_prefix = timestamp[:10] if len(timestamp) >= 10 else "unknown"
    year = date_prefix[:4] if len(date_prefix) >= 4 else "unknown"
    month = date_prefix[5:7] if len(date_prefix) >= 7 else "unknown"
    compact_ts = timestamp.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "Z")
    return MESSAGE_DIR / chat / year / month / f"{compact_ts}_{msg_id}.txt"


def relative_media_path(local_path: str) -> str:
    if not local_path:
        return ""
    try:
        resolved = Path(local_path).resolve()
        media_root = (WACLI_STORE_DIR / "media").resolve()
        return str(resolved.relative_to(media_root))
    except Exception:
        return ""


def build_message_text(row: sqlite3.Row) -> str:
    timestamp = unix_to_iso(row["ts"])
    downloaded_at = unix_to_iso(row["downloaded_at"])
    from_me = bool(row["from_me"])
    body = row["display_text"] or row["text"] or row["media_caption"] or ""
    media_rel = relative_media_path(row["local_path"] or "")
    headers = [
        "WhatsApp Backup Message",
        f"Chat-JID: {row['chat_jid'] or ''}",
        f"Chat-Name: {row['chat_name'] or ''}",
        f"Message-ID: {row['msg_id'] or ''}",
        f"Timestamp: {timestamp}",
        f"Direction: {'outgoing' if from_me else 'incoming'}",
        f"From-Me: {bool_text(from_me)}",
        f"Sender-JID: {row['sender_jid'] or ''}",
        f"Sender-Name: {row['sender_name'] or ''}",
        f"Media-Type: {row['media_type'] or ''}",
        f"Media-Caption: {row['media_caption'] or ''}",
        f"Media-Filename: {row['filename'] or ''}",
        f"Media-Mime: {row['mime_type'] or ''}",
        f"Media-Path: {row['local_path'] or ''}",
        f"Media-Rel: {media_rel}",
        f"Downloaded-At: {downloaded_at}",
        "Text:",
        body,
    ]
    return "\n".join(headers).rstrip() + "\n"


def atomic_write_text(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8", errors="replace") == content:
                return False
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def export_messages_once() -> int:
    ensure_dirs()
    db_path = WACLI_STORE_DIR / "wacli.db"
    if not db_path.exists():
        with state_lock:
            export_state.update(
                {
                    "last_export_at": now_iso(),
                    "last_export_error": None,
                    "last_exported_count": 0,
                }
            )
        rebuild_index()
        return 0

    query = """
        SELECT
            m.chat_jid,
            COALESCE(c.name, m.chat_name, '') AS chat_name,
            m.msg_id,
            COALESCE(m.sender_jid, '') AS sender_jid,
            COALESCE(m.sender_name, '') AS sender_name,
            m.ts,
            m.from_me,
            COALESCE(m.text, '') AS text,
            COALESCE(m.display_text, '') AS display_text,
            COALESCE(m.media_type, '') AS media_type,
            COALESCE(m.media_caption, '') AS media_caption,
            COALESCE(m.filename, '') AS filename,
            COALESCE(m.mime_type, '') AS mime_type,
            COALESCE(m.local_path, '') AS local_path,
            COALESCE(m.downloaded_at, 0) AS downloaded_at
        FROM messages m
        LEFT JOIN chats c ON c.jid = m.chat_jid
        ORDER BY m.ts ASC, m.rowid ASC
    """
    written = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(query):
                path = message_file_path(row)
                if atomic_write_text(path, build_message_text(row)):
                    written += 1
        finally:
            conn.close()
        with state_lock:
            export_state.update(
                {
                    "last_export_at": now_iso(),
                    "last_export_error": None,
                    "last_exported_count": written,
                }
            )
        rebuild_index()
        return written
    except Exception as exc:
        with state_lock:
            export_state.update(
                {
                    "last_export_at": now_iso(),
                    "last_export_error": str(exc),
                    "last_exported_count": written,
                }
            )
        raise


def write_archive_zip(zip_path: Path) -> int:
    ensure_dirs()
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ARCHIVE_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(ARCHIVE_DIR)
            archive.write(path, arcname=str(rel_path).replace(os.sep, "/"))
            file_count += 1
    return file_count


def export_download_name() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return f"whatsapp-backup-export-{timestamp}.zip"


def parse_message_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0] != "WhatsApp Backup Message":
        return None

    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines[1:]:
        if in_body:
            body_lines.append(line)
            continue
        if line == "Text:":
            in_body = True
            continue
        match = HEADER_RE.match(line)
        if match:
            headers[match.group(1).strip().lower().replace("-", "_")] = match.group(2)

    rel_file = str(path.relative_to(MESSAGE_DIR))
    body = "\n".join(body_lines).strip()
    msg = {
        "file": rel_file,
        "chat_jid": headers.get("chat_jid", ""),
        "chat_name": headers.get("chat_name", ""),
        "msg_id": headers.get("message_id", ""),
        "timestamp": headers.get("timestamp", ""),
        "direction": headers.get("direction", ""),
        "from_me": headers.get("from_me", "").lower() == "true",
        "sender_jid": headers.get("sender_jid", ""),
        "sender_name": headers.get("sender_name", ""),
        "media_type": headers.get("media_type", ""),
        "media_caption": headers.get("media_caption", ""),
        "media_filename": headers.get("media_filename", ""),
        "media_mime": headers.get("media_mime", ""),
        "media_path": headers.get("media_path", ""),
        "media_rel": headers.get("media_rel", ""),
        "downloaded_at": headers.get("downloaded_at", ""),
        "text": body,
    }
    searchable = "\n".join(
        [
            msg["chat_jid"],
            msg["chat_name"],
            msg["sender_jid"],
            msg["sender_name"],
            msg["media_caption"],
            msg["media_filename"],
            body,
        ]
    ).lower()
    msg["searchable"] = searchable
    return msg


def rebuild_index() -> None:
    global message_index, chat_index, index_built_at
    ensure_dirs()
    messages: list[dict[str, Any]] = []
    for path in MESSAGE_DIR.glob("**/*.txt"):
        msg = parse_message_file(path)
        if msg is not None:
            messages.append(msg)
    messages.sort(key=lambda item: (item.get("timestamp") or "", item.get("file") or ""), reverse=True)

    chats: dict[str, dict[str, Any]] = {}
    for msg in messages:
        jid = msg.get("chat_jid") or "unknown"
        chat = chats.setdefault(
            jid,
            {
                "jid": jid,
                "name": msg.get("chat_name") or jid,
                "count": 0,
                "last_message_at": "",
                "last_text": "",
                "last_sender": "",
            },
        )
        chat["count"] += 1
        if not chat["last_message_at"] or (msg.get("timestamp") or "") > chat["last_message_at"]:
            chat["last_message_at"] = msg.get("timestamp") or ""
            chat["last_text"] = summarize_text(msg)
            chat["last_sender"] = sender_label(msg)
            if msg.get("chat_name"):
                chat["name"] = msg["chat_name"]

    chats_list = sorted(chats.values(), key=lambda item: item.get("last_message_at") or "", reverse=True)
    with index_lock:
        message_index = messages
        chat_index = chats_list
        index_built_at = now_iso()
    with state_lock:
        export_state["last_indexed_count"] = len(messages)


def get_index() -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    with index_lock:
        return list(message_index), list(chat_index), index_built_at


def sender_label(msg: dict[str, Any]) -> str:
    if msg.get("from_me"):
        return "Me"
    return msg.get("sender_name") or msg.get("sender_jid") or "Unknown"


def summarize_text(msg: dict[str, Any]) -> str:
    text = (msg.get("text") or msg.get("media_caption") or "").strip().replace("\n", " ")
    if not text and msg.get("media_type"):
        text = f"[{msg['media_type']}]"
    return text[:180]


def public_message(msg: dict[str, Any], include_searchable: bool = False) -> dict[str, Any]:
    item = dict(msg)
    if not include_searchable:
        item.pop("searchable", None)
    item["sender_label"] = sender_label(item)
    item["summary"] = summarize_text(item)
    if item.get("media_rel"):
        item["media_url"] = "/api/media/" + item["media_rel"].replace("\\", "/")
    else:
        item["media_url"] = ""
    return item


def clamp_limit(value: str | None, default: int = 100) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        parsed = default
    return max(1, min(MAX_REQUEST_LIMIT, parsed))


def clamp_offset(value: str | None) -> int:
    try:
        parsed = int(value or 0)
    except ValueError:
        parsed = 0
    return max(0, parsed)


def filter_messages(messages: Iterable[dict[str, Any]], chat: str, query: str) -> list[dict[str, Any]]:
    query = query.strip().lower()
    chat = chat.strip()
    out: list[dict[str, Any]] = []
    for msg in messages:
        if chat and msg.get("chat_jid") != chat:
            continue
        if query and query not in msg.get("searchable", ""):
            continue
        out.append(msg)
    return out


def supervisor_loop() -> None:
    ensure_dirs()
    rebuild_index()
    next_export = 0.0
    while not shutdown_requested.is_set():
        now = time.monotonic()
        try:
            if now >= next_export:
                export_messages_once()
                next_export = now + EXPORT_INTERVAL_SECONDS
        except Exception:
            next_export = now + EXPORT_INTERVAL_SECONDS

        try:
            start_sync_if_authenticated()
        except Exception as exc:
            with state_lock:
                append_process_line(sync_lines, f"Sync supervisor error: {exc}")

        shutdown_requested.wait(SUPERVISOR_INTERVAL_SECONDS)


@app.get("/healthz")
def healthz() -> tuple[dict[str, Any], int]:
    return {"ok": True, "time": now_iso()}, 200


@app.get("/api/status")
def api_status() -> tuple[Any, int]:
    auth = refresh_auth_status()
    with index_lock:
        chat_count = len(chat_index)
        built_at = index_built_at
    with state_lock:
        payload = {
            "auth": auth,
            "auto_sync": AUTO_SYNC,
            "auth_process": process_snapshot(auth_process, auth_lines, auth_process_started_at),
            "sync_process": process_snapshot(sync_process, sync_lines, sync_process_started_at),
            "sync_control": {
                **dict(sync_control_state),
                "backoff_remaining_seconds": sync_backoff_remaining_seconds(),
            },
            "notifications": public_notification_state(),
            "export": dict(export_state),
            "index_built_at": built_at,
            "chat_count": chat_count,
            "data_dir": str(DATA_DIR),
            "wacli_store_dir": str(WACLI_STORE_DIR),
            "archive_dir": str(ARCHIVE_DIR),
        }
    return jsonify(payload), 200


@app.post("/api/auth/start")
def api_auth_start() -> tuple[Any, int]:
    with state_lock:
        started = start_auth_locked()
        payload = {"started": started, "auth_process": process_snapshot(auth_process, auth_lines, auth_process_started_at)}
    return jsonify(payload), 200


@app.post("/api/auth/logout")
def api_auth_logout() -> tuple[Any, int]:
    global auth_process, sync_process
    with state_lock:
        stop_process(auth_process, "auth")
        stop_process(sync_process, "sync")
    try:
        payload = run_wacli_json(["auth", "logout"], timeout_seconds=30)
        invalidate_auth_status()
        return jsonify({"logged_out": True, "wacli": payload}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/sync/start")
def api_sync_start() -> tuple[Any, int]:
    status = refresh_auth_status(force=True)
    if not status.get("authenticated"):
        return jsonify({"error": "WhatsApp is not authenticated yet."}), 409
    with state_lock:
        started = start_sync_locked()
        payload = {
            "started": started,
            "sync_process": process_snapshot(sync_process, sync_lines, sync_process_started_at),
            "sync_control": {
                **dict(sync_control_state),
                "backoff_remaining_seconds": sync_backoff_remaining_seconds(),
            },
        }
    return jsonify(payload), 200


@app.post("/api/sync/stop")
def api_sync_stop() -> tuple[Any, int]:
    with state_lock:
        stop_process(sync_process, "sync")
        payload = {"stopped": True, "sync_process": process_snapshot(sync_process, sync_lines, sync_process_started_at)}
    return jsonify(payload), 200


@app.post("/api/export/run")
def api_export_run() -> tuple[Any, int]:
    try:
        written = export_messages_once()
        return jsonify({"exported_count": written, "export": dict(export_state)}), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "export": dict(export_state)}), 500


@app.post("/api/export/download")
def api_export_download() -> Any:
    tmp_path: Optional[Path] = None
    file_handle: Any = None
    try:
        written = export_messages_once()
        with tempfile.NamedTemporaryFile(prefix="whatsapp-backup-", suffix=".zip", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
        file_count = write_archive_zip(tmp_path)
        file_handle = tmp_path.open("rb")
        response = send_file(
            file_handle,
            mimetype="application/zip",
            as_attachment=True,
            download_name=export_download_name(),
        )
        response.headers["Content-Length"] = str(tmp_path.stat().st_size)
        response.headers["X-Exported-Count"] = str(written)
        response.headers["X-Archive-File-Count"] = str(file_count)

        def cleanup_export_file() -> None:
            if file_handle is not None:
                file_handle.close()
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        response.call_on_close(cleanup_export_file)
        return response
    except Exception as exc:
        if file_handle is not None:
            file_handle.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return jsonify({"error": str(exc), "export": dict(export_state)}), 500


@app.get("/api/chats")
def api_chats() -> tuple[Any, int]:
    query = request.args.get("q", "").strip().lower()
    limit = clamp_limit(request.args.get("limit"), default=200)
    _, chats, _ = get_index()
    if query:
        chats = [
            chat
            for chat in chats
            if query in (chat.get("jid", "") + "\n" + chat.get("name", "") + "\n" + chat.get("last_text", "")).lower()
        ]
    return jsonify({"chats": chats[:limit], "total": len(chats)}), 200


@app.get("/api/messages")
def api_messages() -> tuple[Any, int]:
    chat = request.args.get("chat", "")
    query = request.args.get("q", "")
    limit = clamp_limit(request.args.get("limit"), default=100)
    offset = clamp_offset(request.args.get("offset"))
    messages, _, _ = get_index()
    filtered = filter_messages(messages, chat, query)
    page = filtered[offset : offset + limit]
    return jsonify(
        {
            "messages": [public_message(msg) for msg in page],
            "total": len(filtered),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(filtered),
        }
    ), 200


@app.get("/api/message")
def api_message() -> tuple[Any, int]:
    rel = request.args.get("file", "")
    if not rel:
        return jsonify({"error": "file is required"}), 400
    path = (MESSAGE_DIR / rel).resolve()
    try:
        path.relative_to(MESSAGE_DIR.resolve())
    except ValueError:
        return jsonify({"error": "invalid file path"}), 400
    msg = parse_message_file(path)
    if msg is None:
        return jsonify({"error": "message not found"}), 404
    return jsonify(public_message(msg)), 200


@app.get("/api/media/<path:relpath>")
def api_media(relpath: str) -> Any:
    media_root = (WACLI_STORE_DIR / "media").resolve()
    path = (media_root / relpath).resolve()
    try:
        path.relative_to(media_root)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.get("/")
def index() -> Any:
    return send_from_directory(WEB_ROOT, "index.html")


@app.get("/<path:path>")
def static_or_index(path: str) -> Any:
    candidate = WEB_ROOT / path
    if candidate.is_file():
        return send_from_directory(WEB_ROOT, path)
    return send_from_directory(WEB_ROOT, "index.html")


def shutdown(_signum: int, _frame: object) -> None:
    shutdown_requested.set()
    with state_lock:
        stop_process(auth_process, "auth")
        stop_process(sync_process, "sync")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


if __name__ == "__main__":
    ensure_dirs()
    threading.Thread(target=supervisor_loop, name="supervisor", daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
