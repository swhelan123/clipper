#!/usr/bin/env python3
"""
Clipboard + screenshot watcher for macOS, living in the menu bar.

- Polls the clipboard every second
- When it changes, checks if it starts with "#"
- If so, sends the text (minus prefix) to Claude and copies the reply back
- Watches the screenshot folder for new screenshots
- New screenshots are sent to Claude and the reply is copied to the clipboard
- Latest reply is shown in the menu bar (click a line to copy the full reply)
"""

import base64
import json
import os
import queue
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import rumps

POLL_INTERVAL_SECONDS = 1.0
CONTEXT_CHAR_LIMIT = 4000  # rough cap on total chars of history
HISTORY_MAX_EXCHANGES = 5  # max past Q/A pairs to keep
TRIGGER_PREFIX = "#"

MENU_TITLE_IDLE = "📋"
MENU_TITLE_BUSY = "⏳"
MENU_TITLE_CHARS = 40  # chars of the reply shown directly in the menu bar
MENU_WRAP_WIDTH = 70  # chars per line in the dropdown
MENU_MAX_LINES = 40  # cap on dropdown lines for long replies

SCREENSHOT_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

SCREENSHOT_PROMPT = """The attached screenshot contains a question. Answer it.

Your reply is copied to the clipboard and shown in a narrow menu bar, so it must be as short as possible: just the answer, in plain text, with no working, explanation, markdown, LaTeX, or restating of the question. There is no chance to ask for clarification, so if anything is ambiguous, give your best answer anyway.

Answer format:
- Number: only the number, with units if the question asks for them. Use the rounding or format the question specifies; otherwise use sensible precision.
- Multiple choice: only the position of the correct option as an ordinal (1st, 2nd, 3rd, ...), counting top to bottom, or left to right if laid out in a row. Count only the answer options, not the question text or any instructions. If more than one option is correct, list them comma-separated (e.g. 1st, 3rd).
- True/false: only True or False.
- Class, category, or label: only that label.
- Fill in the blank: only the missing word(s) or value(s). If there are several blanks, separate them with commas in order.
- Code question: only the raw code, or only the output if it asks what the code prints.
- Explanation or "why" question: one short clause per part asked, under 20 words total, keywords over full grammar. Example: "Which sorts are stable, and why isn't quicksort?" -> "Merge, insertion; quicksort swaps across the pivot, reordering equal keys."
- Anything else: the shortest phrase that fully answers it.

Hard limit: 25 words unless the answer is code or several numbered answers. Never add background, definitions, or worked examples.

If the screenshot contains several questions, answer each on its own line, numbered in the order they appear (e.g. "1. 42").
If the screenshot shows no question, reply with "No question found."

Read the screenshot carefully, including small text, units, and words like NOT, EXCEPT, or "select all". Work through the problem and check your answer before replying.

Your reply must end with a line starting "ANSWER:" followed by the final answer. Everything before that marker is thrown away, so put nothing after the answer. For several questions or code, the full answer goes after the marker, starting on the same line."""

ANSWER_MARKER = "ANSWER:"


def extract_answer(text: str) -> str:
    """Keep only what follows the last ANSWER: marker, if the model used one."""
    idx = text.rfind(ANSWER_MARKER)
    if idx == -1:
        return text.strip()
    return text[idx + len(ANSWER_MARKER) :].strip()


SYSTEM_PROMPT = """
You are a clipboard-based helper for a programmer and university student using Jupyter Notebooks.

Your reply is copied straight to the clipboard and shown in the macOS menu bar. There is no chance for follow-up, so never ask for clarification; make the most reasonable assumption and answer. The latest message is the current query; earlier turns are recent context only.

Rules:
- Plain text only: no markdown, no LaTeX.
- If the query asks for code or is code-related:
  - Provide ONLY the raw code, ready to paste into a Jupyter cell and run.
  - No comments unless absolutely critical for complex logic.
  - No explanations, intro, or outro text.
- If the query is purely conceptual (not code):
  - Respond with a short, direct explanation (max 3 sentences).
- When the user message includes its own answer-format instructions, follow those.
- Never include greetings, sign offs, or meta discussion.
"""

MODEL = "claude-haiku-5-5"  # text queries: fast and cheap
SCREENSHOT_MODEL = (
    "claude-sonnet-5-5"  # screenshots: more reliable on maths and reasoning
)
ENV_FILENAME = ".env"

TEXT_MAX_TOKENS = 1024
SCREENSHOT_MAX_TOKENS = 8192  # covers thinking + answer
SCREENSHOT_EFFORT: Optional[str] = (
    "high"  # "low", "medium" or "high"; None disables thinking
)
DEBUG_PRINT_THINKING = True  # print the model's thinking to the terminal

# Simple in-memory history: list of (user_text, model_reply)
history: List[Tuple[str, str]] = []


