"""Offline example with fictional observations; does not contact a panel."""
import json
import tempfile
from pathlib import Path

from satel_state import SatelStateStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = SatelStateStore(history_path=Path(directory) / "events.sqlite3")
        try:
            store.set_connection(True, "Przykładowe dane", source="demo")
            store.publish_input("czujka_1", "violated", source="demo")
            store.publish_input("brama_garazowa", "closed", source="demo")
            print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
