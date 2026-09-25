"""Tiny comment/string masker for conservative static import extraction."""

from __future__ import annotations


def mask_comments_and_literals(content: str, *, mask_quoted: bool = False) -> str:
    """Keep newlines and positions; mask comments and template/raw literals.

    Ordinary quoted strings stay visible when an adapter needs their module
    specifier. Java passes ``mask_quoted=True`` because its imports have no
    quoted module names.
    """
    output: list[str] = []
    cursor = 0
    state = "code"
    quote = ""
    while cursor < len(content):
        char = content[cursor]
        next_char = content[cursor + 1] if cursor + 1 < len(content) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                output.extend((" ", " "))
                cursor += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                output.extend((" ", " "))
                cursor += 2
                state = "block_comment"
                continue
            if mask_quoted and content.startswith('"""', cursor):
                output.extend((" ", " ", " "))
                cursor += 3
                state = "text_block"
                continue
            if char in {'"', "'", "`"}:
                quote = char
                state = "quoted"
                output.append(" " if mask_quoted or char == "`" else char)
                cursor += 1
                continue
            output.append(char)
            cursor += 1
            continue
        if state == "line_comment":
            output.append("\n" if char == "\n" else " ")
            cursor += 1
            if char == "\n":
                state = "code"
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                cursor += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                cursor += 1
            continue
        if state == "text_block":
            if content.startswith('"""', cursor):
                output.extend((" ", " ", " "))
                cursor += 3
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                cursor += 1
            continue
        if char == "\\" and next_char:
            masked = mask_quoted or quote == "`"
            output.extend((" " if masked else char, " " if masked else next_char))
            cursor += 2
            continue
        masked = mask_quoted or quote == "`"
        output.append("\n" if char == "\n" and masked else " " if masked else char)
        cursor += 1
        if char == quote:
            state = "code"
    return "".join(output)
