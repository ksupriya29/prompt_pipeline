"""
Prompt Pipeline Engine — Support Ticket Triage
===============================================
Stages:
  1. UNDERSTAND  (role + structured output) — extract facts from raw ticket
  2. REASON      (chain-of-thought)          — decide priority / route with reasoning
  3. PRODUCE     (goal-oriented + constraints) — draft a polished reply

Each stage returns JSON consumed by the next.  Includes parse-with-retry for
malformed JSON and graceful handling of broken inputs.
"""

import json
import os
import time

try:
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ──────────────────────────────────────────────────────────────────────
# 1.  LLM caller (OpenRouter)
# ──────────────────────────────────────────────────────────────────────

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

MODEL = "openai/gpt-4o-mini"

def call_llm(prompt: str, system: str = "") -> str:
    """Single LLM call via OpenRouter. Returns raw text (hopefully JSON)."""
    if not API_KEY:
        return json.dumps({"_error": "No API key found"})
    
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
        "temperature": 0.2,
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
            time.sleep(2 ** attempt)
    return json.dumps({"_error": "LLM call failed after 3 retries"})

# ──────────────────────────────────────────────────────────────────────
# 2.  JSON parser with retry
# ──────────────────────────────────────────────────────────────────────

def parse_json(raw: str, stage_name: str, retries: int = 2) -> dict:
    """Parse JSON from LLM output with retry on failure."""
    for attempt in range(retries + 1):
        text = raw.strip()

        # Remove markdown code fences
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
            print(f"  [JSON parse failed: {e}]")
            if attempt < retries:
                fix_prompt = (
                    f"Your previous output was not valid JSON.\n"
                    f"Error: {e}\n\n"
                    f"Your previous output:\n{raw}\n\n"
                    f"Please return ONLY valid JSON — no markdown, no extra text."
                )
                raw = call_llm(fix_prompt)
            else:
                return {"_error": f"JSON parse failed: {e}"}
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
# 4.  Stage functions — each returns (dict, raw_llm_output)
# ──────────────────────────────────────────────────────────────────────

def stage1_understand(text: str):
    prompt = STAGE1_PROMPT.format(text=text)
    raw = call_llm(prompt, system=STAGE1_SYSTEM)
    result = parse_json(raw, "Stage 1")
    return result, raw


def stage2_reason(brief: dict):
    prompt = STAGE2_PROMPT.format(stage1_json=json.dumps(brief, indent=2))
    raw = call_llm(prompt, system=STAGE2_SYSTEM)
    result = parse_json(raw, "Stage 2")
    return result, raw


def stage3_produce(brief: dict, decision: dict):
    prompt = STAGE3_PROMPT.format(
        stage1_json=json.dumps(brief, indent=2),
        stage2_json=json.dumps(decision, indent=2),
    )
    raw = call_llm(prompt, system=STAGE3_SYSTEM)
    result = parse_json(raw, "Stage 3")
    return result, raw


# ──────────────────────────────────────────────────────────────────────
# 5.  Main pipeline runner
# ──────────────────────────────────────────────────────────────────────

def run_pipeline(text: str) -> dict:
    """
    Run the full 3-stage pipeline.
    Returns a dict with all stage results and final output.
    """
    pipeline_run = {
        "input": text,
        "stages": [],
        "output": None,
        "error": None,
    }

    # Stage 1
    brief, raw1 = stage1_understand(text)
    pipeline_run["stages"].append({
        "name": "Stage 1 — UNDERSTAND",
        "technique": "Role + Structured Output",
        "input": text,
        "raw_llm_output": raw1,
        "parsed_output": brief,
    })
    if "_error" in brief:
        pipeline_run["error"] = f"Stage 1 failed: {brief['_error']}"
        return pipeline_run

    # Stage 2
    decision, raw2 = stage2_reason(brief)
    pipeline_run["stages"].append({
        "name": "Stage 2 — REASON",
        "technique": "Chain-of-Thought",
        "input": json.dumps(brief, indent=2),
        "raw_llm_output": raw2,
        "parsed_output": decision,
    })
    if "_error" in decision:
        pipeline_run["error"] = f"Stage 2 failed: {decision['_error']}"
        return pipeline_run

    # Stage 3
    output, raw3 = stage3_produce(brief, decision)
    pipeline_run["stages"].append({
        "name": "Stage 3 — PRODUCE",
        "technique": "Goal-Oriented + Constraints",
        "input": json.dumps({"brief": brief, "decision": decision}, indent=2),
        "raw_llm_output": raw3,
        "parsed_output": output,
    })
    if "_error" in output:
        pipeline_run["error"] = f"Stage 3 failed: {output['_error']}"
        return pipeline_run

    pipeline_run["output"] = output
    return pipeline_run