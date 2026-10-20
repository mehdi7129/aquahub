from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"agent"))
from aquahub_agent.core import Application, Config
from aquahub_agent.security import ApiError, AuthPolicy
from aquahub_agent.server import UnixServer, MAX_REQUEST_BYTES
from test_core import FakeDocker


class UnixTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.app = Application(Config(self.base/"rpc.sock",self.base/"state",no_poll=True),docker=FakeDocker(),policy=AuthPolicy(os.getuid(),privileged=False))
        self.server = UnixServer(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":.01},daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.directory.cleanup()

    def exchange(self, data):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.app.config.socket_path))
            client.sendall(data)
            with client.makefile("rb") as stream:
                return json.loads(stream.readline())

    def test_real_socket_private_and_json_round_trip(self):
        self.assertEqual(self.app.config.socket_path.stat().st_mode&0o777,0o600)
        result = self.exchange(b'{"id":"one","method":"app.version","params":{}}\n')
        self.assertEqual(result["id"],"one")
        self.assertEqual(result["result"]["protocol"],1)

    def test_peer_uid_comes_from_kernel_not_json(self):
        self.app.policy.allowed_uid = os.getuid()+1
        result = self.exchange(json.dumps({"id":1,"method":"app.version","uid":os.getuid()+1}).encode()+b"\n")
        self.assertEqual(result["error"]["code"],"unauthorized")

    def test_malformed_unknown_and_nonfinite_requests_are_rejected(self):
        for data,code in ((b"{no}\n","invalid_json"),(b"[]\n","invalid_request"),(b'{"id":1,"method":"system.shell"}\n',"unknown_method"),(b'{"id":1,"method":"app.version","uid":0}\n',"invalid_request"),(b'{"id":NaN,"method":"app.version"}\n',"invalid_json")):
            with self.subTest(data=data):
                self.assertEqual(self.exchange(data)["error"]["code"],code)

    def test_size_limit_rejects_before_parsing(self):
        result = self.exchange(b"x"*(MAX_REQUEST_BYTES+1)+b"\n")
        self.assertEqual(result["error"]["code"],"request_too_large")

    def test_rpc_idempotency_key_is_passed_to_jobs(self):
        request = {"id":1,"method":"service.restart","params":{"service":"gcs"},"idempotency_key":"same-click"}
        first = self.exchange(json.dumps(request).encode()+b"\n")
        second = self.exchange(json.dumps(request).encode()+b"\n")
        self.assertEqual(first["result"]["id"],second["result"]["id"])

    def test_active_socket_or_plain_file_is_never_silently_replaced(self):
        with self.assertRaises(ApiError) as caught:
            UnixServer(self.app)
        self.assertEqual(caught.exception.code,"already_running")
        original = self.app.config.socket_path
        self.app.config.socket_path = self.base/"valuable.txt"
        self.app.config.socket_path.write_text("keep")
        try:
            with self.assertRaises(ApiError):
                UnixServer(self.app)
            self.assertEqual(self.app.config.socket_path.read_text(),"keep")
        finally:
            self.app.config.socket_path = original


if __name__=="__main__":
    unittest.main()
