"""Durable email settings, alert episodes and a single background SMTP worker.

No mail is sent until the user supplies SMTP settings and enables notifications
or explicitly requests a test. Unknown arming state never means armed.
"""
import copy
import json
import os
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

DEFAULTS = dict(enabled=False, gate_enabled=True, gate_minutes=20,
                sensors_enabled=True, sensor_zones=[1, 3, 4], smtp_host="",
                smtp_port=587, smtp_security="starttls", smtp_username="",
                smtp_password="", sender="", recipient="")


def valid_address(value):
    return bool(value and "@" in value and not any(c.isspace() for c in value)
                and parseaddr(value)[1] == value and "\r" not in value and "\n" not in value)


def configured(settings):
    return bool(settings["smtp_host"] and valid_address(settings["sender"])
                and valid_address(settings["recipient"])
                and (not settings["smtp_username"] or settings["smtp_password"]))


def validate(current, patch):
    if not isinstance(patch, dict) or set(patch) - (set(DEFAULTS) | {"clear_password"}):
        raise ValueError("Nieprawidłowe ustawienia powiadomień.")
    result = copy.deepcopy(current)
    for key, value in patch.items():
        if key in ("enabled", "gate_enabled", "sensors_enabled", "clear_password"):
            if not isinstance(value, bool):
                raise ValueError("Przełączniki wymagają wartości logicznej.")
        elif key in ("gate_minutes", "smtp_port"):
            maximum = 1440 if key == "gate_minutes" else 65535
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Czas musi wynosić 1–1440 minut, a port 1–65535.")
        elif key == "sensor_zones":
            if (not isinstance(value, list) or any(type(z) is not int or z not in (1,3,4,5,6,7,8)
                                                 for z in value)):
                raise ValueError("Wybierz czujki Z1–Z8, z pominięciem bramy Z2.")
            value = sorted(set(value))
        else:
            if (not isinstance(value, str) or len(value) > (1024 if key == "smtp_password" else 254)
                    or any(c in value for c in "\r\n\0")):
                raise ValueError("Nieprawidłowe pole konfiguracji poczty.")
            if key != "smtp_password":
                value = value.strip()
        if key == "clear_password":
            continue
        if key == "smtp_password" and value == "":
            continue  # An empty password field preserves the saved secret.
        result[key] = value
    if patch.get("clear_password"):
        result["smtp_password"] = ""
    if result["smtp_security"] not in ("starttls", "ssl"):
        raise ValueError("Wybierz STARTTLS lub TLS/SSL.")
    if result["smtp_host"] and any(c in result["smtp_host"] for c in "/@ :"):
        raise ValueError("Podaj nazwę serwera SMTP, bez adresu URL i portu.")
    for key in ("sender", "recipient"):
        if result[key] and not valid_address(result[key]):
            raise ValueError("Podaj poprawny adres nadawcy i odbiorcy.")
    if result["enabled"] and not configured(result):
        raise ValueError("Przed włączeniem wysyłki uzupełnij serwer SMTP, nadawcę, odbiorcę i wymagane hasło.")
    return result


class NotificationStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the credential database without a window of world-readable access.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, episode TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, sent_at REAL, error TEXT,
                message_id TEXT NOT NULL);
        """)

    def get(self, key, default):
        with self.lock:
            row = self.db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else copy.deepcopy(default)

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value)))

    def settings(self):
        return self.get("settings", DEFAULTS)

    def save(self, patch):
        with self.lock:
            result = validate(self.settings(), patch)
            self.put("settings", result)

    def enqueue(self, kind, episode, subject, body, stamp):
        with self.lock, self.db:
            self.db.execute("""INSERT OR IGNORE INTO outbox
                (episode,kind,subject,body,created,message_id) VALUES (?,?,?,?,?,?)""",
                (episode, kind, subject, body, stamp, f"<{uuid.uuid4().hex}@satel.local>"))

    def cancel(self, kind):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE kind=? AND status='pending'", (kind,))

    def close(self):
        self.db.close()


def send_smtp(settings, alert):
    message = EmailMessage()
    message["From"], message["To"] = settings["sender"], settings["recipient"]
    message["Subject"], message["Message-ID"] = alert["subject"], alert["message_id"]
    message.set_content(alert["body"])
    context = ssl.create_default_context()
    if settings["smtp_security"] == "ssl":
        client = smtplib.SMTP_SSL(settings["smtp_host"], settings["smtp_port"], timeout=5, context=context)
    else:
        client = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=5)
    with client:
        if settings["smtp_security"] == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if settings["smtp_username"]:
            client.login(settings["smtp_username"], settings["smtp_password"])
        client.send_message(message)


class Notifications:
    def __init__(self, path, snapshot, sender=send_smtp, clock=time.time):
        self.path, self.snapshot, self.sender, self.clock = Path(path), snapshot, sender, clock
        self._store = None
        self._init_lock = threading.Lock()
        self._observe_lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._worker_error = None

    @property
    def store(self):
        with self._init_lock:
            if self._store is None:
                self._store = NotificationStore(self.path)
        return self._store

    def status(self):
        settings = self.store.settings()
        public = {k: v for k, v in settings.items() if k != "smtp_password"}
        public["password_set"] = bool(settings["smtp_password"])
        public["smtp_configured"] = configured(settings)
        snapshot = self.snapshot()
        armed = snapshot.get("armed_zones") if snapshot.get("connected") else None
        with self.store.lock:
            recent = [dict(r) for r in self.store.db.execute(
                "SELECT id,kind,subject,created,status,attempts,sent_at,error FROM outbox ORDER BY id DESC LIMIT 12")]
        runtime = self.store.get("runtime", {})
        gate_since = runtime.get("gate_since")
        return dict(settings=public, recent=recent, arming_available=armed is not None,
                    armed_zones=armed, worker_error=self._worker_error,
                    gate_elapsed_seconds=max(0, int(self.clock()-gate_since)) if gate_since else None)

    def save(self, patch):
        self.store.save(patch)

    def test(self):
        if not configured(self.store.settings()):
            raise ValueError("Najpierw uzupełnij konfigurację poczty i zapisz ustawienia.")
        self.store.enqueue("test", uuid.uuid4().hex, "SATEL — test powiadomień",
                           "To jest test konfiguracji powiadomień e-mail z Raspberry Pi.", self.clock())

    def observe(self, snapshot):
        with self._observe_lock:
            self._observe(snapshot)

    def observe_current(self):
        with self._observe_lock:
            self._observe(self.snapshot())

    def ingest(self):
        if os.environ.get("SATEL_NOTIFICATIONS_AUTOSTART", "1") != "0":
            # Catch brief sensor transitions in the publishing request. SMTP is
            # always asynchronous and cannot block the data source here.
            self.observe_current()
            self.start()

    def _observe(self, snapshot):
        settings, stamp = self.store.settings(), self.clock()
        runtime = self.store.get("runtime", {})
        previous = copy.deepcopy(runtime)
        identity = snapshot.get("instance_id")
        if identity != runtime.get("instance_id") or not snapshot.get("connected"):
            runtime = {"instance_id": identity}
            self.store.cancel("gate")
        if not settings["enabled"]:
            runtime = {"instance_id": identity}
            self.store.cancel("gate")
            self.store.cancel("sensor")
        else:
            inputs = {i["id"]: i["state"] for i in snapshot.get("inputs", [])}
            gate_open = snapshot.get("connected") and inputs.get("brama_garazowa") == "not_closed"
            if not settings["gate_enabled"] or not gate_open:
                for key in ("gate_since", "gate_episode", "gate_queued"):
                    runtime.pop(key, None)
                self.store.cancel("gate")
            else:
                if "gate_since" not in runtime:
                    runtime.update(gate_since=stamp, gate_episode=uuid.uuid4().hex, gate_queued=False)
                if stamp-runtime["gate_since"] >= settings["gate_minutes"]*60 and not runtime["gate_queued"]:
                    self.store.enqueue("gate", runtime["gate_episode"], "SATEL — brama garażowa niezamknięta",
                        f"Brama garażowa (Z2) pozostaje niezamknięta przez co najmniej {settings['gate_minutes']} minut.\n"
                        "Odczyt kontaktronu nie rozróżnia uchylenia od pełnego otwarcia.", stamp)
                    runtime["gate_queued"] = True
            armed = snapshot.get("armed_zones") if snapshot.get("connected") else None
            zones = dict(snapshot.get("zones", {}))
            # Compatibility with the existing named observations; raw Z1–Z8 take precedence.
            # Example mapping: adapt it to the actual installation.
            for key, zone in (("czujka_1",1), ("czujka_2",3), ("czujka_3",4)):
                if str(zone) not in zones and inputs.get(key) in ("normal", "violated"):
                    zones[str(zone)] = inputs[key] == "violated"
            active = {z for z in settings["sensor_zones"] if zones.get(str(z)) is True}
            last_active, last_armed = set(runtime.get("active", [])), set(runtime.get("armed", []))
            cooldown = runtime.get("cooldown", {})
            triggered = []
            if settings["sensors_enabled"] and armed is not None:
                for zone in active & set(armed):
                    if (zone not in last_active or zone not in last_armed) and stamp-cooldown.get(str(zone), -100000) >= 60:
                        triggered.append(zone)
                        cooldown[str(zone)] = stamp
                if triggered:
                    self.store.enqueue("sensor", uuid.uuid4().hex, "SATEL — naruszenie czujek podczas czuwania",
                        "Potwierdzono naruszenie czujek podczas czuwania ich stref: " +
                        ", ".join(f"Z{z}" for z in sorted(triggered)) + ".\nCzas odczytu UTC: " +
                        datetime.fromtimestamp(stamp, timezone.utc).isoformat(), stamp)
            if not settings["sensors_enabled"]:
                self.store.cancel("sensor")
            runtime.update(active=sorted(active), armed=sorted(armed or []), cooldown=cooldown)
        if runtime != previous:
            self.store.put("runtime", runtime)

    def deliver_one(self):
        settings, stamp = self.store.settings(), self.clock()
        if not configured(settings):
            return
        with self.store.lock:
            alert = self.store.db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 1", (stamp,)).fetchone()
        if alert is None:
            return
        if alert["kind"] != "test":
            rule_enabled = settings["sensors_enabled"] if alert["kind"] == "sensor" else settings["gate_enabled"]
            if not settings["enabled"] or not rule_enabled:
                self.store.cancel(alert["kind"])
                return
        try:
            self.sender(settings, dict(alert))
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE outbox SET status='sent',sent_at=?,error=NULL WHERE id=?", (stamp,alert["id"]))
        except Exception as error:
            if isinstance(error, smtplib.SMTPAuthenticationError):
                message = "Serwer SMTP odrzucił logowanie."
            elif isinstance(error, ssl.SSLError):
                message = "Nie udało się zweryfikować szyfrowanego połączenia SMTP."
            else:
                message = "Wysyłka nie powiodła się. Sprawdź serwer, port i połączenie z pocztą."
            attempts = alert["attempts"]+1
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE outbox SET attempts=?,status=?,next_attempt=?,error=? WHERE id=?",
                    (attempts, "failed" if attempts >= 8 else "pending", stamp+min(3600,60*2**(attempts-1)), message,alert["id"]))

    def start(self):
        if os.environ.get("SATEL_NOTIFICATIONS_AUTOSTART", "1") == "0":
            return
        with self._init_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="satel-email", daemon=True)
                self._thread.start()

    def _work(self):
        # A separate lock survives database transactions and prevents concurrent
        # workers sending twice during Gunicorn's graceful worker replacement.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".worker.lock").open("a") as lock:
            while not self._stop.is_set():
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    self._stop.wait(1)
            while not self._stop.is_set():
                try:
                    self.observe_current()
                    self.deliver_one()
                    self._worker_error = None
                except Exception:
                    self._worker_error = "Błąd rejestratora powiadomień. Sprawdź zapis na dysku."
                self._stop.wait(1)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(30)
        if self._store:
            self._store.close()
