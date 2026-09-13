import threading
import time
import manifest as M


def test_host_gate_serialises_and_spaces_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path))
    stamps = []

    def hit():
        with M.host_gate("example.org", min_interval=0.2):
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.19, gaps
