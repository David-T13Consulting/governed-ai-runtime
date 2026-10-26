"""AS1 step 3: the commit gates in .githooks, through real git operations on synthetic
temporary repositories only (a runtime-like repo, a vault-like repo, a bare remote)."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / ".githooks"
SCAN_SH = Path(__file__).resolve().parent.parent / "secret_scan.sh"


def load_gates():
    sys.dont_write_bytecode = True  # no __pycache__ in .githooks: `check` would see it
    spec = importlib.util.spec_from_file_location("gates_under_test", HOOKS_DIR / "gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gates = load_gates()

# Planted credentials, built so this file holds none.
SK = "sk" + "-" + "abcdefghijklmnopqrstuvwx"
GHP = "ghp" + "_" + "A" * 24
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"

NOTE = "---\nclassification: internal\n---\n# A note\n"
DECISIONS = ("---\nclassification: internal\n---\n# Decisions\n| ID | Date | Decision |\n"
             "|---|---|---|\n| D1 | 1 Oct | A synthetic decision |\n")
FOR_DAVID = ("---\nclassification: internal\n---\n# For David\n## Open\n"
             "- **Q9 → open, not answered**\n## Answered (since 8 Oct)\n- **Q2 → (a)** answered\n")
ARCHIVE = ("---\nclassification: internal\n---\n# Archive\n## Answered\n| Q | Date | Answer |\n"
           "|---|---|---|\n| Q3 | 2 Oct | answered long ago |\n")


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", HOME=home,
               GIT_AUTHOR_DATE="2026-10-14T12:00:00", GIT_COMMITTER_DATE="2026-10-14T12:00:00")
    return env


class GateRepos(unittest.TestCase):
    """Fresh repositories per test: the vault, the runtime pointing at it, a bare remote
    for each, all with the gates installed from this checkout's .githooks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbrain-gates-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.vault = self.make_repo("vault", {
            "10 Projects/AI Second Brain/DECISIONS.md": DECISIONS,
            "90 System/for-david.md": FOR_DAVID,
            "90 System/for-david-archive.md": ARCHIVE,
            "note.md": NOTE})
        self.runtime = self.make_repo("runtime", {"app.py": "print('hi')\n"})
        self.git(self.runtime, "config", "dbrain.vault", self.vault)

    def git(self, repo, *args, data=None, check=True):
        proc = subprocess.run(["git"] + list(args), cwd=repo, env=self.env, input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            raise AssertionError("git %s: %s" % (args, proc.stderr.decode(errors="replace")))
        return proc

    def make_repo(self, kind, files):
        repo = os.path.join(self.tmp, kind)
        os.makedirs(repo)
        self.git(repo, "init", "-q", "-b", "master")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(repo, "config", key, value)
        shutil.copytree(HOOKS_DIR, os.path.join(repo, ".githooks"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in gates.HOOKS:
            path = os.path.join(repo, ".githooks", name)
            with open(path) as f:
                text = f.read().replace("--repo runtime", "--repo " + kind)
            with open(path, "w") as f:
                f.write(text)
        for name, text in files.items():
            self.write(repo, name, text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "initial")   # before the gates are installed
        remote = os.path.join(self.tmp, kind + ".git")
        self.git(self.tmp, "init", "-q", "--bare", remote)
        self.git(repo, "remote", "add", "origin", remote)
        self.git(repo, "push", "-q", "origin", "master")
        installed = self.gate(repo, kind, "install")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        return repo

    def gate(self, repo, kind, *args):
        return subprocess.run([sys.executable, "-I", os.path.join(repo, ".githooks", "gates.py"),
                               "--repo", kind] + list(args), cwd=repo, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, repo, name, text, mode="w"):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(text)
        return path

    def commit(self, repo, message, *args):
        return self.git(repo, "commit", "-q", "-m", message, *args, check=False)

    def blocked(self, proc, *needles):
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        self.assertNotEqual(proc.returncode, 0, out)
        for secret in (SK, GHP, PEM):
            self.assertNotIn(secret, out)
        for needle in needles:
            self.assertIn(needle, out)
        return out

    def passed(self, proc):
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr).decode("utf-8", "replace"))

    def add_commit(self, repo, name, text, message="change", mode="w"):
        self.write(repo, name, text, mode)
        self.git(repo, "add", "-A")
        return self.commit(repo, message)

    def push(self, repo, *args):
        return self.git(repo, "push", "-q", "origin", *(args or ("master",)), check=False)


class SecretScanTest(GateRepos):

    def test_a_clean_commit_passes_in_both_repos(self):
        self.passed(self.add_commit(self.runtime, "b.py", "x = 1\n"))
        self.passed(self.add_commit(self.vault, "other.md", NOTE))
        self.passed(self.push(self.runtime))
        self.passed(self.push(self.vault))

    def test_a_staged_secret_blocks_and_is_never_printed(self):
        for repo in (self.runtime, self.vault):
            for text in ('KEY = "%s"\n' % SK, GHP + "\n", PEM + "\n", "api_" + "key = abcdefghijklmnop\n",
                         "pass" + "word=éabcdefghijklmnop\n"):
                name = "s.txt"
                self.blocked(self.add_commit(repo, name, "ok\n" + text), "s.txt line 2")
                self.git(repo, "reset", "-q")

    def test_only_what_is_staged_counts(self):
        path = self.write(self.runtime, "s.py", "k = '%s'\n" % SK)
        self.git(self.runtime, "add", "s.py")
        self.write(self.runtime, "s.py", "k = None\n")            # clean in the working tree
        self.blocked(self.commit(self.runtime, "staged secret"), "s.py line 1")
        self.git(self.runtime, "add", "s.py")
        self.write(self.runtime, "s.py", "k = '%s'\n" % SK)       # secret only in the working tree
        self.passed(self.commit(self.runtime, "staged clean"))
        os.remove(path)

    def test_commit_all_and_a_path_limited_commit_are_checked(self):
        self.passed(self.add_commit(self.runtime, "c.py", "x = 1\n"))
        self.write(self.runtime, "c.py", "x = '%s'\n" % SK)
        self.blocked(self.commit(self.runtime, "all", "-a"), "c.py line 1")
        self.blocked(self.git(self.runtime, "commit", "-q", "-m", "only", "c.py", check=False),
                     "c.py line 1")

    def test_binary_and_invalid_utf8_content_is_scanned(self):
        for i, data in enumerate((b"\0\x01\x02" + SK.encode(), b"\xff\xfe" + SK.encode())):
            self.write(self.runtime, "b%d.bin" % i, data, "wb")
        self.git(self.runtime, "add", "-A")
        self.blocked(self.commit(self.runtime, "binary"), "b0.bin", "b1.bin")

    def test_a_secret_in_a_path_is_blocked_and_withheld(self):
        self.blocked(self.add_commit(self.runtime, "dir/%s.txt" % SK, "x\n"), "withheld")

    def test_a_symlink_target_is_scanned_and_a_submodule_refused(self):
        os.symlink("x/%s" % SK, os.path.join(self.runtime, "link"))
        self.git(self.runtime, "add", "link")
        self.blocked(self.commit(self.runtime, "link"), "link line 1")
        self.git(self.runtime, "rm", "-q", "--cached", "link")
        sha = self.git(self.runtime, "rev-parse", "HEAD").stdout.decode().strip()
        self.git(self.runtime, "update-index", "--add", "--cacheinfo", "160000,%s,sub" % sha)
        self.blocked(self.commit(self.runtime, "gitlink"), "submodule")

    def test_a_deletion_of_a_file_holding_a_secret_is_allowed(self):
        self.write(self.runtime, "old.txt", SK + "\n")
        self.git(self.runtime, "add", "old.txt")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "planted")
        self.git(self.runtime, "rm", "-q", "old.txt")
        self.passed(self.commit(self.runtime, "remove it"))


