"""Fast comment/string masking for conservative static import extraction."""

from __future__ import annotations

import re


_TOKEN = re.compile(
    r'//[^\n]*|/\*[\s\S]*?(?:\*/|\Z)|"""[\s\S]*?(?:"""|\Z)'
    r'|"(?:\\[\s\S]|[^"\\])*?(?:"|\Z)'
    r"|'(?:\\[\s\S]|[^'\\])*?(?:'|\Z)"
    r'|`(?:\\[\s\S]|[^`\\])*?(?:`|\Z)'
)
_NON_NEWLINE = re.compile(r"[^\n]")


def mask_comments_and_literals(content: str, *, mask_quoted: bool = False) -> str:
    """Keep offsets/newlines; retain ordinary quotes for module specifiers.

    An unterminated token masks through EOF; it must not expose fake imports.
    Tokenization runs in the regex engine, not a Python loop over every byte.
    """
    output: list[str] = []
    cursor = 0
    for match in _TOKEN.finditer(content):
        output.append(content[cursor:match.start()])
        token = match.group()
        if not mask_quoted and token.startswith(('"', "'")) and not token.startswith('"""'):
            output.append(token)
        else:
            output.append(_NON_NEWLINE.sub(" ", token))
        cursor = match.end()
    output.append(content[cursor:])
    return "".join(output)
