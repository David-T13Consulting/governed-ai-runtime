"""PV1 step 4: the rented-model provider adapter, against a stub server on loopback.

Contract (D135, PV1 brief): chosen only by explicit configuration; no fallback to another
provider, model, level or the local model; egress only to the configured host; a missing
key fails closed before any network call; the key never appears in any output.
"""
import json
import os
import socket
import threading
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from dbrain import adapter, provider

MODEL = "mistral-small-3.2-24b-instruct-2506"
# Split so the secret scan finds no credential-shaped literal in the repo.
SENTINEL = "zq" + "SENTINELxKEYx0123456789"


def ok_body(content="Answer. [E1]", model=MODEL, finish="stop"):
    return {"model": model,
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 8}}


class Stub:
    """A loopback HTTP server whose next reply each test sets. Counts calls and keeps
    the Authorization headers it saw."""

    def __init__(self):
        self.calls = 0
        self.auth = []
        self.paths = []
        self.reply = (200, {"Content-Type": "application/json"}, json.dumps(ok_body()).encode())
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.calls += 1
                stub.auth.append(self.headers.get("Authorization"))
                stub.paths.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                code, headers, body = stub.reply
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d/v1" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProviderBase(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.local = Stub()  # stands in for llama.cpp: must never be called
        self.addCleanup(self.stub.close)
        self.addCleanup(self.local.close)

    def make(self, key=SENTINEL, test_port=None, **kw):
        return provider.ProviderAdapter("scaleway", "L1", MODEL,
                                        test_port=test_port or self.stub.port,
                                        key_reader=lambda s, a: key, **kw)

    def assert_no_key(self, exc):
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(SENTINEL, repr(exc.args))


class HappyPathTest(ProviderBase):
    def test_answer_usage_and_identity_come_from_this_call(self):
        a = self.make()
        meta = a.generate_with_meta("q")
        self.assertEqual(meta["text"], "Answer. [E1]")
        self.assertEqual((meta["provider"], meta["level"], meta["model"]), ("scaleway", "L1", MODEL))
        self.assertEqual((meta["prompt_tokens"], meta["completion_tokens"]), (120, 8))
        self.assertEqual(self.stub.auth, ["Bearer " + SENTINEL])
        self.assertEqual(self.stub.paths, ["/v1/chat/completions"])
        text, secs = a.generate("q")
        self.assertEqual(text, "Answer. [E1]")
        self.assertEqual(self.local.calls, 0)

    def test_describe_names_level_provider_model_and_never_the_key(self):
        d = self.make().describe()
        self.assertEqual((d["provider"], d["level"], d["model"]), ("scaleway", "L1", MODEL))
        self.assertNotIn(SENTINEL, json.dumps(d))
        self.assertNotIn(SENTINEL, json.dumps(vars(self.make()), default=str))

    def test_a_failed_call_after_a_good_one_carries_no_usage(self):
        a = self.make()
        a.generate_with_meta("q")
        self.stub.reply = (503, {}, b"down")
        with self.assertRaises(adapter.ModelUnavailable):
            a.generate_with_meta("q")