class MessageAndShimTest(GateRepos):

    def test_a_secret_in_the_commit_message_blocks_at_commit_and_push(self):
        self.write(self.runtime, "m.py", "x = 1\n")
        self.git(self.runtime, "add", "-A")
        self.blocked(self.commit(self.runtime, "key %s" % SK), "the message, line 1")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "key %s" % SK)
        self.blocked(self.push(self.runtime), "the commit object")

    def test_a_secret_in_the_author_blocks_at_commit_and_push(self):
        self.write(self.runtime, "a.py", "x = 1\n")
        self.git(self.runtime, "add", "-A")
        env = dict(self.env, GIT_AUTHOR_NAME=SK)
        proc = subprocess.run(["git", "commit", "-q", "-m", "m"], cwd=self.runtime, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.blocked(proc, "the author or committer")
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "m"], cwd=self.runtime, env=env,
                       check=True)
        self.blocked(self.push(self.runtime), "the commit object")

    def test_a_shim_outside_a_repository_fails_closed_without_git_text(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        for name in gates.HOOKS:
            proc = subprocess.run([os.path.join(self.runtime, ".githooks", name), "x"], cwd=outside,
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  input=b"")
            out = self.blocked(proc, "not inside a repository")
            self.assertNotIn("fatal", out)

    def test_an_error_naming_a_credential_like_path_withholds_it(self):
        self.git(self.runtime, "config", "dbrain.vault", os.path.join(self.tmp, SK))
        self.write(self.runtime, "AGENTS.md", "x\n")
        self.git(self.runtime, "add", "-A")
        self.blocked(self.commit(self.runtime, "m\n\nDavid-approved: D1"), "fail closed", "withheld")


class ClassificationTest(GateRepos):

    def test_bad_vault_notes_block(self):
        cases = {
            "missing.md": "---\ntitle: x\n---\nbody\n",
            "client.md": "---\nclassification: client\n---\n",
            "other.md": "---\nclassification: secretive\n---\n",
            "twice.md": "---\nclassification: public\nclassification: internal\n---\n",
            "lookalike.md": "---\nClassification: public\nclassification: internal\n---\n",
            "nested.md": "---\nmeta:\n  classification: public\n---\n",
            "nofront.md": "# no frontmatter\nclassification: public\n",
            "unclosed.md": "---\nclassification: public\n",
            "crlf.md": "---\r\nclassification: public\r\n---\r\n",
            "late.md": "\n---\nclassification: public\n---\n",
            "hash.md": "---\nclassification: public#suffix\n---\n",
            "explicit.md": "---\nclassification: public\n? classification\n: client\n---\n",
            "escaped.md": '---\nclassification: public\n"clas\\u0073ification": client\n---\n',
            "flow.md": "---\n{classification: client}\n---\n",
            "merge.md": "---\nclassification: public\n<<: {classification: client}\n---\n",
            "indented.md": "---\n  title: x\n  classification: public\n---\n",
            "continued.md": "---\nclassification: public\n  client\n---\n",
            "continued2.md": "---\nclassification: public\n\n  client\n---\n",
            "quoted.md": '---\ntitle: "starts\nclassification: public\n  ends"\n---\n',
            "single.md": "---\ntitle: 'it''s\nclassification: public\n  x'\n---\n",
            "flowseq.md": "---\ntags: [a,\nclassification: public\n  ]\n---\n",
            "flowquote.md": '---\ntags: ["a]", [b,\nclassification: public\n  ]]\n---\n',
            "anchor.md": '---\ntitle: &x "starts\nclassification: public # ends"\n---\n',
            "tag.md": '---\ntitle: !!str "starts\nclassification: public # ends"\n---\n',
            "cr.md": "---\nclassification: public # c\rclassification: client\n---\n",
            "nel.md": "---\nclassification: public # c\u0085classification: client\n---\n",
            "tab.md": "---\nclassification:\tpublic\n---\n",
            "item.md": '---\ntags:\n- k: "starts\nclassification: public # ends"\n---\n',
            "deep.md": '---\ntags:\n  - "starts\nclassification: public # ends"\n---\n',
            "block.md": "---\nnote: |\n  x\nclassification: public\n---\n",
            "alias.md": "---\nclassification: *a\n---\n",
            "commented.md": "---\nclassification: public\n# comment\n  client\n---\n",
            # The closed grammar (D74 consult after review 5, 15 Oct): no list items, block
            # lists, comment lines or quoted labels; a plain value never holds ": ".
            "listitem.md": "---\n- a\nclassification: public\n---\n",
            "flowcomment.md": "---\ntitle: [a, # ]\nclassification: public\nnext: x]\n---\n",
            "blocklist.md": "---\ntitle: x\ntags:\n- a\nclassification: internal\n---\n",
            "commentline.md": "---\n# c\nclassification: internal\n---\n",
            "quotedlabel.md": "---\nclassification: 'internal'\n---\n",
            "dquotedlabel.md": '---\nclassification: "public"\n---\n',
            "colon.md": "---\nsource: a (context: b)\nclassification: public\n---\n",
            "nested2.md": "---\nclassification: public\nmeta: {a: b}\n---\n",
            # Review 1 of 15 Oct: a second space before a quote; an escape YAML rejects.
            "twospaces.md": '---\ntitle:  "starts\nclassification: public # ends"\n---\n',
            "badescape.md": '---\ntitle: "\\z"\nclassification: public\n---\n',
            # Review 2 of 15 Oct: an escape past Unicode's end; a noncharacter.
            "bigescape.md": '---\ntitle: "\\U00110000"\nclassification: public\n---\n',
            "uescape.md": '---\ntitle: "\\u0041"\nclassification: public\n---\n',
            "fffe.md": '---\ntitle: \ufffe\nclassification: public\n---\n',
            "ffff.md": '---\ntitle: \uffff\nclassification: public\n---\n',
            # Fuzzing, 15 Oct: values YAML 1.1 builds specially and cannot.
            "valuetag.md": "---\nx: =\nclassification: public\n---\n",
            "mergetag.md": "---\nx: <<\nclassification: public\n---\n",
            "baddate.md": "---\ncreated: 2026-02-30\nclassification: public\n---\n",
            "badtime.md": "---\ncaptured: 2026-10-15 25:00:00\nclassification: public\n---\n",
            "badhex.md": "---\nx: 0x_\nclassification: public\n---\n",
            "sexagesimal.md": "---\nclassification: public\nx: " + "1:" * 174 + "1.0\n---\n",
            # Review 3 of 15 Oct: keys YAML reads as a date or number; an over-long key.
            "datekey.md": "---\nclassification: public\n2026-02-30: x\n---\n",
            "hexkey.md": "---\nclassification: public\n0x_: x\n---\n",
            "numkey.md": "---\nclassification: public\n1: x\n---\n",
            "longkey.md": "---\nclassification: public\n" + "a" * 65 + ": x\n---\n",
            "hugekey.md": "---\nclassification: public\n" + "a" * 1025 + ": x\n---\n",
        }
        for name, text in cases.items():
            self.blocked(self.add_commit(self.vault, name, text), name)
            self.git(self.vault, "reset", "-q")
            os.remove(os.path.join(self.vault, name))
        self.write(self.vault, "latin.md", b"---\nclassification: public\n---\n\xe9\n", "wb")
        self.git(self.vault, "add", "-A")
        self.blocked(self.commit(self.vault, "latin"), "latin.md is not valid UTF-8")
        self.git(self.vault, "reset", "-q")
        os.remove(os.path.join(self.vault, "latin.md"))
        os.symlink("note.md", os.path.join(self.vault, "alias.md"))
        self.git(self.vault, "add", "alias.md")
        self.blocked(self.commit(self.vault, "alias"), "may not be a symlink")

    def test_good_vault_notes_pass(self):
        for name, text in {"a.md": "---\nclassification: public\n---\n",
                           "b.md": "---\nclassification: internal  # why\ntitle: x\n---\n",
                           "c.MD": '---\ntitle: "a: b"\nclassification: public\n---\n',
                           "d.md": "---\nclassification: public # a comment\n---\n",
                           "h.md": "---\nclassification: public\n" + "k" * 64 + ": x\n_x: y\n---\n",
                           "e.md": "---\ntitle: x\n\ntags: []   # none\nclassification: internal\nurl: http://a#b\n---\n",
                           "f.md": '---\ntitle: "a \\" b"\ntags: [a, b]\nx: \'it\'\'s\'\nclassification: public\nnext: 1\n---\n',
                           "g.md": '---\nmodels: ["opus (lead)", "c]d", \'x\'\'y\']\nclassification: internal\n---\n'}.items():
            self.passed(self.add_commit(self.vault, name, text))

    def test_what_the_grammar_accepts_yaml_reads_the_same(self):
        """Whatever the gate accepts, a real YAML parser reads as a flat mapping whose one
        classification is public or internal (D74 consult after review 5). Generated
        frontmatters from fragments that tripped earlier reviews; no git, so it runs fast."""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        import itertools
        frags = ["classification: public", "classification: internal", "classification: client",
                 "title: x", "title: a: b", 'title: "a: b"', "title: 'it''s'", "tags: [a, b]",
                 "tags: [a, # ]", "next: x]", "- a", "  client", "# c", "", "x: y # z",
                 "x: y#z", "x: &a b", "x: *a", "x: !t b", "x: |", "x: >", "x: {a: b}",
                 "? x", ": y", "x:", "x: [\"a]\", b]", "x: ['a, b]", "x: - y", "x: @y",
                 "Classification: public", "x: %y", "x: `y`", "x:y", "x: 'a\" b'",
                 # review 1 of 15 Oct: extra spaces before an indicator, YAML escapes
                 'title:  "starts', 'classification: public # ends"', 'x:  &a b', 'x:  [a',
                 "x:  'a", 'x:   y', 'x: "\\z"', 'x: "\\x4"', 'x: "\\x41"', 'x: "\\u00e9 \\""',
                 'x: "a\\"', "x: [a,  'b]", 'x: [ "a", b ]',
                 # review 2 of 15 Oct: numeric escapes, noncharacters, raw ranges
                 'x: "\\U00110000"', 'x: "\\UFFFFFFFF"', 'x: "\\uD800"', 'x: "\\\\"',
                 'x: a\ufffe', 'x: a\uffff', 'x: a\ud7ff\ue000', 'x: "\\\""', 'x: a\x85b',
                 'x: =', 'x: = # c', 'x: =a', 'x: <<', 'x: [<<, =]', 'x: <<a',
                 # fuzzing, 15 Oct: timestamps PyYAML builds, and ones it cannot
                 'x: 2026-02-30', 'x: 2026-10-15', 'x: 0000-01-01', 'x: 2026-1-5',
                 'x: 2026-10-01T14:41:00+02:00', 'x: 2026-10-15 25:00:00',
                 'x: 2026-10-15T10:00:00+99:00', 'x: [2026-02-30]', "x: ['2026-02-30']",
                 'x: 2026-10-15 10:00:60', 'x: 2026-10-01T10:37+0200', 'x: 2026-10-15t1:00:00.5Z',
                 # numbers PyYAML resolves and may fail to build
                 'x: 0x_', 'x: 0b_', 'x: -0x_', 'x: [0x_, b]', 'x: 0_', 'x: 0x1f', 'x: 0777',
                 'x: 1:20', 'x: 1_000.5', 'x: .inf', 'x: 0o_', 'x: 1__0',
                 # review 4 of 15 Oct: a long base-60 float overflows in PyYAML
                 'x: ' + '1:' * 174 + '1.0', 'x: ' + '1:' * 100 + '1.0', 'x: ' + '1:' * 174 + '1']
        accepted = 0
        for n in (1, 2, 3):
            for combo in itertools.product(frags, repeat=n):
                front = "\n".join(combo)
                blob = ("---\n%s\n---\n" % front).encode("utf-8")
                if gates.classification_problem(blob) is not None:
                    continue
                accepted += 1
                data = yaml.safe_load(front)
                self.assertIsInstance(data, dict, front)
                self.assertIn(data.get("classification"), ("public", "internal"), front)
                self.assertEqual(sum(1 for k in data if str(k).lower() == "classification"), 1, front)
                for v in data.values():
                    self.assertFalse(isinstance(v, dict), front)
        self.assertGreater(accepted, 100)

    def test_random_frontmatters_yaml_reads_as_the_gate_does(self):
        """Seeded random frontmatters from a tricky alphabet: whatever the gate accepts, PyYAML
        builds as a flat mapping with one public/internal label (fuzzing, 15 Oct, which found
        "=", "<<", bad dates, "0x_" and flow punctuation). 2.4M draws found none after it."""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        import random
        rnd = random.Random(15)
        alpha = list("ab: #'\"[]{},-?&*!|>%@`\\~=<.\t+;()/^$_TZ") + [
            "\u00a0", "\u0085", "\ufffe", "\ufeff", "\u2028", "é", "0", "1", "2", "9", "x", " "]
        keys = ["classification", "Classification", "title", "x", "tags", "1", "true", "on",
                "2026-02-30", "0x_", "_", "y", "null", "a" * 64]
        vals = ["public", "internal", "client", "'public'", '"public"', "[", "~", "=", "<<",
                "2026-02-3", "2026-10-1", "2026-1-1 1:00:0", "0000-0", "1:2", "0x", "0b", ".", "("]
        accepted = 0
        for _ in range(40000):
            lines = []
            for _ in range(rnd.randint(1, 4)):
                r = rnd.random()
                if r < 0.55:
                    v = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 6)))
                    if rnd.random() < 0.6:
                        v = rnd.choice(vals) + v
                    if rnd.random() < 0.25:
                        v = "[" + v + rnd.choice(["]", ", a]", ", 'b']"])
                    key = rnd.choice(keys) if rnd.random() < 0.7 else "".join(
                        rnd.choice(alpha + list("0123456789-_xT")) for _ in range(rnd.randint(1, 12)))
                    lines.append(key + ":" + rnd.choice(["", " ", "  "]) + v)
                elif r < 0.65:
                    lines.append("classification: " + rnd.choice(["public", "internal"]))
                else:
                    lines.append("".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 10))))
            front = "\n".join(lines)
            if gates.classification_problem(("---\n%s\n---\n" % front).encode("utf-8")) is not None:
                continue
            accepted += 1
            data = yaml.safe_load(front)
            self.assertIsInstance(data, dict, front)
            self.assertIn(data.get("classification"), ("public", "internal"), front)
            self.assertEqual(sum(1 for k in data if str(k).lower() == "classification"), 1, front)
        self.assertGreater(accepted, 1000)

    def test_the_runtime_does_not_check_labels(self):
        self.passed(self.add_commit(self.runtime, "README.md", "# no frontmatter\n"))


