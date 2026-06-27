"""
Prompt Pipeline — Support Ticket Triage
========================================
Stages:
  1. UNDERSTAND  (role + structured output) — extract facts from raw ticket
  2. REASON      (chain-of-thought)          — decide priority / route with reasoning
  3. PRODUCE     (goal-oriented + constraints) — draft a polished reply

Each stage returns JSON consumed by the next.  Includes parse-with-retry for
malformed JSON and graceful handling of broken inputs.
"""

import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("Installing requests ...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

# ──────────────────────────────────────────────────────────────────────
# 1.  LLM caller (OpenRouter)
# ──────────────────────────────────────────────────────────────────────

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    # fallback: read from .env in same directory
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

MODEL = "openai/gpt-4o-mini"  # cheap & fast for prompting experiments

def call_llm(prompt: str, system: str = "") -> str:
    """Single LLM call via OpenRouter.  Returns raw text (hopefully JSON)."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,       # low → deterministic JSON
        "max_tokens": 1024,
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return text
        except Exception as e:
            print(f"  [LLM call failed (attempt {attempt+1}/3): {e}]")
            time.sleep(2 ** attempt)  # exponential back-off
    print("  [LLM: all retries exhausted, returning empty]")
    return ""

# ──────────────────────────────────────────────────────────────────────
# 2.  JSON parser with retry
# ──────────────────────────────────────────────────────────────────────

def parse_json(raw: str, stage_name: str, retries: int = 2) -> dict:
    """
    Parse JSON from LLM output.  If it fails, show the error back to the
    model and ask it to fix the JSON — this is the structured-output retry.
    """
    for attempt in range(retries + 1):
        # Try to extract a JSON block from markdown fences first
        text = raw.strip()
        # Remove markdown code fences if present
        if "```json" in text:
            text = text.split("```json", 1)[1]
            if "```" in text:
                text = text.split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1]
            if "```" in text:
                text = text.split("```", 1)[0]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  [⚠ {stage_name}: JSON parse failed — {e}]")
            if attempt < retries:
                fix_prompt = (
                    f"Your previous output was not valid JSON.\n"
                    f"Error: {e}\n\n"
                    f"Your previous output:\n{raw}\n\n"
                    f"Please return ONLY valid JSON — no markdown, no extra text."
                )
                raw = call_llm(fix_prompt)
            else:
                print(f"  [✗ {stage_name}: giving up after {retries} retries]")
                return {"_error": f"JSON parse failed: {e}", "_raw": raw}
    return {}

# ──────────────────────────────────────────────────────────────────────
# 3.  Stage Prompts
# ──────────────────────────────────────────────────────────────────────

STAGE1_SYSTEM = (
    "You are a precise ticket analyst. "
    "Extract structured information from raw customer messages. "
    "Respond ONLY with valid JSON — no markdown, no commentary."
)

STAGE1_PROMPT = """Extract the following fields from this customer support ticket.
Return a JSON object with these keys:
- "customer_name": the customer's name or "Unknown"
- "order_id": any order/reference number or "N/A"
- "issue_category": one of ["delivery", "product_quality", "billing", "account", "other"]
- "issue_summary": one-sentence summary of the problem
- "days_waiting": number of days the customer has been waiting (0 if not mentioned)
- "sentiment": one of ["frustrated", "neutral", "satisfied", "urgent"]

Ticket:
---
{text}
---

Return ONLY the JSON object."""

STAGE2_SYSTEM = (
    "You are a logical reasoning engine. "
    "Think step by step before deciding. "
    "Respond ONLY with valid JSON."
)

STAGE2_PROMPT = """You are triaging a support ticket.  Here is the structured summary from the analyst:

{stage1_json}

Think step by step:

1. How urgent is this issue?  Consider sentiment, days waiting, and issue category.
2. What team should handle it?  Match the category to a team.
3. What is the risk of delay?

Then return this exact JSON structure:
{{
  "chain_of_thought": "your step-by-step reasoning here",
  "priority": "P1" or "P2" or "P3",
  "assigned_team": "one of [support, billing, logistics, quality, engineering]",
  "sla_hours": number of hours within which this must be responded to,
  "risk": "high" or "medium" or "low"
}}

Return ONLY the JSON object — no markdown, no extra text."""

STAGE3_SYSTEM = (
    "You are a professional support writer. "
    "Write concise, empathetic, on-brand replies. "
    "Return ONLY valid JSON."
)

STAGE3_PROMPT = """Draft a customer support reply based on the following analysis.

Ticket summary:
{stage1_json}

Triage decision:
{stage2_json}

Constraints:
- Be empathetic and professional
- Under 120 words
- Address the specific issue
- Do NOT make promises about timelines or refunds unless confirmed
- Include a clear next step for the customer

Return this exact JSON:
{{
  "reply": "your drafted reply here",
  "word_count": number of words in the reply,
  "tone": "empathetic" or "professional" or "urgent"
}}

Return ONLY the JSON object — no markdown, no extra text."""

# ──────────────────────────────────────────────────────────────────────
# 4.  Stage functions
# ──────────────────────────────────────────────────────────────────────

def stage1_understand(text: str) -> dict:
    """Role + structured output: extract ticket facts."""
    print("\n" + "=" * 60)
    print("STAGE 1 — UNDERSTAND (role + structured output)")
    print("=" * 60)
    prompt = STAGE1_PROMPT.format(text=text)
    raw = call_llm(prompt, system=STAGE1_SYSTEM)
    result = parse_json(raw, "Stage 1")
    print(json.dumps(result, indent=2))
    return result


def stage2_reason(brief: dict) -> dict:
    """Chain-of-thought: decide priority, route, risk."""
    print("\n" + "=" * 60)
    print("STAGE 2 — REASON (chain-of-thought)")
    print("=" * 60)
    prompt = STAGE2_PROMPT.format(stage1_json=json.dumps(brief, indent=2))
    raw = call_llm(prompt, system=STAGE2_SYSTEM)
    result = parse_json(raw, "Stage 2")
    print(json.dumps(result, indent=2))
    return result


def stage3_produce(brief: dict, decision: dict) -> dict:
    """Goal-oriented + constraints: write the reply."""
    print("\n" + "=" * 60)
    print("STAGE 3 — PRODUCE (goal-oriented + constraints)")
    print("=" * 60)
    prompt = STAGE3_PROMPT.format(
        stage1_json=json.dumps(brief, indent=2),
        stage2_json=json.dumps(decision, indent=2),
    )
    raw = call_llm(prompt, system=STAGE3_SYSTEM)
    result = parse_json(raw, "Stage 3")
    print(json.dumps(result, indent=2))
    return result


# ──────────────────────────────────────────────────────────────────────
# 5.  Runner
# ──────────────────────────────────────────────────────────────────────

def show(label: str, data):
    """Pretty-print a stage result."""
    print(f"\n── {label} ──")
    if isinstance(data, dict):
        print(json.dumps(data, indent=2))
    else:
        print(data)


def run(text: str) -> dict:
    """Chain the stages and print every step."""
    print("\n" + "#" * 60)
    print(f"# INPUT: {text[:80]}{'…' if len(text) > 80 else ''}")
    print("#" * 60)

    brief = stage1_understand(text)
    if "_error" in brief:
        print("\n[✗ Pipeline aborted: Stage 1 failed]")
        return brief

    decision = stage2_reason(brief)
    if "_error" in decision:
        print("\n[✗ Pipeline aborted: Stage 2 failed]")
        return {"brief": brief, "error": decision}

    output = stage3_produce(brief, decision)
    if "_error" in output:
        print("\n[✗ Pipeline aborted: Stage 3 failed]")
        return {"brief": brief, "decision": decision, "error": output}

    print("\n" + "=" * 60)
    print("FINAL OUTPUT")
    print("=" * 60)
    print(json.dumps(output, indent=2))

    return {"brief": brief, "decision": decision, "output": output}


# ──────────────────────────────────────────────────────────────────────
# 6.  Test cases
# ──────────────────────────────────────────────────────────────────────

TEST_CASES = [
    # --- Normal case 1: delivery issue ---
    (
        "Hi, I ordered a laptop bag on June 10th (order #ORD-4421) and it was "
        "supposed to arrive by June 15th. It's now June 20th and tracking shows "
        "it's still in the warehouse. I need this for a work trip! Can someone "
        "please help? - Sarah",
        "Normal: delivery delay, frustrated customer"
    ),
    # --- Normal case 2: product quality ---
    (
        "I received my order #BBQ-9901 yesterday. The blender arrived with a "
        "cracked jar and the motor makes a grinding noise when I turn it on. "
        "I'd like a replacement or refund. Thanks, Mark",
        "Normal: product defect, neutral tone"
    ),
    # --- Normal case 3: billing question ---
    (
        "Hello support team, I was charged twice for my subscription this month. "
        "My account email is jane@example.com. Can you look into this? "
        "Best, Jane",
        "Normal: billing duplicate charge"
    ),
    # --- Tricky / broken input ---
    (
        "asdlkfj 12345 !@#$%^&*() ??? ??? ORDER???  ",
        "Broken: gibberish / near-empty input — should still produce a structured ticket"
    ),
]


# ──────────────────────────────────────────────────────────────────────
# 7.  Main
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  PROMPT PIPELINE — Support Ticket Triage")
    print("  Model:", MODEL)
    print("=" * 60)

    results = []
    for text, description in TEST_CASES:
        print(f"\n\n{'#' * 60}")
        print(f"# TEST: {description}")
        print(f"{'#' * 60}")
        result = run(text)
        results.append(result)
        print(f"\n{'─' * 60}")

    # --- Reflection (weakest link) ---
    print("\n\n")
    print("=" * 60)
    print("REFLECTION — WEAKEST LINK")
    print("=" * 60)
    reflection = """
The weakest stage is Stage 1 (Understand).  It depends entirely on the LLM correctly
identifying fields like "days_waiting" and "sentiment" from often ambiguous text —
especially when customers express frustration indirectly or mix multiple issues.  A
misclassification here cascades into wrong priority in Stage 2 and an off-tone reply
in Stage 3.

With a retrieval tool (Day 4), I could pull past resolved tickets matching the
issue_category to ground the analysis.  With a structured extraction tool (Day 6+),
I could call a dedicated NER service or a small fine-tuned extractor model rather
than relying on the general-purpose LLM to guess numeric fields like days_waiting.
"""
    print(reflection.strip())
    print("=" * 60)