class NoFallbackTest(ProviderBase):
    def test_provider_down_is_unavailable_and_nothing_else_is_called(self):
        a = self.make(test_port=free_port())
        with self.assertRaises(adapter.ModelUnavailable) as ctx:
            a.generate("q")
        self.assertEqual((self.stub.calls, self.local.calls), (0, 0))
        self.assert_no_key(ctx.exception)

    def test_http_errors_are_unavailable_with_status_only(self):
        for code in (401, 404, 429, 500, 503):
            self.stub.reply = (code, {}, ("body echoes " + SENTINEL).encode())
            with self.assertRaises(adapter.ModelUnavailable) as ctx:
                self.make().generate("q")
            self.assertIn("HTTP %d" % code, str(ctx.exception))
            self.assertNotIn("body echoes", str(ctx.exception))
            self.assert_no_key(ctx.exception)
        self.assertEqual(self.local.calls, 0)

    def test_every_redirect_is_refused_even_to_the_same_host(self):
        for target in ("http://example.com/x?k=" + SENTINEL, self.stub.url + "/chat/completions",
                       self.local.url + "/chat/completions"):  # a live second listener
            for code in (301, 302, 303, 307, 308):
                self.stub.reply = (code, {"Location": target}, b"")
                with self.assertRaises(provider.ProviderRedirect) as ctx:
                    self.make().generate("q")
                self.assertNotIn("example.com", str(ctx.exception))
                self.assert_no_key(ctx.exception)
        self.assertEqual(self.local.calls, 0)

    def test_a_reported_model_other_than_the_configured_one_is_refused(self):
        for reported in ("mistral-small-3.3-24b", "", None, 7, MODEL.upper()):
            body = ok_body()
            body["model"] = reported
            self.stub.reply = (200, {}, json.dumps(body).encode())
            with self.assertRaises(provider.ModelMismatch):
                self.make().generate("q")
        body = ok_body()
        del body["model"]
        self.stub.reply = (200, {}, json.dumps(body).encode())
        with self.assertRaises(provider.ModelMismatch):
            self.make().generate("q")

    def test_the_source_has_no_second_route(self):
        import pathlib
        src = pathlib.Path(provider.__file__).read_text()
        self.assertNotIn("LlamaCppAdapter(", src.split("def from_env")[0])
        self.assertEqual(src.count("_opener().open("), 1)


class KeyTest(ProviderBase):
    def test_absent_or_unusable_key_fails_closed_before_the_network(self):
        for bad in (None, "", "   ", "a\nb", "a\rb", "a\0b", "a\tb", 5):
            with self.assertRaises(provider.KeyUnavailable):
                self.make(key=bad).generate("q")
        self.assertEqual(self.stub.calls, 0)

    def test_a_key_reader_that_raises_fails_closed_without_its_message(self):
        def boom(service, account):
            raise RuntimeError("reader leaked " + SENTINEL)
        a = provider.ProviderAdapter("scaleway", "L1", MODEL, test_port=self.stub.port, key_reader=boom)
        with self.assertRaises(provider.KeyUnavailable) as ctx:
            a.generate("q")
        self.assert_no_key(ctx.exception)
        self.assertEqual(self.stub.calls, 0)

    def test_keychain_states_nonzero_exit_timeout_and_bad_bytes(self):
        import subprocess
        cases = [
            subprocess.CompletedProcess([], 44, stdout=SENTINEL.encode(), stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"\xff\xfe", stderr=b""),
        ]
        for done in cases:
            with mock.patch.object(provider.subprocess, "run", return_value=done):
                with self.assertRaises(provider.KeyUnavailable) as ctx:
                    provider.read_keychain("dbrain-scaleway", "dbrain")
                self.assert_no_key(ctx.exception)
        for exc in (subprocess.TimeoutExpired("security", 10, output=SENTINEL.encode()), OSError("x")):
            with mock.patch.object(provider.subprocess, "run", side_effect=exc):
                with self.assertRaises(provider.KeyUnavailable) as ctx:
                    provider.read_keychain("dbrain-scaleway", "dbrain")
                self.assert_no_key(ctx.exception)

    def test_keychain_call_uses_the_absolute_tool_and_an_argv_list(self):
        import subprocess
        done = subprocess.CompletedProcess([], 0, stdout=b"k\n", stderr=b"")
        with mock.patch.object(provider.subprocess, "run", return_value=done) as run:
            self.assertEqual(provider.read_keychain("svc", "acct"), "k\n")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/security", "find-generic-password", "-s", "svc", "-a", "acct", "-w"])
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("timeout", run.call_args.kwargs)

    def test_the_key_is_read_per_call_so_a_deleted_key_stops_the_next_call(self):
        keys = [SENTINEL, ""]
        a = provider.ProviderAdapter("scaleway", "L1", MODEL, test_port=self.stub.port,
                                     key_reader=lambda s, acc: keys.pop(0))
        a.generate("q")
        with self.assertRaises(provider.KeyUnavailable):
            a.generate("q")
        self.assertEqual(self.stub.calls, 1)

    def test_environment_keys_are_never_used(self):
        env = {k: SENTINEL for k in adapter.CLOUD_KEY_NAMES}
        env["SCALEWAY_API_KEY"] = SENTINEL
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(provider.KeyUnavailable):
                self.make(key=None).generate("q")
        self.assertEqual(self.stub.calls, 0)

    def test_server_text_that_could_carry_the_key_is_not_echoed(self):
        bodies = [
            {"error": {"message": SENTINEL}},
            ok_body(finish="length " + SENTINEL),
            ok_body(model=SENTINEL),
            ok_body(content="here it is: " + SENTINEL),
        ]
        for body in bodies:
            self.stub.reply = (200, {}, json.dumps(body).encode())
            with self.assertRaises(adapter.ModelUnavailable) as ctx:
                self.make().generate("q")
            self.assert_no_key(ctx.exception)


