from __future__ import annotations

import re


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ROOT_PROMPT_RE = re.compile(r"\[root@[^\]]+\]#")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def contains_shell_prompt(text: str, expected_prompt: str) -> bool:
    if expected_prompt and expected_prompt in text:
        return True
    return _ROOT_PROMPT_RE.search(text) is not None
