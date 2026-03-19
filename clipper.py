#!/usr/bin/env python3
"""
Clipboard watcher for macOS with small conversational context.

- Polls the clipboard every second
- When it changes, checks if it starts with "#"
- If so, sends the text (minus prefix) to Gemini
- Copies the model reply back to the clipboard
"""

import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from google import genai
from google.genai import types

POLL_INTERVAL_SECONDS = 1.0
CONTEXT_CHAR_LIMIT = 4000  # rough cap on total chars of history
HISTORY_MAX_EXCHANGES = 5  # max past Q/A pairs to keep

SYSTEM_PROMPT = """
You are a clipboard based helper for a programmer and university student using Jupyter Notebooks.

Rules:
- The clipboard text is the user's current query. Previous turns you see are recent context only.
- If the query asks for code or is code-related:
  - Provide ONLY the raw code.
  - NO markdown formatting (no backticks, no language headers).
  - NO comments in the code unless absolutely critical for complex logic.
  - NO explanations, intro, or outro text.
  - The output must be ready to paste directly into a Jupyter cell and run immediately.
- If the query is purely conceptual (not code):
  - Respond with a short, direct explanation (max 3 sentences).
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


def history_char_count() -> int:
    return sum(len(u) + len(a) for (u, a) in history)


def trim_history() -> None:
    """Trim history to stay within size and length limits."""
    while history and (
        len(history) > HISTORY_MAX_EXCHANGES
        or history_char_count() > CONTEXT_CHAR_LIMIT
    ):
        history.pop(0)


def build_contents(user_text: str):
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

    contents.append(
        types.Content(
            role="user",
            parts=[types.Part(text=user_text)],
        )
    )

    return contents


def query_model(user_text: str) -> str:
    """Send text (with context) to Gemini and return the response text."""
    contents = build_contents(user_text)

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


def main() -> None:
    print(
        "Clipboard watcher started. Copy text starting with '#' to trigger. Press Ctrl+C to stop."
    )
    last_seen = get_clipboard()
    last_response = ""

    while True:
        try:
            current = get_clipboard()
        except Exception as e:
            print(f"Error reading clipboard: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if current != last_seen:
            # Ignore our own last response to avoid loops
            if current == last_response:
                last_seen = current
            else:
                user_text = current.strip()
                if user_text.startswith("#"):
                    if user_text.strip().lower() == "#clear":
                        history.clear()
                        set_clipboard("History cleared.")
                        last_response = "History cleared."
                        last_seen = "History cleared."
                        print("Conversation history cleared.")
                    else:
                        print("Trigger detected, querying model...")
                        query_text = user_text[2:].strip()
                        if query_text:
                            answer = query_model(query_text)
                            if answer:
                                # Update history with this new turn
                                history.append((query_text, answer))
                                trim_history()

                                set_clipboard(answer)
                                last_response = answer
                                last_seen = answer
                                print("Clipboard updated with model response.")
                            else:
                                last_seen = current
                                print("Model returned empty response, doing nothing.")
                        else:
                            last_seen = current
                else:
                    last_seen = current

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
