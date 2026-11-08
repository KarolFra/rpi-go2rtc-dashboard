import copy
import json
import os
import smtplib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from satel_notifications import Notifications, send_smtp

SMTP = dict(smtp_host="smtp.example.test", smtp_username="test-user", smtp_password="private-test-secret",
            sender="sender@example.test", recipient="recipient@example.test")


def observation(gate="closed", active=(), armed=None, connected=True, instance="test"):
    return dict(connected=connected, instance_id=instance,
                inputs=[dict(id="brama_garazowa", state=gate)],
                zones={str(z): z in active for z in range(1,9)}, armed_zones=armed)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "notifications.sqlite3"
        self.time = 1000
        self.current = observation()
        self.sender = Mock()
        self.service = Notifications(self.path, lambda: copy.deepcopy(self.current), self.sender, lambda: self.time)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def enable(self, **settings):
        self.service.save(dict(SMTP, enabled=True, **settings))

    def count(self):
        return len(self.service.status()["recent"])

    def tick(self, seconds=0, **changes):
        self.time += seconds
        self.current.update(changes)
        self.service.observe_current()

    def test_defaults_do_not_queue_or_send_mail(self):
        self.current = observation(gate="not_closed", active=[1], armed=[1])
        self.tick()
        self.tick(3600)
        self.service.deliver_one()
        self.assertEqual(self.count(), 0)
        self.sender.assert_not_called()
        self.assertEqual(self.service.status()["settings"]["gate_minutes"], 20)

    def test_gate_exact_threshold_one_message_per_open_episode(self):
        self.enable()
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(1199)
        self.assertEqual(self.count(), 0)
        self.tick(1)
        self.assertEqual(self.count(), 1)
        self.service.deliver_one()
        self.tick(3600)
        self.service.deliver_one()
        self.sender.assert_called_once()
        self.current = observation()
        self.tick()
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(1200)
        self.assertEqual(self.count(), 2)

    def test_gate_closure_cancels_pending_mail_and_resets_timer(self):
        self.enable(gate_minutes=1)
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(60)
        self.current = observation()
        self.tick()
        self.service.deliver_one()
        self.sender.assert_not_called()
        self.assertEqual(self.service.status()["recent"][0]["status"], "cancelled")
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(59)
        self.assertEqual(self.count(), 1)

    def test_unknown_connection_and_new_instance_reset_gate_observation(self):
        self.enable(gate_minutes=1)
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(59)
        self.tick(1, connected=False)
        self.tick(600, connected=True)
        self.assertEqual(self.count(), 0)
        self.tick(59)
        self.assertEqual(self.count(), 0)
        self.tick(1, instance_id="restarted")
        self.assertEqual(self.count(), 0)
        self.tick(60)
        self.assertEqual(self.count(), 1)

    def test_saved_gate_episode_survives_worker_replacement_without_duplicate(self):
        self.enable(gate_minutes=1)
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(60)
        self.service.deliver_one()
        self.service.close()
        self.service = Notifications(self.path, lambda: self.current, self.sender, lambda: self.time)
        self.tick(3600)
        self.assertEqual(self.count(), 1)
        self.sender.assert_called_once()

    def test_sensor_requires_confirmed_arming_for_the_same_zone(self):
        self.enable()
        self.current = observation(active=[1])
        self.tick()
        self.assertEqual(self.count(), 0)
        self.tick(armed_zones=[])
        self.assertEqual(self.count(), 0)
        self.tick(armed_zones=[5])
        self.assertEqual(self.count(), 0)
        self.tick(armed_zones=[1])
        self.assertEqual(self.count(), 1)
        self.tick(3600)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.service.status()["recent"][0]["kind"], "sensor")

    def test_brief_sensor_transitions_and_cooldown(self):
        self.enable()
        self.current = observation(armed=[1])
        self.tick()
        self.tick(zones={"1":True})
        self.tick(zones={"1":False})
        self.tick(1, zones={"1":True})
        self.assertEqual(self.count(), 1)
        self.tick(zones={"1":False})
        self.tick(60, zones={"1":True})
        self.assertEqual(self.count(), 2)

    def test_disabled_sensor_rule_and_gate_contact_never_trigger_sensor_mail(self):
        self.enable(sensors_enabled=False)
        self.current = observation(active=[1,2], armed=[1,2])
        self.tick()
        self.assertEqual(self.count(), 0)
        self.service.save(dict(sensors_enabled=True))
        self.current = observation(active=[2], armed=[2])
        self.tick()
        self.assertEqual(self.count(), 0)

    def test_settings_password_is_write_only_and_blank_preserves_it(self):
        self.enable()
        self.assertNotIn(SMTP["smtp_password"], json.dumps(self.service.status()))
        self.assertTrue(self.service.status()["settings"]["password_set"])
        self.service.save(dict(gate_minutes=15, smtp_password=""))
        self.assertEqual(self.service.store.settings()["smtp_password"], SMTP["smtp_password"])
        self.service.save(dict(enabled=False, clear_password=True))
        self.assertFalse(self.service.status()["settings"]["password_set"])

    def test_invalid_settings_and_header_injection_are_rejected_atomically(self):
        for update in [dict(enabled=True), dict(gate_minutes=0), dict(gate_minutes=True),
                       dict(gate_minutes=1.5), dict(sensor_zones=[2]), dict(sensor_zones=[9]),
                       dict(smtp_security="plain"), dict(sender="x@y.test\r\nBcc:evil@test"),
                       dict(smtp_host="https://mail.test"), dict(enabled="true")]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.service.save(update)
        self.assertFalse(self.service.status()["settings"]["enabled"])

    def test_explicit_test_mail_can_be_sent_while_automatic_rules_are_disabled(self):
        self.service.save(SMTP)
        self.service.test()
        self.service.observe_current()
        self.service.deliver_one()
        self.sender.assert_called_once()
        self.assertEqual(self.service.status()["recent"][0]["status"], "sent")

    def test_smtp_failure_retries_durably_without_exposing_server_details(self):
        self.service.save(SMTP)
        self.service.test()
        self.sender.side_effect = smtplib.SMTPAuthenticationError(535, b"private-test-secret")
        self.service.deliver_one()
        self.assertEqual(self.sender.call_count, 1)
        message_id = self.sender.call_args.args[1]["message_id"]
        self.assertNotIn("private-test-secret", json.dumps(self.service.status()))
        self.service.deliver_one()
        self.assertEqual(self.sender.call_count, 1)
        self.service.close()
        self.service = Notifications(self.path, lambda: self.current, self.sender, lambda: self.time)
        self.time += 60
        self.sender.side_effect = None
        self.service.deliver_one()
        self.assertEqual(self.sender.call_args.args[1]["message_id"], message_id)
        self.assertEqual(self.service.status()["recent"][0]["status"], "sent")

    def test_disabling_notifications_cancels_unsent_automatic_messages(self):
        self.enable(gate_minutes=1)
        self.current = observation(gate="not_closed")
        self.tick()
        self.tick(60)
        self.service.save(dict(enabled=False))
        self.service.deliver_one()
        self.sender.assert_not_called()
        self.assertEqual(self.service.status()["recent"][0]["status"], "cancelled")

    def test_transport_uses_starttls_and_app_password(self):
        with patch("satel_notifications.smtplib.SMTP") as smtp:
            alert = dict(subject="Test", body="Test", message_id="<test@satel.local>")
            send_smtp(dict(SMTP, smtp_port=587, smtp_security="starttls"), alert)
            client = smtp.return_value.__enter__.return_value
            # send_smtp uses the SMTP object itself inside its context manager.
            smtp.return_value.starttls.assert_called_once()
            smtp.return_value.login.assert_called_once_with("test-user", "private-test-secret")
            smtp.return_value.send_message.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "Linux worker lock")
    def test_two_workers_share_one_sending_lock(self):
        sent = threading.Event()
        release = threading.Event()
        def slow_sender(*_):
            sent.set()
            release.wait(3)
        self.sender.side_effect = slow_sender
        self.service.save(SMTP)
        self.service.test()
        other = Notifications(self.path, lambda: self.current, Mock(), lambda: self.time)
        try:
            with patch.dict(os.environ, SATEL_NOTIFICATIONS_AUTOSTART="1"):
                self.service.start()
                self.assertTrue(sent.wait(3))
                other.start()
                time.sleep(0.1)
            self.sender.assert_called_once()
            other.sender.assert_not_called()
        finally:
            release.set()
            other.close()


if __name__ == "__main__":
    unittest.main()
