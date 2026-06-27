"""
Flask API server for the Prompt Pipeline.
Exposes POST /api/run that accepts raw ticket text and returns pipeline results.
"""

import sys
import os

# Add parent directory to path so pipeline module can be found
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pipeline import run_pipeline, STAGE1_PROMPT, STAGE2_PROMPT, STAGE3_PROMPT
import json

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)

# ──────────────────────────────────────────────────────────────
# Sample tickets for the UI dropdown
# ──────────────────────────────────────────────────────────────

SAMPLE_TICKETS = [
    {
        "label": "Delivery delay (frustrated)",
        "text": "Hi, I ordered a laptop bag on June 10th (order #ORD-4421) and it was supposed to arrive by June 15th. It's now June 20th and tracking shows it's still in the warehouse. I need this for a work trip! Can someone please help? - Sarah",
    },
    {
        "label": "Product defect (neutral)",
        "text": "I received my order #BBQ-9901 yesterday. The blender arrived with a cracked jar and the motor makes a grinding noise when I turn it on. I'd like a replacement or refund. Thanks, Mark",
    },
    {
        "label": "Billing duplicate charge",
        "text": "Hello support team, I was charged twice for my subscription this month. My account email is jane@example.com. Can you look into this? Best, Jane",
    },
    {
        "label": "Gibberish / broken input",
        "text": "asdlkfj 12345 !@#$%^&*() ??? ??? ORDER???",
    },
    {
        "label": "Account access issue",
        "text": "Hey, I can't log into my account since yesterday. It keeps saying 'invalid password' even though I reset it twice. My username is tech_guru42. This is really annoying. - Mike",
    },
]


# ──────────────────────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/samples", methods=["GET"])
def get_samples():
    """Return the list of sample tickets."""
    return jsonify(SAMPLE_TICKETS)


@app.route("/api/prompts", methods=["GET"])
def get_prompts():
    """Return the stage prompts so the UI can display them."""
    return jsonify({
        "stage1": STAGE1_PROMPT,
        "stage2": STAGE2_PROMPT,
        "stage3": STAGE3_PROMPT,
    })


@app.route("/api/run", methods=["POST"])
def run():
    """Run the pipeline on the provided ticket text."""
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "Missing 'text' field in request body"}), 400

    text = data["text"].strip()
    if not text:
        return jsonify({"error": "Text cannot be empty"}), 400

    result = run_pipeline(text)
    return jsonify(result)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 Prompt Pipeline API running on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)