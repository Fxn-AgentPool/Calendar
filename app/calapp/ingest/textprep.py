"""Clean up incoming text before it goes to the LLM.

Quoted replies and forwarded parts are kept on purpose: the event is often
in them ("> Can we meet Tuesday at 3?" / "Sounds good").
"""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_HTML_HINT = re.compile(r"<(html|body|div|p|br|table|tr|td|span|a|font)\b", re.IGNORECASE)
_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}
_SKIP_TAGS = {"script", "style", "head", "title"}
# Control and invisible formatting characters, except tab and newline.
_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def clean(text: str, max_chars: int) -> str:
    if _HTML_HINT.search(text):
        text = html_to_text(text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        # Keep the start: in emails and chats the newest part is usually first.
        text = text[:max_chars].rstrip() + "\n[…cut]"
    return text
