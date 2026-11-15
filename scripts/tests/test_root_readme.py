#!/usr/bin/env python3
"""The repository root README.md is a short, accurate orientation
(REQ-DEVOPS-011, WORK-000005, REQ-CHAT-20261115-003).

It is checked against the repository itself, not against a copy of its text:
the length limit, the three pointers it must carry, that every repository
path it names exists, and that every command it shows is one the tool
accepts (the sub-commands and options are looked up in the tool's own
``--help``). Product neutrality is covered by the identity scan in
scripts/tests/test_workers.py, which reads every tracked text file.

Run: python3 -m unittest scripts.tests.test_root_readme -v
"""
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
MAX_LINES = 80
POINTERS = ("docs/README.md", "AGENTS.md", "docs/process/agent-coordination.md")
# What the request asked the README to cover, as the words it must contain.
TOPICS = ("requests", "claims", "gated completion", "workers", "agent coordination",
          "scripts/devsys.py doctor", "scripts/devsys.py status", "scripts/create_request.py",
          "workers start", "scripts/msg.py panels", "web control panel",
          "terminal dashboard", "qt control panel")
PATH_LIKE = re.compile(r"^[\w.\-]+(/[\w.\-]+)*/?$")


def code_spans(text: str) -> list[str]:
    """Inline `code` spans outside fenced blocks."""
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.findall(r"`([^`\n]+)`", prose)


def shown_commands(text: str) -> list[list[str]]:
    """The commands of the fenced bash blocks: comments and the brackets of
    optional parts removed, split into words."""
    out = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL):
        for line in block.splitlines():
            line = line.split("#", 1)[0].replace("[", " ").replace("]", " ").strip()
            if line:
                out.append(shlex.split(line))
    return out


def help_text(script: str, subcommands: list[str]) -> str:
    cmd = [sys.executable, str(REPO / script), *subcommands, "--help"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=60)
    return res.stdout + res.stderr if res.returncode == 0 else ""


class RootReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding="utf-8") if README.is_file() else ""

    def test_it_exists_and_is_short(self):
        self.assertTrue(README.is_file(), "README.md is missing at the repository root")
        self.assertLessEqual(len(self.text.splitlines()), MAX_LINES)

    def test_it_points_to_the_canonical_documents(self):
        for pointer in POINTERS:
            self.assertIn(f"`{pointer}`", self.text)
            self.assertTrue((REPO / pointer).is_file(), pointer)

    def test_it_covers_what_was_asked(self):
        lowered = self.text.lower()
        for topic in TOPICS:
            self.assertIn(topic, lowered)

    def test_every_repository_path_it_names_exists(self):
        named = [s for s in code_spans(self.text) if "/" in s or s.endswith((".md", ".json"))]
        named = [s for s in named if PATH_LIKE.match(s)]
        self.assertTrue(named)
        for rel in named:
            self.assertTrue((REPO / rel).exists(), f"README.md names {rel}, which does not exist")

    def test_every_command_it_shows_is_accepted_by_the_tool(self):
        commands = shown_commands(self.text)
        self.assertTrue(commands)
        for words in commands:
            if words[0] == "python3":
                script, rest = words[1], words[2:]
            else:  # a launcher run directly (tools/worker-panel)
                self.assertTrue((REPO / words[0]).is_file(), words[0])
                continue
            self.assertTrue((REPO / script).is_file(), script)
            # Leading bare words are sub-commands as long as the tool has a
            # help page for them; what follows are options and their values.
            subs = []
            for word in rest:
                if word.startswith("-") or not help_text(script, subs + [word]):
                    break
                subs.append(word)
            page = help_text(script, subs)
            self.assertTrue(page, f"{script} {' '.join(subs)} --help failed")
            if rest and not rest[0].startswith("-"):
                self.assertTrue(subs, f"README.md shows `{' '.join(words)}`: "
                                      f"{script} has no sub-command {rest[0]}")
            for word in rest[len(subs):]:
                if word.startswith("--"):
                    self.assertIn(word, page, f"README.md shows `{' '.join(words)}`: "
                                              f"{script} {' '.join(subs)} has no option {word}")


if __name__ == "__main__":
    unittest.main()
