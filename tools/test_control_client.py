import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import control_client


def make_handler(state, received):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            received.append({
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("X-Control-Token"),
                "content_type": self.headers.get("Content-Type"),
                "body": json.loads(raw.decode("utf-8")) if raw else None,
            })
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle

    return H


class TestControlClient(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.state = {"code": 200, "body": {"ok": True}}
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state, self.received))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._orig_port = control_client.http_port
        self._orig_token = control_client.token
        control_client.http_port = lambda: self.port
        control_client.token = lambda: "tok123"

    def tearDown(self):
        control_client.http_port = self._orig_port
        control_client.token = self._orig_token
        self.server.shutdown()

    def test_health_sends_token(self):
        out = control_client.health()
        self.assertTrue(out["ok"])
        self.assertEqual("tok123", self.received[0]["token"])

    def test_cmd_posts_json(self):
        control_client.cmd("//set stone")
        req = self.received[0]
        self.assertEqual("/command", req["path"])
        self.assertEqual({"command": "//set stone"}, req["body"])
        self.assertTrue(req["content_type"].startswith("application/json"))

    def test_chat_posts_json(self):
        control_client.chat("hi")
        self.assertEqual({"text": "hi"}, self.received[0]["body"])

    def test_close_posts_to_screen_close(self):
        control_client.close()
        req = self.received[0]
        self.assertEqual("POST", req["method"])
        self.assertEqual("/screen/close", req["path"])
        self.assertIsNone(req["body"])

    def test_http_error_raises_control_error(self):
        self.state["code"] = 409
        self.state["body"] = {"ok": False, "error": "not in game"}
        with self.assertRaises(control_client.ControlError) as cm:
            control_client.cmd("/say hi")
        self.assertIn("409", str(cm.exception))
        self.assertIn("not in game", str(cm.exception))

    def test_connection_refused_raises_control_error(self):
        control_client.http_port = lambda: 1  # 不可连端口
        with self.assertRaises(control_client.ControlError):
            control_client.state()


if __name__ == "__main__":
    unittest.main()
