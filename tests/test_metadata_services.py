import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from metadata_services import read_metadata, update_metadata, write_metadata


def _increment_metadata(path: str, iterations: int, start_event) -> None:
    start_event.wait()
    for _ in range(iterations):
        def increment(data):
            current = int(data.get("count", 0))
            time.sleep(0.002)
            data["count"] = current + 1

        update_metadata(Path(path), increment)


class MetadataConcurrencyTests(unittest.TestCase):
    def test_concurrent_process_updates_do_not_lose_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            write_metadata(path, {"count": 0})

            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            workers = [
                context.Process(
                    target=_increment_metadata,
                    args=(str(path), 12, start_event),
                )
                for _ in range(3)
            ]
            for worker in workers:
                worker.start()
            start_event.set()
            for worker in workers:
                worker.join(20)

            self.assertEqual([worker.exitcode for worker in workers], [0, 0, 0])
            self.assertEqual(read_metadata(path)["count"], 36)


if __name__ == "__main__":
    unittest.main()
