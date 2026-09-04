import socket
import unittest

from dashboard_server import _pick_port


class PickPortTests(unittest.TestCase):
    def test_prefers_requested_port_when_free(self):
        port = _pick_port(8765)
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)

    def test_uses_next_free_port_when_requested_port_is_busy(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy_port = busy.getsockname()[1]
        busy.listen(1)

        chosen = _pick_port(busy_port)
        self.assertNotEqual(chosen, busy_port)
        busy.close()


if __name__ == "__main__":
    unittest.main()