class MalformedTest(ProviderBase):
    def test_malformed_bodies_are_failures_not_answers(self):
        bodies = [b"not json", b"\xff\xfe", b"[]", b"{}", b'{"model": "%s"}' % MODEL.encode(),
                  json.dumps(ok_body(content=None)).encode(),
                  json.dumps(ok_body(content="")).encode(),
                  json.dumps(ok_body(content="<think>still going")).encode(),
                  json.dumps(ok_body(finish="length")).encode(),
                  json.dumps({"model": MODEL, "choices": "x"}).encode(),
                  b"[" * 100000]
        for body in bodies:
            self.stub.reply = (200, {}, body)
            with self.assertRaises(adapter.MalformedResponse, msg=body[:40]):
                self.make().generate("q")

    def test_an_oversized_body_is_refused(self):
        self.stub.reply = (200, {}, b" " * (provider.MAX_RESPONSE_BYTES + 10))
        with self.assertRaises(adapter.MalformedResponse):
            self.make().generate("q")


class ConstructionTest(ProviderBase):
    def test_unknown_provider_wrong_level_or_unregistered_model_is_refused(self):
        for args in (("openai", "L1", MODEL), ("scaleway", "L3", MODEL), ("scaleway", "L0", MODEL),
                     ("scaleway", "l1", MODEL), ("scaleway", "L1", "gpt-4o"), ("scaleway", "L1", "")):
            with self.assertRaises(provider.ProviderRefused, msg=args):
                provider.ProviderAdapter(*args)

    def test_registry_url_is_the_default_and_https(self):
        a = provider.ProviderAdapter("scaleway", "L1", MODEL)
        self.assertEqual(a.base_url, "https://api.scaleway.ai/v1")

    def test_no_url_is_accepted_from_a_caller(self):
        # D74 consult (21 Oct): reviews 1/2 and 2/2 broke URL parsing twice
        # ("[::1%30]" connected to ::10; "@127.0.0.1" to a resolved name). There is no
        # URL argument any more, and base_url cannot be set.
        for url in ("http://[::1%30]:8080/v1", "http://@127.0.0.1:8080/v1",
                    "https://api.example.com/v1"):
            with self.assertRaises(TypeError):
                provider.ProviderAdapter("scaleway", "L1", MODEL, base_url=url,
                                         key_reader=lambda s, a: SENTINEL)
        a = provider.ProviderAdapter("scaleway", "L1", MODEL)
        with self.assertRaises(AttributeError):
            a.base_url = "http://example.com/v1"

    def test_test_port_must_be_a_plain_int_in_range(self):
        for port in (0, -1, 65536, "8080", 8080.0, True, None.__class__, [8080]):
            with self.assertRaises(provider.ProviderRefused, msg=repr(port)):
                provider.ProviderAdapter("scaleway", "L1", MODEL, test_port=port,
                                         key_reader=lambda s, a: SENTINEL)

    def test_test_mode_never_reads_the_keychain(self):
        with mock.patch.object(provider.subprocess, "run") as run:
            for reader in (None, provider.read_keychain):
                with self.assertRaises(provider.ProviderRefused):
                    provider.ProviderAdapter("scaleway", "L1", MODEL, test_port=self.stub.port,
                                             key_reader=reader)
            run.assert_not_called()
        self.assertEqual(self.stub.calls, 0)

    def test_test_mode_sends_to_the_fixed_loopback_path_only(self):
        a = self.make()
        self.assertEqual(a.base_url, "http://127.0.0.1:%d/v1" % self.stub.port)
        a.generate("q")
        self.assertEqual(self.stub.paths, ["/v1/chat/completions"])
        self.assertEqual(self.stub.auth, ["Bearer " + SENTINEL])

    def test_production_endpoint_comes_from_the_registry_at_send_time(self):
        a = provider.ProviderAdapter("scaleway", "L1", MODEL)
        provider.REGISTRY["scaleway"]["base_url"], saved = "https://changed.invalid/v1", provider.REGISTRY["scaleway"]["base_url"]
        try:
            self.assertEqual(a.base_url, "https://changed.invalid/v1")
        finally:
            provider.REGISTRY["scaleway"]["base_url"] = saved
        self.assertEqual(a.base_url, "https://api.scaleway.ai/v1")

    def test_opener_has_no_proxy_and_refuses_redirects(self):
        # ProxyHandler({}) registers no proxy method, and stops build_opener adding the
        # default ProxyHandler that would read HTTP(S)_PROXY: so no proxy handler at all.
        handlers = provider._opener().handlers
        self.assertEqual([h for h in handlers if type(h).__name__ == "ProxyHandler"], [])
        self.assertTrue(any(isinstance(h, provider._RefuseAllRedirects) for h in handlers))

    def test_ambient_proxies_in_any_case_are_ignored(self):
        names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        with mock.patch.dict(os.environ, {n: "http://127.0.0.1:%d" % free_port() for n in names}):
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            handlers = provider._opener().handlers
            self.assertEqual([h for h in handlers if type(h).__name__ == "ProxyHandler"], [])
            # Control: the default opener would have picked these up.
            import urllib.request
            default = urllib.request.build_opener().handlers
            self.assertTrue([h for h in default if type(h).__name__ == "ProxyHandler"])