class ProtectedFilesTest(GateRepos):

    PATHS = ("AGENTS.md", "sub/AGENTS.md", "Agents.MD", "CLAUDE.md", "x/claude.md",
             "data-policy.md", "a/Data-Policy-v2.md", ".claude/settings.json", "x/.CLAUDE/y",
             ".gitignore", "d/.gitignore", "prof.sb", "secret_scan.sh", "x/secret_scan.sh",
             ".githooks/extra")

    def test_every_protected_path_needs_a_recorded_approval(self):
        for repo in (self.runtime, self.vault):
            for path in self.PATHS:
                # Unique content: on macOS, Agents.MD and AGENTS.md are the same file.
                self.write(repo, path, NOTE + "# " + path + "\n")
                self.git(repo, "add", "-A")
                proc = self.commit(repo, "touch %s" % path)
                self.assertNotIn(b"nothing to commit", proc.stdout, (repo, path))
                self.blocked(proc, "without a David-approved")
                self.passed(self.commit(repo, "touch %s\n\nDavid-approved: D1" % path))

    def test_protected_names_are_matched_case_folded_at_any_depth(self):
        for path in ("Agents.MD", "a/b/CLAUDE.MD", "x/.Claude/s.json", "D/.GitIgnore", "P.SB",
                     "Secret_Scan.sh", ".GitHooks/pre-commit", "a/DATA-POLICY.md"):
            self.assertTrue(gates.is_protected(path.encode()), path)
        for path in ("agents.md.bak", "notclaude.md", "claude", "policy.md", "x.sbx", "githooks/x"):
            self.assertFalse(gates.is_protected(path.encode()), path)

    def test_deleting_or_renaming_a_protected_file_needs_it_too(self):
        self.passed(self.add_commit(self.runtime, "AGENTS.md", "x\n", "add\n\nDavid-approved: D1"))
        self.git(self.runtime, "mv", "AGENTS.md", "moved.md")
        self.blocked(self.commit(self.runtime, "rename"), "AGENTS.md")
        self.git(self.runtime, "reset", "-q", "--hard")
        self.git(self.runtime, "rm", "-q", "AGENTS.md")
        self.blocked(self.commit(self.runtime, "delete"), "AGENTS.md")
        self.passed(self.commit(self.runtime, "delete\n\nDavid-approved: Q2"))

    def test_which_approvals_count(self):
        self.write(self.runtime, "AGENTS.md", "x\n")
        self.git(self.runtime, "add", "-A")
        for msg, ok in (("m\n\nDavid-approved: Q2", True),        # answered, current queue
                        ("m\n\nDavid-approved: Q3", True),        # answered, archive
                        ("m\n\nDavid-approved: D99", False),      # not recorded
                        ("m\n\nDavid-approved: Q9", False),       # open, not answered
                        ("m\n\nDavid-approved: D1x", False),      # not a plain ID
                        ("m\n\nDavid-approved: D1, D2", False),
                        ("m\n\ndavid-approved: D1", False),       # wrong key
                        ("David-approved: D1\n\nbody after it", False),  # not in the trailer block
                        ("m\n\nDavid-approved: D1\n\nCo-Authored-By: X <x@y.z>", False),
                        ("m\n\nDavid-approved: D1\nCo-Authored-By: X <x@y.z>", True)):
            proc = self.commit(self.runtime, msg, "--allow-empty")
            if ok:
                self.passed(proc)
                self.git(self.runtime, "reset", "-q", "--soft", "HEAD~1")
            else:
                self.blocked(proc)

    def test_approval_is_read_from_committed_vault_state_only(self):
        # Recorded in the vault's working files and index, not committed: does not count.
        self.write(self.vault, "10 Projects/AI Second Brain/DECISIONS.md",
                   DECISIONS + "| D7 | 14 Oct | staged only |\n")
        self.git(self.vault, "add", "-A")
        self.write(self.runtime, "AGENTS.md", "x\n")
        self.git(self.runtime, "add", "-A")
        self.blocked(self.commit(self.runtime, "m\n\nDavid-approved: D7"), "D7")
        # In the vault, the same commit cannot approve itself: the parent must hold it.
        self.write(self.vault, "AGENTS.md", NOTE)
        self.git(self.vault, "add", "-A")
        self.blocked(self.commit(self.vault, "m\n\nDavid-approved: D7"), "D7")
        self.git(self.vault, "reset", "-q", "AGENTS.md")
        self.passed(self.commit(self.vault, "record D7"))
        self.git(self.vault, "add", "-A")
        self.passed(self.commit(self.vault, "m\n\nDavid-approved: D7"))
        self.passed(self.commit(self.runtime, "m\n\nDavid-approved: D7"))

    def test_a_missing_vault_fails_closed(self):
        self.git(self.runtime, "config", "dbrain.vault", os.path.join(self.tmp, "nowhere"))
        self.write(self.runtime, "AGENTS.md", "x\n")
        self.git(self.runtime, "add", "-A")
        out = self.blocked(self.commit(self.runtime, "m\n\nDavid-approved: D1"), "fail closed")
        self.assertIn("COMMIT GATE ERROR", out)
        self.git(self.runtime, "reset", "-q")
        os.remove(os.path.join(self.runtime, "AGENTS.md"))
        self.passed(self.add_commit(self.runtime, "b.py", "x = 1\n"))  # unprotected: no lookup


