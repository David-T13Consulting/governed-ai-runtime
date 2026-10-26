"""P2.7 step C3: the server-log check fails closed. No server is started: the process,
the health check and the model are replaced; the log is a temporary file."""
import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path

from dbrain import outpath

_SPEC = importlib.util.spec_from_file_location(
    "server_log_check", Path(__file__).resolve().parent.parent / "experiments" / "server_log_check.py")
slc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slc)


class FakeServer:
    def __init__(self, argv, stdout=None, stderr=None):
        os.write(stdout.fileno(), b"... prompt eval time = 1 ms ...\n")

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class FakeHealth:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class EchoModel:
    def __init__(self, **kw):
        pass

    def generate(self, prompt, max_tokens=32):
        return prompt.rsplit(" ", 1)[-1], 0.0      # the answer marker, as asked


class FailClosedTest(unittest.TestCase):
    def setUp(self):
        saved = {name: getattr(slc, name) for name in ("port_free", "flags_match_launcher")}
        saved_mods = (slc.subprocess.Popen, slc.urllib.request.urlopen,
                      slc.adapter.LlamaCppAdapter, slc.outpath.open_for_write)

        def restore():
            for name, value in saved.items():
                setattr(slc, name, value)
            (slc.subprocess.Popen, slc.urllib.request.urlopen,
             slc.adapter.LlamaCppAdapter, slc.outpath.open_for_write) = saved_mods
        self.addCleanup(restore)
        slc.port_free = lambda port: True
        slc.flags_match_launcher = lambda: True
        slc.subprocess.Popen = FakeServer
        slc.urllib.request.urlopen = lambda url, timeout=None: FakeHealth()
        slc.adapter.LlamaCppAdapter = EchoModel

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = slc.main()
        return code, json.loads(out.getvalue())

    def test_the_faked_clean_run_passes(self):
        code, result = self.run_main()
        self.assertEqual((code, result["contains_question"], result["contains_answer"]),
                         (0, "no", "no"))
        self.assertNotIn("error", result)

    def test_a_guard_refusal_when_the_log_closes_fails_the_check(self):
        real = outpath.open_for_write

        @contextlib.contextmanager
        def refusing_on_close(path, **kw):
            with real(path, **kw) as fh:
                yield fh
            raise outpath.OutsideVaultError("refused at close")
        slc.outpath.open_for_write = refusing_on_close
        code, result = self.run_main()
        self.assertEqual((code, result["error"]), (1, "OutsideVaultError"))
        self.assertEqual((result["contains_question"], result["contains_answer"]), ("no", "no"))

    def test_a_preflight_error_is_reported_by_type_and_fails(self):
        def broken():
            raise ValueError("launcher line not found: secret text")
        slc.flags_match_launcher = broken
        code, result = self.run_main()
        self.assertEqual((code, result["error"]), (1, "ValueError"))
        self.assertNotIn("secret text", json.dumps(result))

    def test_a_server_that_does_not_stop_in_time_fails_and_is_reaped(self):
        calls = []

        class Stubborn(FakeServer):
            def wait(self, timeout=None):
                calls.append("wait")
                if len(calls) == 1:
                    raise slc.subprocess.TimeoutExpired("llama-server", timeout)
                return 0

            def kill(self):
                calls.append("kill")
        slc.subprocess.Popen = Stubborn
        code, result = self.run_main()
        self.assertEqual((code, result["error"]), (1, "TimeoutExpired"))
        self.assertEqual(calls, ["wait", "kill", "wait"])

    def test_a_failure_to_reap_after_kill_fails_the_check(self):
        class Unreapable(FakeServer):
            def wait(self, timeout=None):
                raise ValueError("cannot wait")

            def kill(self):
                pass
        slc.subprocess.Popen = Unreapable
        code, result = self.run_main()
        self.assertEqual((code, result["error"]), (1, "ValueError"))

    def test_flags_that_differ_from_the_launcher_fail(self):
        slc.flags_match_launcher = lambda: False
        code, result = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(result["server_started"])


if __name__ == "__main__":
    unittest.main()
