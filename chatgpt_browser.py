#!/usr/bin/env python3
"""
chatgpt_browser.py
==================
Automates ChatGPT in a browser to analyse your trading session.

Flow:
  1. Finds the latest AI_Summary_*.md from logs/analysis/
  2. Opens ChatGPT in Chromium (headed, so you can watch + stay logged in)
  3. Pastes the markdown as a structured prompt
  4. Waits for the full response
  5. Saves the response to logs/analysis/ChatGPT_Browser_<ts>.md

Usage:
  .venv/bin/python3 chatgpt_browser.py                   # uses latest summary
  .venv/bin/python3 chatgpt_browser.py path/to/file.md   # specific file

Can also be called from ANALYZE_BOT.py via run_chatgpt_browser(md_path).
"""

from __future__ import annotations
import os, sys, re, time
from datetime import datetime
from pathlib import Path

BASE     = os.path.dirname(os.path.abspath(__file__))
OUT_DIR  = os.path.join(BASE, "logs", "analysis")
os.makedirs(OUT_DIR, exist_ok=True)

CHATGPT_URL = "https://chatgpt.com/"

# ── Prompt wrapper sent to ChatGPT ───────────────────────────────────────────
PROMPT_PREFIX = """You are an expert Indian intraday options trading analyst.

Analyse the trading session data below and give me:
1. What actually happened this session (2-3 honest sentences)
2. What worked well — specific trades/times
3. Root cause of each loss > ₹5,000 — what specifically went wrong
4. Structural issues I should fix in the bot
5. Most important single improvement to make

Be specific. Reference actual trade times and P&L numbers from the data. No generic advice.

---SESSION DATA---
"""

PROMPT_SUFFIX = "\n---END DATA---\n\nGive me the full analysis now."


# ── Find latest summary file ─────────────────────────────────────────────────
def find_latest_summary() -> str | None:
    files = sorted(Path(OUT_DIR).glob("AI_Summary_*.md"), reverse=True)
    return str(files[0]) if files else None


