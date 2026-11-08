import threading
import time
import unittest

try:
    from satel_state import SatelStateStore
except ModuleNotFoundError:  # Allows discovery from the repository root.
    from deploy.satel_state import SatelStateStore


class SatelStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SatelStateStore(history_size=4)

    def test_initial_state_is_explicitly_disconnected_and_unknown(self):
        snapshot = self.store.snapshot()
        self.assertFalse(snapshot["connected"])
        self.assertEqual(snapshot["connection_state"], "disconnected")
        self.assertEqual(len(snapshot["inputs"]), 6)
        self.assertTrue(all(item["state"] == "unknown" for item in snapshot["inputs"]))

    def test_publish_input_wakes_waiters(self):
        result = []

        def wait():
            result.append(self.store.wait_for_change(0, timeout=1))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.02)
        self.store.publish_input("czujka_1", "violated")
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["revision"], 1)
        self.assertEqual(result[0]["events"][0]["state"], "violated")

    def test_wait_for_specific_gate_state(self):
        result = []

        def wait():
            result.append(
                self.store.wait_for_input("brama_garazowa", {"closed"}, timeout=1)
            )

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.02)
        self.store.publish_input("brama_garazowa", "not_closed")
        self.store.publish_input("brama_garazowa", "closed")
        thread.join(1)

        self.assertEqual(result[0]["state"], "closed")

    def test_invalid_sensor_state_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.publish_input("czujka_1", "open")

    def test_client_revision_ahead_returns_current_snapshot(self):
        started = time.monotonic()
        snapshot = self.store.wait_for_change(99, timeout=0.5)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(snapshot["revision"], 0)

    def test_instance_id_distinguishes_process_restarts(self):
        other_store = SatelStateStore()
        self.assertNotEqual(
            self.store.snapshot()["instance_id"],
            other_store.snapshot()["instance_id"],
        )


if __name__ == "__main__":
    unittest.main()
