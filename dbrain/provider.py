"""Rented-model providers (PV1 step 4, D135). Explicit, logged, never a fallback.

The local llama.cpp adapter stays the default. A provider is used only when the caller
asks for one by name, level and model, and each of those must match the closed registry
below. If the provider fails, the caller gets ModelUnavailable: there is no branch here
that tries another provider, another model, another level or the local model. Look for
one before trusting this file.

The API key comes from the macOS Keychain at call time. It is never read from an
environment variable, never stored on the adapter, and never put in an exception, a
return value or describe(). Every error message below is fixed text plus values this
module chose (provider, model, status code); nothing a server sent is echoed back,
because a server's error text, redirect target or finish reason could carry the key.
"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

from dbrain import adapter as adapter_mod
from dbrain.adapter import MalformedResponse, ModelUnavailable

SECURITY = "/usr/bin/security"
KEY_READ_TIMEOUT = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CHAT_PATH = "/chat/completions"
PARAMS = {"temperature": 0.0, "stream": False}

# The closed registry: provider -> its level, its one base URL, its Keychain item and the
# models it may be asked for. Adding a provider, a level or a model is a reviewed code
# change, not configuration. L2 and L3 arrive in stage 3 (D141; PV1 constraint: no Claude route now).
REGISTRY = {
    "scaleway": {
        "level": "L1",
        "base_url": "https://api.scaleway.ai/v1",
        "keychain": ("dbrain-scaleway", "dbrain"),
        # Request IDs as the API expects them; PV1 step 5 checks them against /v1/models.
        "models": (
            "mistral-small-3.2-24b-instruct-2506",
            "qwen3.8-27b",
            "qwen3-235b-a22b-instruct-2507",
        ),
    },
}


class ProviderRefused(Exception):
    """Refused at construction: unknown provider, wrong level, URL or model."""


class KeyUnavailable(ModelUnavailable):
    """No usable key in the Keychain. Fails closed, before any network call."""


class ModelMismatch(MalformedResponse):
    """The provider says a different model answered than the one configured."""


# Test mode (D74 consult, 21 Oct, after two parser-differential blockers): no URL is
# accepted from a caller at all. A test names only a port; host, scheme and path are fixed
# here, so there is no string whose parsing could disagree with the connection's.
TEST_HOST = "127.0.0.1"


def _test_port(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProviderRefused("test_port must be an int between 1 and 65535")
    return port


def read_keychain(service, account):
    """The key from the login Keychain, or KeyUnavailable. Output never surfaces."""
    try:
        done = subprocess.run(
            [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=KEY_READ_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise KeyUnavailable("could not read the Keychain item %r" % service) from None
    if done.returncode != 0:
        raise KeyUnavailable("no key in the Keychain item %r" % service)
    try:
        key = done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyUnavailable("the Keychain item %r is not text" % service) from None
    return key


def _usable_key(key, service):
    if not isinstance(key, str):
        raise KeyUnavailable("the key reader for %r returned no text" % service)
    key = key.strip()
    if not key or any(c in key for c in "\r\n\0") or not key.isprintable():
        raise KeyUnavailable("the Keychain item %r holds no usable key" % service)
    return key


class _RefuseAllRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The target is not quoted: it could carry anything the server chose.
        raise ProviderRedirect("provider returned a %d redirect; not followed" % code)

    # Python 3.10's handler has no 308 entry; without this a 308 surfaced as an HTTP error.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


class ProviderRedirect(ModelUnavailable):
    """A redirect from the provider, same host or not, is a stop, never a hop."""


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # no ambient HTTP(S)_PROXY / ALL_PROXY, any case
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _RefuseAllRedirects(),
    )


class ProviderAdapter(adapter_mod.ModelAdapter):
    """OpenAI-compatible chat completions at one registered provider, one model."""

    name = "provider"

    def __init__(self, provider, level, model, test_port=None, key_reader=None, timeout=60):
        entry = REGISTRY.get(provider)
        if entry is None:
            raise ProviderRefused("unknown provider %r" % (provider,))
        if level != entry["level"]:
            raise ProviderRefused("provider %r is level %s, not %r" % (provider, entry["level"], level))
        if model not in entry["models"]:
            raise ProviderRefused("model %r is not registered for %r" % (model, provider))
        if test_port is not None:
            _test_port(test_port)
            # Test mode never reads the Keychain: a real key must not reach a test server.
            if key_reader is None or key_reader is read_keychain:
                raise ProviderRefused("test_port needs an injected key_reader, never the Keychain")
            adapter_mod.assert_loopback(TEST_HOST, test_port)
        self.provider, self.level, self.model = provider, level, model
        self._test_port = test_port
        self.keychain = entry["keychain"]
        self._key_reader = key_reader or read_keychain
        self.timeout = timeout

    @property
    def base_url(self):
        """Rebuilt from fixed parts on every use: nothing stored can be changed after the
        checks above, and nothing here is parsed from caller text."""
        if self._test_port is not None:
            return "http://%s:%d/v1" % (TEST_HOST, self._test_port)
        return REGISTRY[self.provider]["base_url"]

    def _key(self):
        service, account = self.keychain
        try:
            key = self._key_reader(service, account)
        except KeyUnavailable:
            raise
        except Exception:
            raise KeyUnavailable("could not read the Keychain item %r" % service) from None
        return _usable_key(key, service)

    def generate_with_meta(self, prompt, max_tokens=320):
        """The answer, with this call's own usage and identity. No state is kept on
        the adapter, so a later badge cannot inherit another call's usage."""
        key = self._key()  # fails closed before any network call
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": adapter_mod.SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            **PARAMS,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + CHAT_PATH, data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        started = time.time()
        unavailable = "provider %s (%s) did not answer" % (self.provider, self.model)
        try:
            with _opener().open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except ModelUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            raise ModelUnavailable("%s: HTTP %d" % (unavailable, exc.code)) from None
        except (urllib.error.URLError, socket.error, OSError, http.client.HTTPException, ValueError):
            raise ModelUnavailable(unavailable + " (connection failed)") from None
        seconds = time.time() - started
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MalformedResponse("provider response exceeds %d bytes" % MAX_RESPONSE_BYTES)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise MalformedResponse("provider returned a body that is not UTF-8 JSON") from None
        text = self._answer(payload)
        if key in text:
            raise MalformedResponse("provider answer contains the API key; refused")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "text": text,
            "seconds": seconds,
            "provider": self.provider,
            "level": self.level,
            "model": self.model,
            "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            "completion_tokens": usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
        }

    def _answer(self, payload):
        if not isinstance(payload, dict) or "error" in payload:
            raise MalformedResponse("provider returned an error or a non-object body")
        # Only an advertised substitution is detectable: a successor answering under the
        # requested ID is not (README, "Rented providers").
        if payload.get("model") != self.model:
            raise ModelMismatch("provider did not report the configured model %r" % self.model)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise MalformedResponse("provider response has no choices[0].message.content") from None
        if not isinstance(content, str):
            raise MalformedResponse("provider content is not text")
        if finish != "stop":
            raise MalformedResponse("provider generation did not finish")
        try:
            return adapter_mod.answer_text({"choices": [{"message": {"content": content},
                                                          "finish_reason": "stop"}]})
        except MalformedResponse:
            raise MalformedResponse("provider returned no answer text") from None

    def generate(self, prompt, max_tokens=320):
        meta = self.generate_with_meta(prompt, max_tokens)
        return meta["text"], meta["seconds"]

    def describe(self):
        return {
            "adapter": self.name,
            "provider": self.provider,
            "level": self.level,
            "endpoint": self.base_url,
            "model": self.model,
            "runtime": "%s API" % self.provider,
        }


def from_env(environ=None, **kwargs):
    """The configured adapter. DBRAIN_PROVIDER unset or empty: the local llama.cpp
    adapter, as before. Set: a provider adapter or ProviderRefused, never the local one."""
    env = os.environ if environ is None else environ
    provider = env.get("DBRAIN_PROVIDER", "")
    if not provider:
        if env.get("DBRAIN_LEVEL") or env.get("DBRAIN_PROVIDER_MODEL"):
            raise ProviderRefused("DBRAIN_LEVEL or DBRAIN_PROVIDER_MODEL set without DBRAIN_PROVIDER")
        return adapter_mod.LlamaCppAdapter(**kwargs)
    level, model = env.get("DBRAIN_LEVEL", ""), env.get("DBRAIN_PROVIDER_MODEL", "")
    if not level or not model:
        raise ProviderRefused("DBRAIN_PROVIDER needs DBRAIN_LEVEL and DBRAIN_PROVIDER_MODEL")
    return ProviderAdapter(provider, level, model, **kwargs)
