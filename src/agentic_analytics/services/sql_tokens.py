"""SQL lexical boundaries for policy checks and source-reference substitution.

DuckDB remains the SQL parser. These tokens only distinguish executable identifiers from
comments and string data, retaining offsets so substitutions cannot change literal values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z_0-9\u0080-\U0010ffff]*)?\$")


@dataclass(frozen=True)
class SqlToken:
    kind: str
    value: str
    start: int
    end: int


def tokenize_sql(sql: str) -> list[SqlToken]:
    tokens: list[SqlToken] = []
    index = 0
    while index < len(sql):
        start = index
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            ending = re.search(r"[\r\n]", sql[index + 2 :])
            index = len(sql) if ending is None else index + 2 + ending.end()
            continue
        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while depth and index < len(sql):
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError("unterminated SQL comment")
            continue
        dollar = _DOLLAR_QUOTE.match(sql, index)
        if dollar:
            marker = dollar.group()
            end = sql.find(marker, dollar.end())
            if end < 0:
                raise ValueError("unterminated SQL dollar string")
            index = end + len(marker)
            tokens.append(SqlToken("string", sql[dollar.end() : end], start, index))
            continue
        escaped = char.lower() == "e" and sql[index + 1 : index + 2] == "'"
        if char in {"'", '"'} or escaped:
            if escaped:
                index += 1
            quote = sql[index]
            index += 1
            value: list[str] = []
            while index < len(sql):
                if escaped and sql[index] == "\\":
                    # Keep escapes opaque: source IDs never require escape sequences.
                    value.append(sql[index : index + 2])
                    index += 2
                elif sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote:
                        value.append(quote)
                        index += 1
                    else:
                        break
                else:
                    value.append(sql[index])
                    index += 1
            else:
                raise ValueError("unterminated SQL quoted value")
            kind = "quoted_identifier" if quote == '"' else "string"
            tokens.append(SqlToken(kind, "".join(value), start, index))
            continue
        if char.isalpha() or char == "_":
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            tokens.append(SqlToken("identifier", sql[start:index], start, index))
            continue
        tokens.append(SqlToken("symbol", char, start, start + 1))
        index += 1
    return tokens
