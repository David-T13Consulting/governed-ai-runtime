import os
import socket
import unittest

from dbrain import adapter


class NoCloudFallbackTest(unittest.TestCase):
    def test_non_loopback_endpoints_are_refused_at_construction(self):
        for endpoint in (
            "http://api.openai.com",
            "https://api.anthropic.com/v1",
            "http://10.0.0.5:8080",
            "http://192.168.1.10:11434",
            "http://example.com",
        ):
            with self.assertRaises(adapter.NonLocalEndpoint, msg=endpoint):
                adapter.LlamaCppAdapter(endpoint=endpoint)

    def test_loopback_endpoints_are_accepted(self):
        for endpoint in ("http://127.0.0.1:18087", "http://localhost:8080", "http://[::1]:9000"):
            self.assertTrue(adapter.LlamaCppAdapter(endpoint=endpoint).endpoint)

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_model_down_fails_loudly_and_does_not_reroute(self):
        a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % self._free_port())
        with self.assertRaises(adapter.ModelUnavailable) as ctx:
            a.generate("anything")
        self.assertIn("does not call anything else", str(ctx.exception))

    def test_cloud_keys_present_do_not_produce_an_answer(self):
        """With provider keys set, a dead local model still fails - no silent fallback."""
        # Split so the secret scan finds no credential-shaped literal in the repo.
        fake = "sk" + "-test-not-a-real-key"
        keys = {k: fake for k in adapter.CLOUD_KEY_NAMES}
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update(keys)
        try:
            self.assertTrue(adapter.cloud_keys_present())
            a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % self._free_port())
            with self.assertRaises(adapter.ModelUnavailable):
                a.generate("anything")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ambient_proxy_cannot_move_the_request_off_the_machine(self):
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy")}
        os.environ.update({"HTTP_PROXY": "http://evil.example:3128",
                           "HTTPS_PROXY": "http://evil.example:3128",
                           "http_proxy": "http://evil.example:3128"})
        try:
            a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % self._free_port())
            with self.assertRaises(adapter.ModelUnavailable):
                a.generate("anything")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_remote_host_appears_in_the_adapter_source(self):
        import pathlib
        src = pathlib.Path(adapter.__file__).read_text()
        body = src.split("CLOUD_KEY_NAMES")[0] + src.split(")\n", 1)[-1]
        for host in ("api.openai.com", "api.anthropic.com", "googleapis.com", "huggingface.co"):
            self.assertNotIn(host, body)


if __name__ == "__main__":
    unittest.main()


class RedirectTest(unittest.TestCase):
    """A real local server that answers with a 302 to an external URL. The adapter must
    refuse the hop rather than follow it off the machine."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://example.com/evidence")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Redirector)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_local_302_to_an_external_url_is_refused(self):
        a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % self.port)
        with self.assertRaises(adapter.RedirectRefused) as ctx:
            a.generate("anything")
        self.assertIn("example.com", str(ctx.exception))
        self.assertIn("no remote path", str(ctx.exception))

    def test_redirect_refusal_is_a_model_failure_not_a_silent_answer(self):
        a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % self.port)
        # RedirectRefused subclasses ModelUnavailable, so callers that handle the
        # model-down case cannot accidentally treat a redirect as a successful answer.
        self.assertTrue(issubclass(adapter.RedirectRefused, adapter.ModelUnavailable))
        with self.assertRaises(adapter.ModelUnavailable):
            a.generate("anything")


class ResolvedAddressTest(unittest.TestCase):
    def test_a_name_that_resolves_off_box_is_refused(self):
        with self.assertRaises(adapter.NonLocalEndpoint):
            adapter.assert_loopback("example.com", 80)

    def test_loopback_names_resolve_only_to_loopback(self):
        for name in ("127.0.0.1", "localhost"):
            for addr in adapter.assert_loopback(name, 80):
                self.assertTrue(adapter._is_loopback_address(addr), addr)

    def test_adapter_records_the_addresses_it_verified(self):
        a = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:18087")
        self.assertEqual(a.describe()["resolved_addresses"], ["127.0.0.1"])