class FromEnvTest(unittest.TestCase):
    def test_unset_is_the_local_adapter_as_before(self):
        a = provider.from_env({})
        self.assertIsInstance(a, adapter.LlamaCppAdapter)

    def test_set_is_the_provider_never_the_local_one(self):
        a = provider.from_env({"DBRAIN_PROVIDER": "scaleway", "DBRAIN_LEVEL": "L1",
                               "DBRAIN_PROVIDER_MODEL": MODEL})
        self.assertIsInstance(a, provider.ProviderAdapter)

    def test_partial_or_wrong_configuration_is_refused_not_defaulted(self):
        for env in ({"DBRAIN_PROVIDER": "scaleway"},
                    {"DBRAIN_PROVIDER": "scaleway", "DBRAIN_LEVEL": "L1"},
                    {"DBRAIN_PROVIDER": "scaleway", "DBRAIN_PROVIDER_MODEL": MODEL},
                    {"DBRAIN_LEVEL": "L1"}, {"DBRAIN_PROVIDER_MODEL": MODEL},
                    {"DBRAIN_PROVIDER": "nope", "DBRAIN_LEVEL": "L1", "DBRAIN_PROVIDER_MODEL": MODEL}):
            with self.assertRaises(provider.ProviderRefused, msg=env):
                provider.from_env(env)


if __name__ == "__main__":
    unittest.main()