class PrePushTest(GateRepos):
    """Commits made without the commit hooks are re-checked when they are pushed."""

    def test_no_verify_commits_are_caught_at_push(self):
        self.write(self.runtime, "s.txt", SK + "\n")
        self.git(self.runtime, "add", "-A")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "bypass")
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.write(self.vault, "bad.md", "no label\n")
        self.write(self.vault, "AGENTS.md", NOTE)
        self.git(self.vault, "add", "-A")
        self.git(self.vault, "commit", "-q", "--no-verify", "-m", "bypass")
        self.blocked(self.push(self.vault), "bad.md has no frontmatter", "without a David-approved")

    def test_an_amend_that_drops_the_approval_is_caught_at_push(self):
        self.passed(self.add_commit(self.runtime, "AGENTS.md", "x\n", "ok\n\nDavid-approved: D1"))
        self.passed(self.commit(self.runtime, "no approval now", "--amend"))
        self.blocked(self.push(self.runtime), "without a David-approved")

    def test_cherry_pick_merge_and_plumbing_commits_are_caught_at_push(self):
        self.git(self.runtime, "checkout", "-q", "-b", "side")
        self.write(self.runtime, "s.txt", SK + "\n")
        self.git(self.runtime, "add", "-A")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "on side")
        side = self.git(self.runtime, "rev-parse", "HEAD").stdout.decode().strip()
        self.git(self.runtime, "checkout", "-q", "master")
        self.git(self.runtime, "cherry-pick", side, check=False)
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.git(self.runtime, "reset", "-q", "--hard", "origin/master")
        # A merge: refused when made, and caught at push when forced through.
        self.passed(self.add_commit(self.runtime, "m.py", "x = 1\n"))
        self.blocked(self.git(self.runtime, "merge", "--no-ff", "-q", "-m", "merge", side,
                              check=False), "linear history")
        self.git(self.runtime, "merge", "--abort", check=False)
        self.git(self.runtime, "merge", "--no-ff", "--no-verify", "-q", "-m", "merge", side)
        self.blocked(self.push(self.runtime), "merge commit")
        self.git(self.runtime, "reset", "-q", "--hard", "origin/master")
        # Plumbing: a commit object written and pointed to without any commit hook.
        tree = self.git(self.runtime, "write-tree").stdout.decode().strip()
        blob = self.git(self.runtime, "hash-object", "-w", "--stdin",
                        data=(SK + "\n").encode()).stdout.decode().strip()
        self.git(self.runtime, "update-index", "--add", "--cacheinfo", "100644,%s,p.txt" % blob)
        tree = self.git(self.runtime, "write-tree").stdout.decode().strip()
        commit = self.git(self.runtime, "commit-tree", tree, "-p", "HEAD",
                          data=b"plumbing\n").stdout.decode().strip()
        self.git(self.runtime, "update-ref", "refs/heads/master", commit)
        self.blocked(self.push(self.runtime), "p.txt line 1")

    def test_am_and_rebase_replays_are_caught_at_push(self):
        self.git(self.runtime, "checkout", "-q", "-b", "side")
        self.write(self.runtime, "s.txt", SK + "\n")
        self.git(self.runtime, "add", "-A")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "secret on side")
        patch = self.git(self.runtime, "format-patch", "-1", "--stdout").stdout
        self.git(self.runtime, "checkout", "-q", "master")
        self.git(self.runtime, "am", "-q", data=patch, check=False)
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.git(self.runtime, "am", "--abort", check=False)
        self.git(self.runtime, "reset", "-q", "--hard", "origin/master")
        self.git(self.runtime, "checkout", "-q", "-b", "side2")
        self.write(self.runtime, "AGENTS.md", "x\n")
        self.git(self.runtime, "add", "-A")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "protected, no approval")
        self.git(self.runtime, "checkout", "-q", "master")
        self.passed(self.add_commit(self.runtime, "n.py", "x = 3\n"))
        self.git(self.runtime, "checkout", "-q", "side2")
        self.git(self.runtime, "rebase", "-q", "master", check=False)
        self.git(self.runtime, "checkout", "-q", "master")
        self.git(self.runtime, "merge", "-q", "--ff-only", "side2")
        self.blocked(self.push(self.runtime), "without a David-approved")

    def plant(self, repo, message="planted"):
        """A commit holding a secret, made without the commit hooks; its id."""
        self.write(repo, "s.txt", SK + "\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "--no-verify", "-m", message)
        return self.rev(repo, "HEAD")

    def rev(self, repo, spec):
        return self.git(repo, "rev-parse", spec).stdout.decode().strip()

    def bare(self, name, *pushes):
        """A bare repository holding the runtime's commits given as (commit, ref) pairs,
        written past the gates (set-up only)."""
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "init", "-q", "--bare", path)
        for commit, ref in pushes:
            self.git(self.runtime, "push", "-q", "--no-verify", path, "%s:%s" % (commit, ref))
        return path

    def holds(self, bare, commit):
        return self.git(bare, "cat-file", "-e", commit + "^{commit}", check=False).returncode == 0

    def hook(self, stdin, *args):
        return subprocess.run([sys.executable, "-I", os.path.join(self.runtime, ".githooks", "gates.py"),
                               "--repo", "runtime", "pre-push"] + list(args),
                              cwd=self.runtime, env=self.env, input=stdin.encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def test_local_tags_of_a_blob_or_tree_or_with_secret_text_refuse_any_push(self):
        """Git can pack a tag it does not list for the hook (a rejected update), so every
        local tag is checked on every push (D74 consult, 19 Oct)."""
        blob = self.git(self.runtime, "hash-object", "-w", "--stdin",
                        data=(SK + "\n").encode()).stdout.decode().strip()
        tree = self.rev(self.runtime, "HEAD^{tree}")
        for name, target, needle in (("leak", blob, "pushing a blob directly"),
                                     ("tree", tree, "pushing a tree directly")):
            self.git(self.runtime, "tag", name, target)
            self.passed(self.add_commit(self.runtime, name + ".py", "x = 1\n"))
            self.blocked(self.push(self.runtime), needle)
            self.git(self.runtime, "tag", "-d", name)
        self.git(self.runtime, "tag", "-a", "noted", "-m", "key %s" % SK)
        self.blocked(self.push(self.runtime), "the tag's text")
        self.git(self.runtime, "tag", "-d", "noted")
        self.git(self.runtime, "tag", "-a", "fine", "-m", "release")
        self.passed(self.push(self.runtime))

    def test_creating_a_branch_or_tag_on_the_remote_is_refused(self):
        remote = os.path.join(self.tmp, "runtime.git")
        self.git(self.runtime, "checkout", "-q", "-b", "topic")
        self.passed(self.add_commit(self.runtime, "t.py", "x = 2\n"))
        self.blocked(self.push(self.runtime, "topic"), "refs/heads/topic: creating a ref")
        self.git(self.runtime, "tag", "-a", "v1", "-m", "release")
        self.blocked(self.push(self.runtime, "refs/tags/v1"), "refs/tags/v1: creating a ref")
        self.blocked(self.push(self.runtime, "--follow-tags", "topic:master"), "creating a ref")
        self.assertFalse(self.holds(remote, self.rev(self.runtime, "topic")))
        self.passed(self.push(self.runtime, "topic:master"))   # an update of master is fine

    def test_a_remote_commit_not_known_here_refuses_the_push(self):
        tip = self.rev(self.runtime, "HEAD")
        self.blocked(self.hook("refs/heads/master %s refs/heads/master %s\n" % (tip, "1" * 40),
                               "origin", "unused"), "not here; fetch first")

    def test_deleting_a_remote_ref_sends_nothing_and_passes_the_gate(self):
        self.blocked(self.hook("(delete) %s refs/heads/old %s\n" % ("0" * 40, self.rev(self.runtime, "HEAD"))
                               + "refs/heads/master %s refs/heads/master %s\n"
                               % (self.rev(self.runtime, "HEAD"), "1" * 40), "o", "u"), "fetch first")
        self.passed(self.hook("(delete) %s refs/heads/old %s\n"
                              % ("0" * 40, self.rev(self.runtime, "HEAD")), "o", "u"))

    def test_a_violation_stays_blocked_after_a_later_commit_repairs_it(self):
        self.plant(self.runtime)
        self.passed(self.git(self.runtime, "rm", "-q", "s.txt"))
        self.passed(self.commit(self.runtime, "remove it"))
        self.blocked(self.push(self.runtime), "s.txt line 1")

    def test_a_forced_divergent_update_checks_its_whole_new_side(self):
        remote = os.path.join(self.tmp, "runtime.git")
        self.passed(self.add_commit(self.runtime, "a.py", "x = 1\n"))
        self.passed(self.push(self.runtime))
        self.git(self.runtime, "reset", "-q", "--hard", "HEAD~1")
        planted = self.plant(self.runtime)
        self.passed(self.git(self.runtime, "rm", "-q", "s.txt"))
        self.passed(self.commit(self.runtime, "remove it"))
        self.blocked(self.push(self.runtime, "--force", "master"), "s.txt line 1")
        self.assertFalse(self.holds(remote, planted))

    def test_stale_remote_tracking_refs_do_not_hide_commits(self):
        planted = self.plant(self.runtime)
        self.git(self.runtime, "update-ref", "refs/remotes/origin/master", planted)   # stale
        self.blocked(self.push(self.runtime), "s.txt line 1")
        empty = self.bare("empty.git")
        self.git(self.runtime, "remote", "set-url", "origin", empty)
        self.blocked(self.push(self.runtime), "creating a ref")
        self.assertFalse(self.holds(empty, planted))

    def test_a_push_url_other_than_the_fetch_url_is_what_counts(self):
        """D74 consult after review 5 (15 Oct): a commit the fetch URL has but the push URL
        lacks must still be checked."""
        initial = self.rev(self.runtime, "HEAD")
        planted = self.plant(self.runtime)
        self.git(self.runtime, "push", "-q", "--no-verify", "origin", "master")  # fetch URL has it
        other = self.bare("push-only.git", (initial, "refs/heads/master"))
        self.git(self.runtime, "remote", "set-url", "--push", "origin", other)
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.assertFalse(self.holds(other, planted))

    def test_a_push_path_named_like_another_remote_is_what_counts(self):
        """Review 4 of 15 Oct: origin's pushurl "sink" is a folder; a remote named "sink"
        points elsewhere and already holds the commit."""
        initial = self.rev(self.runtime, "HEAD")
        planted = self.plant(self.runtime)
        has = self.bare("has.git", (planted, "refs/heads/master"))
        self.git(self.runtime, "remote", "add", "sink", has)
        self.git(self.runtime, "init", "-q", "--bare", "sink")
        self.git(self.runtime, "push", "-q", "--no-verify", os.path.join(self.runtime, "sink"),
                 "%s:refs/heads/master" % initial)
        with open(os.path.join(self.runtime, ".git", "info", "exclude"), "a") as f:
            f.write("sink/\n")
        self.git(self.runtime, "remote", "set-url", "--push", "origin", "sink")
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.assertFalse(self.holds(os.path.join(self.runtime, "sink"), planted))

    def test_chained_url_rewriting_does_not_hide_commits(self):
        """Review 5 of 15 Oct: git rewrites "as1:repo" to A once; a second lookup of A would
        be rewritten again to B, which already holds the commit."""
        initial = self.rev(self.runtime, "HEAD")
        planted = self.plant(self.runtime)
        a = self.bare("A.git", (initial, "refs/heads/master"))
        b = self.bare("B.git", (planted, "refs/heads/master"))
        self.git(self.runtime, "config", "url.file://%s.insteadOf" % a, "as1:repo")
        self.git(self.runtime, "config", "url.file://%s.insteadOf" % b, "file://%s" % a)
        self.git(self.runtime, "remote", "set-url", "--push", "origin", "as1:repo")
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.assertFalse(self.holds(a, planted))

    def test_an_update_git_rejects_itself_cannot_carry_unchecked_commits(self):
        """D74 consult, 19 Oct: git leaves a non-fast-forward update out of the hook's input
        but can still pack its commits beside an accepted update."""
        remote = os.path.join(self.tmp, "runtime.git")
        self.git(self.runtime, "checkout", "-q", "-b", "other")
        self.passed(self.add_commit(self.runtime, "o.py", "x = 1\n"))
        self.git(self.runtime, "push", "-q", "--no-verify", "origin", "other")
        self.git(self.runtime, "reset", "-q", "--hard", "master")
        planted = self.plant(self.runtime)                 # other now diverges from the remote
        self.git(self.runtime, "checkout", "-q", "master")
        self.passed(self.add_commit(self.runtime, "m.py", "x = 2\n"))
        for args in (("master", "other"), ("--all",)):
            self.blocked(self.push(self.runtime, *args), "s.txt line 1")
            self.assertFalse(self.holds(remote, planted))
        self.git(self.runtime, "branch", "-q", "-D", "other")
        self.passed(self.push(self.runtime, "--atomic", "master"))

    def test_replacement_objects_do_not_hide_content(self):
        clean = self.rev(self.runtime, "HEAD")
        self.plant(self.runtime)
        blob = self.rev(self.runtime, "HEAD:s.txt")
        harmless = self.git(self.runtime, "hash-object", "-w", "--stdin",
                            data=b"nothing\n").stdout.decode().strip()
        self.git(self.runtime, "replace", blob, harmless)
        self.blocked(self.push(self.runtime), "s.txt line 1")
        self.git(self.runtime, "replace", "-d", blob)
        # The planted commit shown as a clean one on the same parent.
        cover = self.git(self.runtime, "commit-tree", clean + "^{tree}", "-p", clean,
                         data=b"cover\n").stdout.decode().strip()
        self.git(self.runtime, "replace", self.rev(self.runtime, "HEAD"), cover)
        self.blocked(self.push(self.runtime), "s.txt line 1")

    def test_shallow_or_grafted_history_refuses_the_push(self):
        grafts = os.path.join(self.runtime, ".git", "info", "grafts")
        self.write(self.runtime, ".git/info/grafts", self.rev(self.runtime, "HEAD") + "\n")
        self.blocked(self.push(self.runtime), "grafts")
        os.remove(grafts)
        self.write(self.runtime, ".git/shallow", self.rev(self.runtime, "HEAD") + "\n")
        self.blocked(self.push(self.runtime), "shallow")

    def test_the_hook_needs_both_of_gits_arguments(self):
        self.assertNotEqual(self.hook("", "origin").returncode, 0)

    def test_a_credential_like_ref_name_is_refused_and_withheld(self):
        self.blocked(self.push(self.runtime, "HEAD:refs/heads/%s" % SK), "ref name", "withheld")


class InstallAndCheckTest(GateRepos):

    def test_check_passes_when_installed_and_detects_tampering(self):
        self.passed(self.gate(self.runtime, "runtime", "check"))
        self.git(self.runtime, "config", "--unset", "core.hooksPath")
        self.blocked(self.gate(self.runtime, "runtime", "check"), "core.hooksPath")
        self.git(self.runtime, "config", "core.hooksPath", ".githooks")
        gate = os.path.join(self.runtime, ".githooks", "gates.py")
        with open(gate, "a") as f:
            f.write("\n# edited\n")
        self.blocked(self.gate(self.runtime, "runtime", "check"), "differs from HEAD")
        self.git(self.runtime, "checkout", "-q", "--", ".githooks")
        os.chmod(os.path.join(self.runtime, ".githooks", "pre-push"), 0o644)
        self.blocked(self.gate(self.runtime, "runtime", "check"), "not executable")
        os.chmod(os.path.join(self.runtime, ".githooks", "pre-push"), 0o755)
        self.write(self.runtime, ".githooks/stray", "x\n")
        self.blocked(self.gate(self.runtime, "runtime", "check"), "does not track")
        os.remove(os.path.join(self.runtime, ".githooks", "stray"))
        self.passed(self.gate(self.runtime, "runtime", "check"))
        # Index flags that make git's own diff look away (review 1).
        for flag in ("--assume-unchanged", "--skip-worktree"):
            self.git(self.runtime, "update-index", flag, ".githooks/pre-push")
            with open(os.path.join(self.runtime, ".githooks", "pre-push"), "a") as f:
                f.write("# edited\n")
            out = self.blocked(self.gate(self.runtime, "runtime", "check"), "differs from HEAD")
            self.assertIn("assume-unchanged or skip-worktree", out)
            self.git(self.runtime, "update-index", flag.replace("--", "--no-"), ".githooks/pre-push")
            self.git(self.runtime, "checkout", "-q", "--", ".githooks")

    def test_check_withholds_a_credential_like_hook_path(self):
        name = ".githooks/%s.txt" % SK
        self.write(self.runtime, name, "x\n")
        self.git(self.runtime, "add", "-A")
        self.git(self.runtime, "commit", "-q", "--no-verify", "-m", "m")
        os.remove(os.path.join(self.runtime, name))
        self.blocked(self.gate(self.runtime, "runtime", "check"), "withheld")

    def test_a_fresh_clone_installs_with_one_command(self):
        clone = os.path.join(self.tmp, "clone")
        self.git(self.tmp, "clone", "-q", os.path.join(self.tmp, "runtime.git"), clone)
        self.blocked(self.gate(clone, "runtime", "check"), "core.hooksPath")
        self.passed(self.gate(clone, "runtime", "install"))


class FailClosedTest(unittest.TestCase):

    def test_an_unmerged_or_unknown_change_is_an_error(self):
        for status in (b"U", b"X", b"C100", b"R100"):
            with self.assertRaises(gates.GateError):
                gates.parse_raw(b":100644 100644 aa bb " + status + b"\0p\0")
        with self.assertRaises(gates.GateError):
            gates.parse_raw(b"garbage\0p\0")

    def test_any_failure_inside_a_hook_refuses_and_prints_no_detail(self):
        real = gates.subprocess.run

        def broken(*a, **k):
            raise OSError("detail that must not be printed: " + SK)
        gates.subprocess.run = broken
        try:
            import io
            err, sys.stderr = sys.stderr, io.StringIO()
            try:
                rc = gates.main(["--repo", "runtime", "pre-commit"])
                printed = sys.stderr.getvalue()
            finally:
                sys.stderr = err
        finally:
            gates.subprocess.run = real
        self.assertEqual(rc, 2)
        self.assertIn("fail closed", printed)
        self.assertNotIn(SK, printed)
        self.assertNotIn("detail", printed)

    def test_the_self_test_guards_a_broken_pattern_list(self):
        real = gates.COMPILED
        gates.COMPILED = ()
        try:
            self.assertEqual(gates.main(["--repo", "runtime", "check"]), 2)
        finally:
            gates.COMPILED = real


class ParityWithSecretScanTest(unittest.TestCase):
    """The gate's patterns are the runtime scanner's, and flag the same lines."""

    def scan_sh_patterns(self):
        text = SCAN_SH.read_text()
        block = text.split("PATTERNS='", 1)[1].split("'\n", 1)[0]
        return [p for p in block.split("\n") if p]

    def test_the_lists_are_identical(self):
        self.assertEqual(self.scan_sh_patterns(), [ere for _, ere in gates.PATTERNS])

    def test_they_flag_the_same_lines(self):
        lines = ['k = "%s"' % SK, GHP, "AKIA" + "ABCDEFGHIJKLMNOP", PEM,
                 "12345678" + ":AA" + "b" * 30, "pass" + "word = abcdefghijklm", "tok" + "en:\t'abcdefghijklmn",
                 "secret =x", "sk-short", "API-KEY = abcdefghijklmnop", "api" + "key=abcdefghijklmnop",
                 "Password = abcdefghijklmn", "token\v=\fabcdefghijklmn", "a normal line",
                 "pass" + "word=éabcdefghijklmnop", "tok" + "en: 🔑abcdefghijklmn",
                 "pass" + "word\u2003=abcdefghijklmnop", "pass" + "word\u00a0=\u00a0abcdefghijklmnop",
                 "pass" + "word = ééééééééééééé"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            for line in lines:
                with open(path, "w") as f:
                    f.write(line + "\n")
                env = dict(os.environ, LC_ALL="en_US.UTF-8")
                codes = [subprocess.run(["grep", "-qE", "-e", p, path], env=env).returncode
                         for p in self.scan_sh_patterns()]
                self.assertTrue(set(codes) <= {0, 1}, "grep failed: %r" % codes)  # review 5
                grep = 0 in codes
                # The gate flags at least what grep flags in a UTF-8 locale (review 1).
                if grep:
                    self.assertTrue(gates.scan(line.encode()), repr(line))
                if line == "a normal line":
                    self.assertFalse(gates.scan(line.encode()))


if __name__ == "__main__":
    unittest.main()
