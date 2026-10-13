#!/usr/bin/env python3
"""
Clipboard + screenshot watcher for macOS, living in the menu bar.

- Polls the clipboard every second
- When it changes, checks if it starts with "#"
- If so, sends the text (minus prefix) to Gemini and copies the reply back
- Watches the screenshot folder for new screenshots
- New screenshots are sent to Gemini and the reply is copied to the clipboard
- Latest reply is shown in the menu bar (click a line to copy the full reply)
"""

import os
import queue
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import rumps
from google import genai
from google.genai import types

POLL_INTERVAL_SECONDS = 1.0
CONTEXT_CHAR_LIMIT = 4000  # rough cap on total chars of history
HISTORY_MAX_EXCHANGES = 5  # max past Q/A pairs to keep
TRIGGER_PREFIX = "#"

MENU_TITLE_IDLE = "📋"
MENU_TITLE_BUSY = "⏳"
MENU_TITLE_CHARS = 40  # chars of the reply shown directly in the menu bar
MENU_WRAP_WIDTH = 70  # chars per line in the dropdown
MENU_MAX_LINES = 40  # cap on dropdown lines for long replies

SCREENSHOT_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
SCREENSHOT_PROMPT = "Screenshot attached. Answer the question shown in it, as concisely as possible."

SYSTEM_PROMPT = """
You are a clipboard based helper for a programmer and university student using Jupyter Notebooks.

Rules:
- The clipboard text or screenshot is the user's current query. Previous turns you see are recent context only.
- If the query asks for code or is code-related:
  - Provide ONLY the raw code.
  - NO markdown formatting (no backticks, no language headers).
  - NO comments in the code unless absolutely critical for complex logic.
  - NO explanations, intro, or outro text.
  - The output must be ready to paste directly into a Jupyter cell and run immediately.
- If the query is purely conceptual (not code):
  - Respond with a short, direct explanation (max 3 sentences).
- If the query is a screenshot, answer as concisely as possible, overriding the rules above:
  - If the answer is a number, give ONLY the number (with units if the question requires them).
  - If the answer is a class, category, or label, give ONLY that class.
  - If it is multiple choice, give ONLY the position of the correct option as an ordinal: 1st, 2nd, 3rd, 4th, etc.
    For multiple correct options, list them comma-separated (e.g. 1st, 3rd).
  - Otherwise give the shortest answer that fully answers the question (a word or short phrase if possible).
  - No working, no explanation, no restating the question.
- Never include greetings, sign offs, or meta discussion.
"""

API_KEY_FILENAME = "GEMINI_API_KEY.txt"
_client: Optional[genai.Client] = None

# Simple in-memory history: list of (user_text, model_reply)
history: List[Tuple[str, str]] = []


def _resolve_api_key() -> Optional[str]:
    env_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if env_key:
        return env_key.strip()

    file_path = Path(__file__).with_name(API_KEY_FILENAME)
    if file_path.exists():
        key = file_path.read_text(encoding="utf-8").strip()
        if key:
            return key

    return None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "Set GEMINI_API_KEY/GOOGLE_API_KEY or create GEMINI_API_KEY.txt with your key."
            )
        _client = genai.Client(api_key=api_key)
    return _client


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


def build_contents(user_text: str, image: Optional[Tuple[bytes, str]] = None):
    """
    Build the contents list for generate_content:
    interleave past user/model turns, then the new user turn.
    """
    contents = []

    for u, a in history:
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part(text=u)],
            )
        )
        contents.append(
            types.Content(
                role="model",
                parts=[types.Part(text=a)],
            )
        )

    parts = []
    if image:
        data, mime_type = image
        parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    parts.append(types.Part(text=user_text))
    contents.append(types.Content(role="user", parts=parts))

    return contents


def query_model(user_text: str, image: Optional[Tuple[bytes, str]] = None) -> str:
    """Send text (and optional image) with context to Gemini and return the response text."""
    contents = build_contents(user_text, image)

    try:
        client = get_client()
    except RuntimeError as e:
        print(e)
        return ""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=1024,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0  # disable hidden thinking so it must emit visible text
                ),
            ),
        )
    except Exception as e:
        print(f"Error from generate_content: {e!r}")
        return ""

    # Preferred path
    if getattr(response, "text", None):
        return response.text.strip()

    # Fallback: try to reconstruct from candidates/parts
    try:
        parts_text = []
        for cand in getattr(response, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if content and getattr(content, "parts", None):
                for part in content.parts:
                    t = getattr(part, "text", None)
                    if t:
                        parts_text.append(t)
        if parts_text:
            return "\n".join(parts_text).strip()
    except Exception as e:
        print(f"Error parsing response candidates: {e!r}")

    print("No usable text found in response.")
    return ""


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

        query_text = user_text[len(TRIGGER_PREFIX):].strip()
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
            items.extend(rumps.MenuItem(line, callback=self.copy_answer) for line in lines)
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
