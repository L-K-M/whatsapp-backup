import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import app as backup


DEVICE_JID = "15551234567:23@s.whatsapp.net"
LINKED_JID = "15551234567@s.whatsapp.net"


class AuthStatusTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            shutil.which(backup.WACLI_BIN),
            "Build wacli and set WACLI_BIN before running integration tests.",
        )
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store_dir = Path(directory) / "store"
        self.session_path = self.store_dir / "session.db"
        self.enterContext(mock.patch.object(backup, "WACLI_STORE_DIR", self.store_dir))
        self.enterContext(mock.patch.object(backup, "AUTO_SYNC", True))

    def open_paired_session(self):
        self.store_dir.mkdir()
        db = sqlite3.connect(self.session_path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
        db.execute("INSERT INTO whatsmeow_device VALUES (?)", (DEVICE_JID,))
        db.commit()
        return db

    def test_status_during_sync_write_does_not_report_an_outage(self):
        db = self.open_paired_session()
        # Hold sync's writer lock while the check reads committed WAL data.
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE whatsmeow_device SET jid = ?", ("15557654321@s.whatsapp.net",))

        with (
            mock.patch.object(backup, "sync_process", mock.Mock(poll=lambda: None)),
            mock.patch.object(
                backup, "sync_started_monotonic", time.monotonic() - 2 * backup.SYNC_STABLE_SECONDS
            ),
            mock.patch.object(backup, "mark_connection_failure") as failure,
            mock.patch.object(backup, "mark_connection_restored") as restored,
        ):
            status = backup.refresh_auth_status(force=True)
            backup.update_connection_health(status)

            self.assertIsNone(status["error"])
            self.assertTrue(status["authenticated"])
            self.assertEqual(status["linked_jid"], LINKED_JID)
            failure.assert_not_called()
            restored.assert_called_once_with()

    def test_missing_session_is_unauthenticated_without_creating_a_store(self):
        status = backup.refresh_auth_status(force=True)

        self.assertIsNone(status["error"])
        self.assertFalse(status["authenticated"])
        self.assertFalse(self.store_dir.exists())

    def test_revoked_session_is_unauthenticated_despite_a_stored_device(self):
        self.open_paired_session()
        (self.store_dir / "SESSION_REVOKED").write_text("reason=logged_out\n")

        status = backup.refresh_auth_status(force=True)

        self.assertIsNone(status["error"])
        self.assertFalse(status["authenticated"])

    def test_database_errors_remain_visible(self):
        self.store_dir.mkdir()
        self.session_path.write_text("not a SQLite database")

        status = backup.refresh_auth_status(force=True)

        self.assertFalse(status["authenticated"])
        self.assertIn("not a database", status["error"])


class AuthStatusErrorAlertTests(unittest.TestCase):
    def setUp(self):
        backup.auth_status_error_streak = 0
        self.addCleanup(setattr, backup, "auth_status_error_streak", 0)
        self.enterContext(mock.patch.object(backup, "AUTO_SYNC", True))
        self.enterContext(mock.patch.object(backup, "AUTH_STATUS_ERROR_THRESHOLD", 3))

    def check_once(self):
        backup.update_connection_health(backup.refresh_auth_status(force=True))

    def test_single_check_error_does_not_page(self):
        with (
            mock.patch.object(backup, "run_wacli_json", side_effect=RuntimeError("timed out")),
            mock.patch.object(backup, "mark_connection_failure") as failure,
        ):
            self.check_once()
            self.check_once()
            failure.assert_not_called()
            self.assertEqual(backup.auth_status_error_streak, 2)

            self.check_once()
            failure.assert_called_once()
            self.check_once()
            self.assertEqual(failure.call_count, 2)

    def test_successful_check_resets_the_error_streak(self):
        backup.auth_status_error_streak = 2
        with (
            mock.patch.object(backup, "run_wacli_json", return_value={"data": {"authenticated": True}}),
            mock.patch.object(backup, "mark_connection_failure") as failure,
        ):
            self.check_once()

        self.assertEqual(backup.auth_status_error_streak, 0)
        failure.assert_not_called()

    def test_unauthenticated_status_still_pages_immediately(self):
        with (
            mock.patch.object(backup, "run_wacli_json", return_value={"data": {"authenticated": False}}),
            mock.patch.object(backup, "mark_connection_failure") as failure,
        ):
            self.check_once()

        failure.assert_called_once()
