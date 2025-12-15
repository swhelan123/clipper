# Clipper 📋🤖

A macOS clipboard watcher that automatically queries Google's Gemini AI with whatever you copy, replacing your clipboard with the AI's response.

## ⚠️ Disclaimer

This tool is designed for learning and productivity. **I am not responsible if this tool is used to cheat in exams or violate academic integrity policies.** Use it ethically and in accordance with your institution's policies.

## What It Does

Clipper runs in the background and monitors your macOS clipboard. When it detects new text that starts with `??` (double question mark trigger), it:

1. **Strips the trigger and sends the rest to Gemini AI** along with recent conversation history for context
2. **Gets an intelligent response** tailored for programmers and students
3. **Replaces your clipboard** with the AI's answer
4. **Maintains context** across multiple clipboard operations

Perfect for quick code fixes, concept explanations, or formatting snippets without leaving your workflow.

## Features

- 🔄 **Automatic clipboard monitoring** - polls every second
- 💬 **Conversational context** - remembers recent exchanges (up to 5 Q&A pairs)
- 🎯 **Smart response formatting**:
  - Code queries → clean code blocks only
  - Questions → concise 3-sentence explanations
- 🚫 **Loop prevention** - won't process its own responses
- 🧠 **Optimized for CS/Math students** - understands technical queries

## Prerequisites

- **macOS** (uses `pbpaste` and `pbcopy`)
- **Python 3.7+**
- **Google Gemini API key**

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/yourusername/clipper.git
   cd clipper
   ```

2. **Install dependencies:**

   ```bash
   pip install google-genai
   ```

3. **Set up your API key:**

   Get a free API key from [Google AI Studio](https://aistudio.google.com/app/apikey), then set it as an environment variable:

   ```bash
   export GEMINI_API_KEY="your-api-key-here"
   ```

   Or add it to your `~/.zshrc` or `~/.bash_profile` to persist across sessions:

   ```bash
   echo 'export GEMINI_API_KEY="your-api-key-here"' >> ~/.zshrc
   source ~/.zshrc
   ```

   Prefer a file-based setup? Copy `GEMINI_API_KEY.txt.example` to `GEMINI_API_KEY.txt` (next to `clipper.py`) and paste your key inside. The template stays committed while the real file is ignored, so collaborators can drop in their own keys after cloning. Clipper automatically loads whichever option you configure.

## Usage

1. **Start the watcher:**

   ```bash
   python3 clipper.py
   ```

2. **Copy something that begins with `??`** (⌘C) - for example, `?? fix this python loop` followed by buggy code. Anything without the trigger is ignored so you can keep normal clipboard usage.

3. **Paste** (⌘V) - you'll get the AI's response instead!

**Example workflow:**

- Copy: `?? fix this python loop` followed by buggy code
- Paste: Get corrected code ready to use
- Copy: `?? what is a linked list`
- Paste: Get a concise explanation

## How It Works

```
┌─────────────┐
│ Copy Text   │
└──────┬──────┘
       │
       ▼
┌─────────────────────┐
│ Clipper Detects     │
│ Change              │
└──────┬──────────────┘
       │
       ▼
┌─────────────────────┐
│ Send to Gemini AI   │
│ (with context)      │
└──────┬──────────────┘
       │
       ▼
┌─────────────────────┐
│ Response Replaces   │
│ Clipboard           │
└─────────────────────┘
```

## Configuration

You can adjust these constants in `clipper.py`:

- `POLL_INTERVAL_SECONDS`: How often to check clipboard (default: 1.0)
- `CONTEXT_CHAR_LIMIT`: Max characters of history to maintain (default: 4000)
- `HISTORY_MAX_EXCHANGES`: Max Q&A pairs to remember (default: 5)

## System Prompt

Clipper uses a specialized system prompt that:

- Provides code-only responses when you need code
- Gives brief explanations for conceptual questions
- Responds in the same language as your query
- Skips pleasantries and gets straight to the answer

## Stopping the Watcher

Press `Ctrl+C` in the terminal where Clipper is running.

## Tips

- **Code fixes**: Copy broken code with a brief description at the top
- **Quick lookups**: Copy technical terms or function names
- **Format conversion**: Copy data and specify the desired format
- **Context matters**: Recent clipboard operations inform current responses

## Limitations

- macOS only (Linux/Windows would need different clipboard commands)
- Requires active internet connection
- API rate limits apply (Gemini free tier is generous)
- Not suitable for very large code files (stays under context limits)

## License

MIT License - feel free to modify and distribute.

## Contributing

Issues and pull requests welcome! Potential improvements:

- Cross-platform clipboard support
- GUI for starting/stopping
- Custom system prompts
- Token usage tracking
- Whitelist/blacklist for apps

---

**Remember:** Use this tool responsibly. Academic integrity matters. This is a learning aid, not a shortcut to bypass understanding.
