"""Run the held-out eval set against the real risk-classifier and measure
the false-negative rate (design doc Next Step 8 / Implementation Task T6).

False negative here means: a post labeled "risky" in the eval set that the
classifier said was "safe" -- the dangerous direction, since that's a post
that would have auto-published unreviewed. A false positive (labeled safe,
classified risky) just costs a few minutes of manual review -- annoying,
not dangerous.

This is a smoke test at n=18, not a statistical safety guarantee -- see the
eval set's own _notes field for the contamination-avoidance caveat. The
review gate itself remains the real backstop for legal exposure.

Run: .venv/Scripts/python.exe eval/run_classifier_eval.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.errors import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from classifier.risk_classifier import classify_one  # noqa: E402
from schema.messages import PostDrafted, compute_news_id  # noqa: E402

load_dotenv()

EVAL_SET_PATH = Path(__file__).resolve().parent / "classifier_eval_set.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Free-tier Gemini has a low requests-per-minute ceiling. An 18-call eval
# run back-to-back hit it directly (429 RESOURCE_EXHAUSTED on call #7 in
# the first attempt at this eval) -- pace calls and retry once on a 429
# rather than just recording every remaining example as an error.
SECONDS_BETWEEN_CALLS = 7
RATE_LIMIT_RETRY_WAIT_SECONDS = 35


def _classify_with_rate_limit_retry(client, item):
    try:
        return classify_one(client, item)
    except ClientError as exc:
        if exc.code == 429:
            print(f"  (rate limited, waiting {RATE_LIMIT_RETRY_WAIT_SECONDS}s and retrying once)")
            time.sleep(RATE_LIMIT_RETRY_WAIT_SECONDS)
            return classify_one(client, item)
        raise


def load_eval_set() -> list[dict]:
    data = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    return data["examples"]


def run_eval() -> dict:
    client = genai.Client()
    examples = load_eval_set()

    results = []
    for i, example in enumerate(examples):
        if i > 0:
            time.sleep(SECONDS_BETWEEN_CALLS)

        link = f"https://eval.local/classifier/{example['id']}"
        item = PostDrafted(
            news_id=compute_news_id(link),
            source_link=link,
            draft_text=example["draft_text"],
            archetype_disclosure="(eval fixture, not a real disclosure)",
            drafted_at=datetime.now(timezone.utc),
        )
        try:
            classified = _classify_with_rate_limit_retry(client, item)
            actual = classified.verdict
            reason = classified.verdict_reason
            error = None
        except Exception as exc:  # noqa: BLE001 -- record the failure, keep going
            actual = "ERROR"
            reason = ""
            error = str(exc)

        expected = example["expected_verdict"]
        results.append(
            {
                "id": example["id"],
                "expected": expected,
                "actual": actual,
                "correct": actual == expected,
                "reason": reason,
                "error": error,
            }
        )
        status = "OK" if actual == expected else "MISS"
        print(f"[{status}] {example['id']}: expected={expected} actual={actual}")

    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    false_negatives = [r for r in results if r["expected"] == "risky" and r["actual"] == "safe"]
    false_positives = [r for r in results if r["expected"] == "safe" and r["actual"] == "risky"]
    errors = [r for r in results if r["actual"] == "ERROR"]

    risky_total = sum(1 for r in results if r["expected"] == "risky")
    false_negative_rate = len(false_negatives) / risky_total if risky_total else None

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model": "gemini-3.5-flash",
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else None,
        "false_negatives": [r["id"] for r in false_negatives],
        "false_negative_rate": false_negative_rate,
        "false_positives": [r["id"] for r in false_positives],
        "errors": [r["id"] for r in errors],
        "results": results,
    }
    return report


def main():
    report = run_eval()

    print()
    print(f"Total: {report['total']}  Correct: {report['correct']}  Accuracy: {report['accuracy']:.0%}")
    print(f"False negatives (risky misclassified as safe): {report['false_negatives'] or 'NONE'}")
    if report["false_negative_rate"] is not None:
        print(f"False negative rate: {report['false_negative_rate']:.0%}")
    print(f"False positives (safe misclassified as risky): {report['false_positives'] or 'none'}")
    if report["errors"]:
        print(f"Errors (classifier call failed): {report['errors']}")

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"classifier-eval-{timestamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nFull report saved to {out_path}")


if __name__ == "__main__":
    main()
