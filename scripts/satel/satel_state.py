from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from satel_history import SatelHistory


SATEL_INPUTS = {
    "czujka_1": {"label": "Czujka 1 (przykład)", "kind": "sensor"},
    "czujka_2": {"label": "Czujka 2 (przykład)", "kind": "sensor"},
    "czujka_3": {"label": "Czujka 3 (przykład)", "kind": "sensor"},
    "czujka_4": {"label": "Czujka 4", "kind": "sensor"},
    "czujka_5": {"label": "Czujka 5", "kind": "sensor"},
    "brama_garazowa": {"label": "Brama garażowa (przykład)", "kind": "gate"},
}

VALID_STATES = {
    "sensor": {"unknown", "normal", "violated"},
    "gate": {"unknown", "closed", "not_closed"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SatelStateStore:
    """Thread-safe, transport-independent state for a read-only Satel reader."""

    def __init__(self, history_size: int = 100, history_path=None,
                 stale_after: float = 35.0, clock=time.monotonic):
        self._condition = threading.Condition(threading.RLock())
        self._instance_id = uuid.uuid4().hex
        self._revision = 0
        self._connected = False
        self._source = None
        self._zones = {str(zone): None for zone in range(1, 9)}
        self._armed_zones = None
        self._connection_state = "disconnected"
        self._connection_message = "Centrala SATEL nie jest jeszcze podłączona."
        self._updated_at: str | None = None
        self._inputs = {
            input_id: {
                "id": input_id,
                "label": config["label"],
                "kind": config["kind"],
                "state": "unknown",
                "updated_at": None,
            }
            for input_id, config in SATEL_INPUTS.items()
        }
        self._events: deque[dict] = deque(maxlen=history_size)
        self._history = SatelHistory(history_path) if history_path is not None else None
        self._storage_error = None
        self._last_received_at = self._history.get("last_received_at") if self._history else None
        self._last_received_mono = None
        self._clock = clock
        self._stale_after = stale_after
        self._stop = threading.Event()
        self._watchdog = None
        if self._history:
            self._events.extend(self._history.page(history_size)["events"])
            self._append_event_locked({
                "type": "connection", "state": "disconnected", "time": utc_now(),
                "message": "Start rejestratora. Brak potwierdzonego odczytu; "
                           "stany pozostają nieznane do otrzymania danych.",
            })
        self._connection_message = "Brak aktualnych danych z centrali. Rejestracja wejść wymaga działającego źródła."

    def start_watchdog(self):
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch, name="satel-watchdog", daemon=True)
            self._watchdog.start()

    def _watch(self):
        while not self._stop.wait(1):
            try:
                self.check_freshness()
            except Exception:
                logging.exception("SATEL watchdog: failed to record loss of data")

    def close(self):
        self._stop.set()
        if self._watchdog:
            self._watchdog.join(3)
        if self._history:
            self._history.close()

    def check_freshness(self):
        with self._condition:
            if (self._connected and self._last_received_mono is not None
                    and self._clock() - self._last_received_mono >= self._stale_after):
                self.mark_disconnected(
                    f"Brak danych od obserwatora przez {self._stale_after:g} s. "
                    "Bieżące stany nieznane; zdarzenia z przerwy nie są rejestrowane.")

    def history_page(self, limit=100, before=None):
        if self._history:
            return self._history.page(limit, before)
        with self._condition:
            return {"events": copy.deepcopy(list(self._events))[:limit], "next_before": None}

    def snapshot(self) -> dict:
        self.check_freshness()
        with self._condition:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        return {
            "instance_id": self._instance_id,
            "revision": self._revision,
            "connected": self._connected,
            "connection_state": self._connection_state,
            "connection_message": self._connection_message,
            "updated_at": self._updated_at,
            "last_received_at": self._last_received_at,
            "heartbeat_timeout_seconds": self._stale_after,
            "recording": self._history is not None and self._storage_error is None,
            "storage_error": self._storage_error,
            "source": self._source,
            "zones": dict(self._zones),
            "armed_zones": copy.deepcopy(self._armed_zones),
            "inputs": copy.deepcopy(list(self._inputs.values())),
            "events": copy.deepcopy(list(self._events)),
        }

    def wait_for_change(
        self,
        since_revision: int,
        timeout: float = 25.0,
        since_instance: str | None = None,
    ) -> dict:
        timeout = max(0.0, min(float(timeout), 30.0))
        with self._condition:
            self.check_freshness()
            self._condition.wait_for(
                lambda: self._revision != since_revision
                or (
                    since_instance is not None
                    and since_instance != self._instance_id
                ),
                timeout=timeout,
            )
            self.check_freshness()
            return self._snapshot_locked()

    def wait_for_input(
        self,
        input_id: str,
        expected_states: set[str],
        timeout: float | None = None,
    ) -> dict | None:
        """Wait for a normalized input state; intended for future automations."""
        if input_id not in self._inputs:
            raise KeyError(input_id)

        with self._condition:
            matched = self._condition.wait_for(
                lambda: self._inputs[input_id]["state"] in expected_states,
                timeout=timeout,
            )
            return copy.deepcopy(self._inputs[input_id]) if matched else None

    def set_connection(self, connected: bool, message: str, source=None) -> dict:
        now = utc_now()
        with self._condition:
            state = "connected" if connected else "disconnected"
            if connected:
                if self._history:
                    try:
                        self._history.set("last_received_at", now)
                    except Exception as error:
                        self._storage_error = str(error)
                        self._bump_locked()
                        raise
                self._last_received_at = now
                self._last_received_mono = self._clock()
            if (
                self._connected == connected
                and self._connection_state == state
                and self._connection_message == message
                and (source is None or self._source == source)
            ):
                return self._snapshot_locked()

            self._append_event_locked(
                {
                    "type": "connection",
                    "state": state,
                    "message": message,
                    "time": now,
                }
            )
            self._connected = connected
            if source is not None:
                self._source = source
            self._connection_state = state
            self._connection_message = message
            self._updated_at = now
            self._bump_locked()
            return self._snapshot_locked()

    def publish_observation(self, zones=None, armed_zones=None):
        """Arming requires explicit, trusted per-zone data; omission means unknown."""
        with self._condition:
            changed = self._zones != (zones or self._zones) or self._armed_zones != armed_zones
            if zones is not None:
                self._zones = dict(zones)
            self._armed_zones = copy.deepcopy(armed_zones)
            if changed:
                self._bump_locked()

    def publish_input(self, input_id: str, state: str, source: str = "local-adapter") -> dict:
        if input_id not in self._inputs:
            raise KeyError(input_id)

        input_state = self._inputs[input_id]
        if state not in VALID_STATES[input_state["kind"]]:
            raise ValueError(f"Invalid state {state!r} for {input_state['kind']}")

        now = utc_now()
        with self._condition:
            input_state = self._inputs[input_id]
            if input_state["state"] == state:
                return copy.deepcopy(input_state)

            previous_state = input_state["state"]
            self._append_event_locked(
                {
                    "type": "input",
                    "input_id": input_id,
                    "label": input_state["label"],
                    "previous_state": previous_state,
                    "state": state,
                    "source": source,
                    "time": now,
                }
            )
            input_state["state"] = state
            input_state["updated_at"] = now
            self._updated_at = now
            self._bump_locked()
            return copy.deepcopy(input_state)

    def mark_disconnected(self, message: str) -> dict:
        now = utc_now()
        with self._condition:
            changed = self._connected or self._connection_message != message
            changed = changed or any(item["state"] != "unknown" for item in self._inputs.values())
            if changed:
                event = {"type": "connection", "state": "disconnected",
                         "message": message, "time": now}
                try:
                    self._append_event_locked(event)
                except Exception:
                    # A full/failed disk must never leave stale inputs marked healthy.
                    logging.exception("SATEL: could not persist disconnect")
                    self._events.appendleft(event)
            self._connected = False
            self._zones = {str(zone): None for zone in range(1, 9)}
            self._armed_zones = None
            self._connection_state = "disconnected"
            self._connection_message = message
            for input_state in self._inputs.values():
                if input_state["state"] != "unknown":
                    changed = True
                    input_state["state"] = "unknown"
                    input_state["updated_at"] = now
            if changed:
                self._updated_at = now
                self._bump_locked()
            return self._snapshot_locked()

    def _append_event_locked(self, event: dict) -> None:
        if self._history:
            try:
                event = self._history.append(event)
                self._storage_error = None
            except Exception as error:
                self._storage_error = str(error)
                raise
        self._events.appendleft(event)

    def _bump_locked(self) -> None:
        self._revision += 1
        self._condition.notify_all()
