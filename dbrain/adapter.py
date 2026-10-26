"""Model adapters. The runtime and model are swappable; the no-cloud rule is not.

P2.2 "Done when" 4: the adapter talks only to a local endpoint, answers with provider
keys absent, and fails loudly when the local model is down. It never routes elsewhere.
There is deliberately no fallback branch in this file - look for one before trusting it.
"""
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx from the local model is not followed.

    Following one would let a local endpoint hand the service an external URL, which is
    exactly the cloud path this module is supposed not to have.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRefused(
            "local model returned a %s redirect to %r. Not following it: this service "
            "has no remote path." % (code, newurl)
        )


def _is_loopback_address(addr):
    import ipaddress

    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def parts_host_header(endpoint):
    from urllib.parse import urlsplit

    parts = urlsplit(endpoint)
    return parts.netloc


def pin_to_address(endpoint, address):
    """Rewrite the endpoint to a numeric loopback address, keeping the port."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(endpoint)
    literal = "[%s]" % address if ":" in address else address
    netloc = "%s:%d" % (literal, parts.port) if parts.port else literal
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")


def assert_loopback(host, port):
    """Every address the host resolves to must be loopback.

    Checking the *name* is not enough: a name in LOOPBACK_NAMES could resolve off-box,
    and a name outside it could legitimately resolve to 127.0.0.1. The resolved
    addresses are what the socket will actually connect to.
    """
    try:
        infos = socket.getaddrinfo(host, port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NonLocalEndpoint("cannot resolve model host %r: %s" % (host, exc))
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise NonLocalEndpoint("model host %r resolved to nothing" % host)
    off_box = sorted(a for a in addrs if not _is_loopback_address(a))
    if off_box:
        raise NonLocalEndpoint(
            "model host %r resolves off-box to %s. Refused." % (host, ", ".join(off_box))
        )
    return sorted(addrs)

# Checked only so the service can state it is ignoring them. Never read for a value.
CLOUD_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "HF_TOKEN",
)


class ModelUnavailable(Exception):
    """The local model did not answer. This is a loud stop, never a reroute."""


class NonLocalEndpoint(Exception):
    """Refused: the endpoint is not loopback, or does not resolve to loopback."""


class RedirectRefused(ModelUnavailable):
    """The local endpoint tried to redirect us elsewhere. A loud stop, not a reroute."""


class MalformedResponse(ModelUnavailable):
    """The local model answered, but not with a usable, complete answer. A loud stop.

    An error object, a missing or non-string content, a truncated generation or an
    unterminated <think> block used to become an empty or partial answer that was then
    *graded* like a real one (round C finding 3). None of them is an answer.
    """


class ModelAdapter:
    name = "abstract"

    def generate(self, prompt, max_tokens=320):
        raise NotImplementedError

    def describe(self):
        raise NotImplementedError


# Qwen3 thinks aloud unless told not to; "/no_think" in the system turn switches it off,
# as in the 1 Oct benchmark. The answer rules themselves stay in the user turn (PROMPT).
SYSTEM = "You have no tools and cannot change files. /no_think"

# Everything about the request that can change an answer, besides the question and the
# evidence. Hashed into the run's prompt version, so a change of endpoint, system turn
# or sampling shows up as a new version (round C finding 4: two different request shapes
# shared one version because only the user template was hashed).
CHAT_PATH = "/v1/chat/completions"
GEN_PARAMS = {"temperature": 0.0, "seed": 42, "cache_prompt": False, "stream": False}


def request_signature(user_template, max_tokens=320):
    import hashlib

    shape = {
        "path": CHAT_PATH,
        "system": SYSTEM,
        "user_template": user_template,
        "params": dict(GEN_PARAMS, max_tokens=max_tokens),
    }
    blob = json.dumps(shape, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def chat_content(payload):
    """The answer text of a complete chat completion, or MalformedResponse."""
    if not isinstance(payload, dict):
        raise MalformedResponse("local model returned %s, not a JSON object" % type(payload).__name__)
    if "error" in payload:
        raise MalformedResponse("local model returned an error: %r" % (payload["error"],))
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise MalformedResponse("local model response has no choices[0].message.content")
    if not isinstance(content, str):
        raise MalformedResponse("local model content is %s, not text" % type(content).__name__)
    finish = choice.get("finish_reason")
    if finish != "stop":
        # "length" means the answer was cut at max_tokens: grading half an answer would
        # measure the cap, not the model.
        raise MalformedResponse("generation did not finish (finish_reason=%r)" % (finish,))
    return content


def strip_think(text):
    """Drop a <think>...</think> block. With thinking off, Qwen3 still emits an empty one.

    An opening tag with no close means the model was still thinking when it stopped;
    passing that through would grade its reasoning as the answer.
    """
    import re

    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in stripped:
        raise MalformedResponse("unterminated <think> block in the model's answer")
    return stripped.strip()


def answer_text(payload):
    """The visible answer of a complete chat completion, or MalformedResponse.

    Round D finding 4: an empty, whitespace-only or thinking-only completion used to come
    back as "" and be graded `incomplete`, as if the model had answered badly. It did not
    answer at all.
    """
    text = strip_think(chat_content(payload))
    if not text:
        raise MalformedResponse("the model returned no answer text (empty, whitespace or only a <think> block)")
    return text


class LlamaCppAdapter(ModelAdapter):
    """llama.cpp server over HTTP on loopback only.

    Provisional default runtime per P2.2: llama.cpp was the runtime benchmarked on
    1 Oct, so it is what this adapter targets. T9 is NOT closed by this choice.
    """

    name = "llama.cpp"

    def __init__(self, endpoint=None, model=None, timeout=120):
        endpoint = endpoint or os.environ.get(
            "DBRAIN_MODEL_ENDPOINT", "http://127.0.0.1:18087"
        )
        host = self._host_of(endpoint)
        from urllib.parse import urlsplit

        port = urlsplit(endpoint).port
        if host not in LOOPBACK_NAMES:
            raise NonLocalEndpoint(
                "refusing a non-loopback model endpoint: %r (host %r). The service "
                "has no remote path by design." % (endpoint, host)
            )
        # Belt and braces: the name is allowlisted *and* must resolve to loopback.
        self.addresses = assert_loopback(host, port)
        self.host_header = parts_host_header(endpoint)
        # Pin the connection to a verified numeric address. Validating the name and then
        # handing the *name* to urllib leaves a second resolution that can answer
        # differently - classic DNS rebinding. The request goes to the literal IP.
        self.endpoint = pin_to_address(endpoint, self.addresses[0])
        self.declared_endpoint = endpoint.rstrip("/")
        self.model = model or os.environ.get("DBRAIN_MODEL", "gemma-4-E4B_q4_0-it")
        self.timeout = timeout

    @staticmethod
    def _host_of(endpoint):
        from urllib.parse import urlsplit

        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https"):
            raise NonLocalEndpoint("unsupported scheme in %r" % endpoint)
        return (parts.hostname or "").lower()

    def generate(self, prompt, max_tokens=320):
        # Chat endpoint, so the server applies the model's own chat template and the
        # turn ends at end-of-turn. The raw /completion endpoint did neither: the model
        # never saw an end-of-turn and repeated itself up to max_tokens (found 5 Oct, on
        # the first run with weights). Matches the 1 Oct benchmark's request shape.
        body = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                **GEN_PARAMS,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + CHAT_PATH,
            data=body,
            headers={"Content-Type": "application/json", "Host": self.host_header},
        )
        started = time.time()
        try:
            # No proxy handler: an ambient HTTP(S)_PROXY must not be able to move a
            # loopback request off the machine.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),  # ambient HTTP_PROXY must not apply
                RefuseRedirects(),                # a 3xx is a hard stop, not a hop
            )
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Round F finding 3: a model failure, so it must reach ask()'s failure
                # path (and its trace) like any other malformed reply.
                raise MalformedResponse("local model returned bytes that are not UTF-8")
            payload = json.loads(text)
        except ModelUnavailable:
            raise
        except (urllib.error.URLError, socket.error, OSError, http.client.HTTPException) as exc:
            # HTTPException covers IncompleteRead and other protocol failures (round G).
            raise ModelUnavailable(
                "local model at %s did not answer (%s). Start it, or accept no answer: "
                "this service does not call anything else." % (self.endpoint, exc)
            )
        except json.JSONDecodeError as exc:
            raise ModelUnavailable("local model returned non-JSON: %s" % exc)
        return answer_text(payload), time.time() - started

    def describe(self):
        return {
            "adapter": self.name,
            "endpoint": self.endpoint,
            "declared_endpoint": getattr(self, "declared_endpoint", self.endpoint),
            "resolved_addresses": getattr(self, "addresses", []),
            "model": self.model,
            "runtime": os.environ.get("DBRAIN_RUNTIME_VERSION", "llama.cpp b11146 (recorded 1 Oct)"),
        }


class StubAdapter(ModelAdapter):
    """Deterministic adapter for tests. Quotes the evidence back, cites every chunk."""

    name = "stub"

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []

    def generate(self, prompt, max_tokens=320):
        self.calls.append(prompt)
        if self.reply is not None:
            return self.reply, 0.0
        cites = [line.split()[0] for line in prompt.splitlines() if line.startswith("[E")]
        return ("Answer grounded in the evidence. " + " ".join(cites)), 0.0

    def describe(self):
        return {"adapter": self.name, "endpoint": "none", "model": "stub", "runtime": "stub"}


def cloud_keys_present():
    """Names only, for the trace. The service's behaviour does not depend on these."""
    return sorted(k for k in CLOUD_KEY_NAMES if os.environ.get(k))