# ── Main browser automation ───────────────────────────────────────────────────
def run_chatgpt_browser(md_path: str | None = None) -> str | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("❌  playwright not installed. Run: .venv/bin/pip install playwright && .venv/bin/playwright install chromium")
        return None

    if not md_path:
        md_path = find_latest_summary()
    if not md_path or not os.path.exists(md_path):
        print(f"❌  No AI_Summary_*.md found in {OUT_DIR}")
        print("    Run ANALYZE_BOT.py first to generate the summary.")
        return None

    with open(md_path, encoding="utf-8") as f:
        md_content = f.read()

    full_prompt = PROMPT_PREFIX + md_content + PROMPT_SUFFIX
    print(f"\n📄  Using summary: {os.path.basename(md_path)}")
    print(f"    Prompt length: {len(full_prompt):,} chars\n")

    with sync_playwright() as pw:
        # Launch headed Chromium with a persistent profile so you stay logged in
        profile_dir = os.path.join(BASE, ".chatgpt_profile")
        os.makedirs(profile_dir, exist_ok=True)

        print("🌐  Opening ChatGPT browser...")
        context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = context.new_page() if context.pages == [] else context.pages[0]

        # ── Navigate to ChatGPT ───────────────────────────────────────────
        print("    Navigating to chatgpt.com ...")
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)

        # ── Handle login wall ─────────────────────────────────────────────
        # If not logged in, wait up to 90s for user to log in manually
        login_needed = False
        try:
            page.wait_for_selector(
                'div[contenteditable="true"], textarea#prompt-textarea, div#prompt-textarea',
                timeout=8_000,
            )
        except PWTimeout:
            login_needed = True

        if login_needed:
            print("\n  ⚠️  ChatGPT login required.")
            print("  👉  Please log in inside the browser window that just opened.")
            print("  ⏳  Waiting up to 90 seconds for you to log in...\n")
            try:
                page.wait_for_selector(
                    'div[contenteditable="true"], textarea#prompt-textarea, div#prompt-textarea',
                    timeout=90_000,
                )
                print("  ✅  Logged in!\n")
            except PWTimeout:
                print("  ❌  Login timeout. Close the browser and try again.")
                context.close()
                return None

        # ── Start a new chat ──────────────────────────────────────────────
        print("    Starting new chat...")
        try:
            # Click "New chat" button if visible
            new_chat = page.query_selector('a[href="/"], button[aria-label*="New chat"]')
            if new_chat:
                new_chat.click()
                time.sleep(1.5)
        except Exception:
            pass

        # ── Find the input box ────────────────────────────────────────────
        input_sel = 'div[contenteditable="true"][data-placeholder], div#prompt-textarea, textarea#prompt-textarea'
        try:
            page.wait_for_selector(input_sel, timeout=15_000)
        except PWTimeout:
            print("  ❌  Could not find ChatGPT input box. UI may have changed.")
            context.close()
            return None

        input_box = page.query_selector(input_sel)
        if not input_box:
            print("  ❌  Input box not found.")
            context.close()
            return None

        # ── Paste the prompt via clipboard ────────────────────────────────
        print("    Pasting prompt into ChatGPT...")
        input_box.click()
        time.sleep(0.5)

        # Use clipboard for reliable large-text paste
        try:
            import pyperclip
            pyperclip.copy(full_prompt)
            if sys.platform == "darwin":
                page.keyboard.press("Meta+v")
            else:
                page.keyboard.press("Control+v")
        except ImportError:
            # Fallback: type directly (slower but works without pyperclip)
            print("    (pyperclip not available, typing directly — may be slow)")
            input_box.fill(full_prompt)

        time.sleep(1.5)

        # ── Submit ────────────────────────────────────────────────────────
        print("    Submitting...")
        page.keyboard.press("Enter")

        # ── Wait for response to complete ─────────────────────────────────
        print("    Waiting for ChatGPT response", end="", flush=True)

        # Wait for the stop/regenerate button to appear (response started)
        try:
            page.wait_for_selector(
                'button[aria-label*="Stop"], button[data-testid="stop-button"]',
                timeout=20_000,
            )
        except PWTimeout:
            pass  # some UI versions don't show stop button immediately

        # Wait for response to finish (stop button disappears)
        max_wait = 180  # 3 min
        elapsed  = 0
        interval = 3
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            print(".", end="", flush=True)
            stop_btn = page.query_selector(
                'button[aria-label*="Stop"], button[data-testid="stop-button"]'
            )
            if not stop_btn:
                break
        print(" done\n")

        # ── Extract response text ─────────────────────────────────────────
        print("    Extracting response...")
        response_text = ""
        try:
            # Get all assistant message blocks
            msgs = page.query_selector_all(
                'div[data-message-author-role="assistant"] .markdown, '
                'div[data-message-author-role="assistant"] p, '
                '[data-testid*="conversation-turn"] .markdown'
            )
            if msgs:
                # Take the last (most recent) response
                response_text = msgs[-1].inner_text()
            else:
                # Fallback: grab all visible text in the conversation
                conv = page.query_selector('main')
                response_text = conv.inner_text() if conv else "Could not extract response."
        except Exception as e:
            response_text = f"Extraction error: {e}"

        # ── Save to file ──────────────────────────────────────────────────
        ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out  = os.path.join(OUT_DIR, f"ChatGPT_Browser_{ts}.md")
        header = (
            f"# ChatGPT Deep Analysis (Browser)\n\n"
            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
            f"**Source:** {os.path.basename(md_path)}\n\n---\n\n"
        )
        with open(out, "w", encoding="utf-8") as f:
            f.write(header + response_text)

        print(f"✅  Response saved → {out}")
        print("\n📋  Response preview:")
        print("─" * 70)
        preview = response_text[:1200]
        print(preview)
        if len(response_text) > 1200:
            print(f"\n  ... [{len(response_text) - 1200:,} more chars in file]")
        print("─" * 70)

        print("\n  Browser will close in 10 seconds. Press Ctrl+C to keep it open.")
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            print("  Keeping browser open. Close it manually when done.")
            input("  Press Enter to exit script...")

        context.close()
        return out


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    md_path = sys.argv[1] if len(sys.argv) > 1 else None
    result  = run_chatgpt_browser(md_path)
    if not result:
        sys.exit(1)
