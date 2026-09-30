import json
import socket
import unittest
import urllib.error
import urllib.request

from ttb.status_server import StatusServer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StatusServerTests(unittest.TestCase):
    def test_health_and_ready_endpoints(self):
        port = _free_port()
        state = {"state": "healthy", "symbol": "TMFJ6"}
        server = StatusServer("127.0.0.1", port, lambda: dict(state))
        server.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ready"])

            state["state"] = "manual_reconciliation_required"
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2)
            self.assertEqual(caught.exception.code, 503)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