def _resolve_api_key() -> Optional[str]:
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return env_key.strip()

    file_path = Path(__file__).with_name(ENV_FILENAME)
    if file_path.exists():
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name.startswith("export "):
                name = name[7:].strip()
            if name == "ANTHROPIC_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if value:
                    return value

    return None


def get_clipboard() -> str:
    """Return current macOS clipboard text."""
    result = subprocess.run(
        ["pbpaste"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout or ""


def set_clipboard(text: str) -> None:
    """Set macOS clipboard text."""
    proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
    proc.communicate(text)


def get_screenshot_dir() -> Path:
    """Screenshot folder: $CLIPPER_SCREENSHOT_DIR, else macOS setting, else Desktop."""
    env_dir = os.getenv("CLIPPER_SCREENSHOT_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    result = subprocess.run(
        ["defaults", "read", "com.apple.screencapture", "location"],
        check=False,
        capture_output=True,
        text=True,
    )
    location = result.stdout.strip()
    if result.returncode == 0 and location:
        return Path(location).expanduser()

    return Path.home() / "Desktop"


def list_screenshots(directory: Path) -> Set[Path]:
    try:
        return {
            p
            for p in directory.iterdir()
            if p.suffix.lower() in SCREENSHOT_EXTENSIONS and not p.name.startswith(".")
        }
    except OSError:
        return set()


def history_char_count() -> int:
    return sum(len(u) + len(a) for (u, a) in history)


def trim_history() -> None:
    """Trim history to stay within size and length limits."""
    while history and (
        len(history) > HISTORY_MAX_EXCHANGES
        or history_char_count() > CONTEXT_CHAR_LIMIT
    ):
        history.pop(0)


def query_model(user_text: str, image: Optional[Tuple[bytes, str]] = None) -> str:
    """Send text (and optional image) with context to Claude and return its reply."""
    api_key = _resolve_api_key()
    if not api_key:
        print("Set ANTHROPIC_API_KEY or add it to .env next to clipper2.py.")
        return ""

    messages = []
    # Screenshots are self-contained; past answers would only bias the model
    for past_user, past_answer in [] if image else history:
        messages.extend(
            [
                {"role": "user", "content": past_user},
                {"role": "assistant", "content": past_answer},
            ]
        )

    user_content = []
    if image:
        data, mime_type = image
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    user_content.append({"type": "text", "text": user_text})
    messages.append({"role": "user", "content": user_content})

    body = {
        "model": SCREENSHOT_MODEL if image else MODEL,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }
    if image and SCREENSHOT_EFFORT:
        # Adaptive thinking improves accuracy on bare answers; it requires default temperature
        body["max_tokens"] = SCREENSHOT_MAX_TOKENS
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": SCREENSHOT_EFFORT}
    else:
        body["max_tokens"] = TEXT_MAX_TOKENS
        body["temperature"] = 0.2

    print(
        f"Using {body['model']}"
        + (f" (effort {SCREENSHOT_EFFORT})" if "thinking" in body else "")
    )
    payload = json.dumps(body).encode("utf-8")
    request = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"Claude API error ({e.code}): {detail}")
        return ""
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        print(f"Error calling Claude API: {e!r}")
        return ""

    if DEBUG_PRINT_THINKING:
        for block in result.get("content", []):
            if block.get("type") == "thinking" and block.get("thinking"):
                print("----- model thinking -----")
                print(block["thinking"])
                print("--------------------------")

    # Only text blocks go to the clipboard; thinking blocks are ignored
    reply = "\n".join(
        block["text"]
        for block in result.get("content", [])
        if block.get("type") == "text" and block.get("text")
    ).strip()
    if image:
        reply = extract_answer(reply)
    if not reply:
        if result.get("stop_reason") == "max_tokens":
            print("Hit max_tokens before an answer; raise SCREENSHOT_MAX_TOKENS.")
        else:
            print("No usable text found in Claude response.")
    return reply


class Watcher(threading.Thread):
    """
    Background thread polling the clipboard and screenshot folder.
    Queries run sequentially here; UI updates are posted to `events`
    and applied on the main thread by the menu bar app.
    """

    def __init__(self, events: "queue.Queue[Tuple[str, str]]", screenshot_dir: Path):
        super().__init__(daemon=True)
        self.events = events
        self.screenshot_dir = screenshot_dir
        self.watch_screenshots = threading.Event()
        self.watch_screenshots.set()
        self.history_lock = threading.Lock()
        self.last_response = ""

    def clear_history(self) -> None:
        with self.history_lock:
            history.clear()
        print("Conversation history cleared.")

    def ask(self, user_text: str, image: Optional[Tuple[bytes, str]] = None) -> None:
        self.events.put(("busy", ""))
        with self.history_lock:
            answer = query_model(user_text, image)
            if answer:
                # Screenshots are stored as text-only placeholders to keep context small
                history.append((user_text if not image else "[screenshot]", answer))
                trim_history()

        if answer:
            set_clipboard(answer)
            self.last_response = answer
            self.events.put(("answer", answer))
            print("Clipboard updated with model response.")
        else:
            self.events.put(("error", "Model returned empty response."))
            print("Model returned empty response, doing nothing.")

    def run(self) -> None:
        last_seen = get_clipboard()
        seen_screenshots = list_screenshots(self.screenshot_dir)

        while True:
            try:
                current = get_clipboard()
                if current != last_seen:
                    last_seen = current
                    self.handle_clipboard(current)
            except Exception as e:
                print(f"Error reading clipboard: {e}")

            if self.watch_screenshots.is_set():
                current_shots = list_screenshots(self.screenshot_dir)
                for path in sorted(current_shots - seen_screenshots):
                    self.handle_screenshot(path)
                seen_screenshots = current_shots
            else:
                # Don't process screenshots taken while paused once resumed
                seen_screenshots = list_screenshots(self.screenshot_dir)

            time.sleep(POLL_INTERVAL_SECONDS)

    def handle_clipboard(self, current: str) -> None:
        # Ignore our own last response to avoid loops
        if current == self.last_response:
            return

        user_text = current.strip()
        if not user_text.startswith(TRIGGER_PREFIX):
            return

        if user_text.lower() == f"{TRIGGER_PREFIX}clear":
            self.clear_history()
            set_clipboard("History cleared.")
            self.last_response = "History cleared."
            self.events.put(("cleared", ""))
            return

        query_text = user_text[len(TRIGGER_PREFIX) :].strip()
        if query_text:
            print("Trigger detected, querying model...")
            self.ask(query_text)

    def handle_screenshot(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as e:
            print(f"Error reading screenshot {path.name}: {e}")
            return
        if not data:
            return
        print(f"Screenshot detected ({path.name}), querying model...")
        self.ask(SCREENSHOT_PROMPT, (data, SCREENSHOT_EXTENSIONS[path.suffix.lower()]))


class ClipperApp(rumps.App):
    def __init__(self):
        super().__init__("Clipper", title=MENU_TITLE_IDLE, quit_button=None)
        self.events: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self.watcher = Watcher(self.events, get_screenshot_dir())
        self.answer = ""

        self.screenshot_toggle = rumps.MenuItem(
            "Watch screenshots", callback=self.toggle_screenshots
        )
        self.screenshot_toggle.state = 1
        self.render_menu()

        print(
            f"Clipper started. Copy text starting with '{TRIGGER_PREFIX}' or take a "
            f"screenshot (watching {self.watcher.screenshot_dir})."
        )
        self.watcher.start()

    def render_menu(self, status: str = "") -> None:
        self.menu.clear()
        items: List = []

        if self.answer:
            lines: List[str] = []
            for raw in self.answer.splitlines():
                lines.extend(textwrap.wrap(raw, MENU_WRAP_WIDTH) or [" "])
            if len(lines) > MENU_MAX_LINES:
                lines = lines[:MENU_MAX_LINES] + ["…"]
            # Every line copies the full reply when clicked
            items.extend(
                rumps.MenuItem(line, callback=self.copy_answer) for line in lines
            )
            items.append(None)
        if status:
            items.extend([rumps.MenuItem(status), None])

        items.extend(
            [
                self.screenshot_toggle,
                rumps.MenuItem("Clear history", callback=self.clear_history),
                None,
                rumps.MenuItem("Quit", callback=rumps.quit_application),
            ]
        )
        self.menu = items

    @rumps.timer(0.25)
    def drain_events(self, _) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return

            if kind == "busy":
                self.title = MENU_TITLE_BUSY
            elif kind == "answer":
                self.answer = payload
                first_line = payload.strip().splitlines()[0] if payload.strip() else ""
                snippet = first_line[:MENU_TITLE_CHARS]
                if len(first_line) > MENU_TITLE_CHARS or "\n" in payload.strip():
                    snippet += "…"
                self.title = f"🤖 {snippet}"
                self.render_menu()
            elif kind == "error":
                self.title = MENU_TITLE_IDLE
                self.render_menu(status=payload)
            elif kind == "cleared":
                self.answer = ""
                self.title = MENU_TITLE_IDLE
                self.render_menu(status="History cleared.")

    def copy_answer(self, _) -> None:
        if self.answer:
            set_clipboard(self.answer)
            self.watcher.last_response = self.answer

    def toggle_screenshots(self, sender: rumps.MenuItem) -> None:
        sender.state = not sender.state
        if sender.state:
            self.watcher.watch_screenshots.set()
        else:
            self.watcher.watch_screenshots.clear()

    def clear_history(self, _) -> None:
        self.watcher.clear_history()
        self.answer = ""
        self.title = MENU_TITLE_IDLE
        self.render_menu(status="History cleared.")


if __name__ == "__main__":
    ClipperApp().run()
