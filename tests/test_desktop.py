import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from aquahub_desktop.bridge import BridgeError, RpcClient, fixed_url, is_app_uri, portainer_url, agent_connection, portainer_credentials


class DesktopTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "rpc.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def reply(self, producer):
        def work():
            connection, _ = self.server.accept()
            with connection:
                raw = bytearray()
                while b"\n" not in raw:
                    raw.extend(connection.recv(4096))
                request = json.loads(raw)
                value = producer(request)
                connection.sendall(value if isinstance(value, bytes) else json.dumps(value).encode() + b"\n")
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 2)

    def test_real_unix_socket_round_trip_preserves_structured_data_and_null(self):
        self.reply(lambda request: {"id": request["id"], "result": {"error": None, "services": [{"id": "gcs"}]}})
        value = RpcClient(self.path).request("status.get")
        self.assertIsNone(value["error"])
        self.assertEqual(value["services"][0]["id"], "gcs")

    def test_identity_key_is_carried_without_becoming_a_shell_command(self):
        def producer(request):
            self.assertEqual(request["idempotency_key"], "click-operation-123")
            self.assertEqual(request["params"], {"service": "$(touch /unexpected)"})
            return {"id": request["id"], "error": {"code": "invalid_service", "message": "Composant inconnu."}}
        self.reply(producer)
        with self.assertRaises(BridgeError) as context:
            RpcClient(self.path).request("service.restart", {"service": "$(touch /unexpected)"}, idempotency_key="click-operation-123")
        self.assertEqual(context.exception.code, "invalid_service")

    def test_reply_cannot_resolve_another_request(self):
        self.reply(lambda request: {"id": "other-request", "result": True})
        with self.assertRaises(BridgeError) as context:
            RpcClient(self.path).request("status.get")
        self.assertEqual(context.exception.code, "agent_protocol")

    def test_truncated_json_is_reported_not_rendered_as_success(self):
        self.reply(lambda request: b'{"id":')
        with self.assertRaises(BridgeError) as context:
            RpcClient(self.path).request("status.get")
        self.assertEqual(context.exception.code, "agent_protocol")

    def test_reply_limit_is_enforced_during_receive(self):
        self.reply(lambda request: b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(BridgeError) as context:
            RpcClient(self.path).request("status.get")
        self.assertEqual(context.exception.code, "reply_too_large")

    def test_closed_agent_produces_recoverable_user_message(self):
        missing = Path(self.temp.name) / "missing.sock"
        with self.assertRaises(BridgeError) as context:
            RpcClient(missing).request("status.get")
        self.assertEqual(context.exception.code, "agent_unavailable")


class DesktopPolicyTests(unittest.TestCase):
    def test_native_portainer_credentials_require_strong_confirmed_input(self):
        password='AquaHub-example-only-123'
        self.assertEqual(portainer_credentials('admin',password,password),{'username':'admin','password':password})
        for args in (('admin','short','short'),('admin',password,'different'),('bad user',password,password),('admin','é'*40,'é'*40)):
            with self.assertRaises(BridgeError): portainer_credentials(*args)

    def test_installed_app_with_missing_system_socket_never_spawns_session_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            system=Path(temporary)/'system.sock'; runtime=Path(temporary)/'session.sock'
            self.assertEqual(agent_connection(True,runtime,system),(system,False))
            self.assertEqual(agent_connection(False,runtime,system),(runtime,True))

    def test_adopted_portainer_uses_published_loopback_port_only(self):
        for value in ('https://127.0.0.1:19443','http://localhost:9000','https://[::1]:9443/'):
            self.assertEqual(portainer_url(value),value)
        for value in (None,'https://example.com:9443','file:///etc/passwd','https://user:secret@localhost:9443','http://localhost:9000/?token=secret','http://localhost:0'):
            with self.assertRaises(BridgeError): portainer_url(value)

    def test_only_exact_local_entrypoint_has_bridge(self):
        index = Path("/usr/share/aquahub/frontend/index.html")
        self.assertTrue(is_app_uri(index.as_uri(), index))
        self.assertTrue(is_app_uri(index.as_uri() + "#settings", index))
        for uri in ("https://github.com/project/release", "file:///tmp/index.html", "file://evilhost/usr/share/aquahub/frontend/index.html", "about:blank", "javascript:alert(1)"):
            self.assertFalse(is_app_uri(uri, index), uri)

    def test_external_link_targets_are_closed(self):
        self.assertEqual(fixed_url("gcs"), "http://localhost:4200")
        self.assertEqual(fixed_url("fleet"), "http://localhost:8000")
        self.assertEqual(fixed_url("portainer"), "https://localhost:9443")
        for value in ("file:///etc/shadow", "javascript:alert(1)", ";sh", "https://evilhost"):
            with self.assertRaises(BridgeError):
                fixed_url(value)


if __name__ == "__main__":
    unittest.main()
