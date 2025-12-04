#!/usr/bin/env python3
"""
Clipboard watcher for macOS with small conversational context.

- Polls the clipboard every second
- When it changes, sends the new text (plus recent history) to Gemini
- Copies the model reply back to the clipboard
"""

import subprocess
import time
from typing import List, Tuple

from google import genai
from google.genai import types

POLL_INTERVAL_SECONDS = 1.0
CONTEXT_CHAR_LIMIT = 4000        # rough cap on total chars of history
HISTORY_MAX_EXCHANGES = 5        # max past Q/A pairs to keep

SYSTEM_PROMPT = """
You are a clipboard based helper for a programmer and university student.

Rules:
- The clipboard text is the user's current query. Previous turns you see are recent context only; use them when obviously relevant.
- Answer in the same language as the query.
- If the query explicitly asks for code, or is clearly code that needs completing, fixing, or rewriting:
  - Respond with code only, in a single fenced code block, in the requested language.
  - No explanations, no comments, no extra prose.
- Otherwise respond with a short, direct explanation, at most three sentences. Use a short bullet list only if it clearly helps.
- Never include greetings, sign offs, or meta discussion about being an AI.
- If the query is ambiguous, choose the most likely meaning for a CS / maths student and answer that directly.
"""

client = genai.Client()  # uses GEMINI_API_KEY or GOOGLE_API_KEY from env

# Simple in-memory history: list of (user_text, model_reply)
history: List[Tuple[str, str]] = []


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
    print("Clipboard watcher started. Press Ctrl+C to stop.")
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
                if user_text:
                    print("New clipboard text detected, querying model...")
                    answer = query_model(user_text)
                    if answer:
                        # Update history with this new turn
                        history.append((user_text, answer))
                        trim_history()

                        set_clipboard(answer)
                        last_response = answer
                        last_seen = answer
                        print("Clipboard updated with model response.")
                    else:
                        last_seen = current
                        print("Model returned empty response, doing nothing.")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
