"""Run a small held-out set of synthetic news items through the drafter and
check structural adherence to the composite-archetype disclosure rules
(design doc Next Step 8 / Implementation Task T6, drafter half).

This is a lighter check than the classifier eval: the classifier's rate is
a hard number (false negatives = legal exposure risk), while "is this a
good post" is a judgment call. What's mechanically checkable -- and
checked here -- is:
  1. archetype_disclosure is non-empty.
  2. archetype_disclosure appears verbatim inside draft_text (system prompt
     rule 6 requires this -- if it doesn't hold, the model drifted from a
     structured field into disconnected free text).
  3. The disclosure phrasing actually varies across the run (not the same
     line reused), exercising the same duplicate-avoidance logic real runs
     use.

Full draft text is printed for manual quality judgment -- that part isn't
mechanically checkable, same as the Next Step 0b gut-check.

Run: .venv/Scripts/python.exe eval/run_drafter_eval.py
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.errors import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drafter.post_drafter import draft_one  # noqa: E402
from schema.messages import NewsScored, compute_news_id  # noqa: E402

load_dotenv()

SECONDS_BETWEEN_CALLS = 5

SAMPLE_ITEMS = [
    ("Government backs lifting the TRO on the Metro Manila wage hike", "The government supports reversing a court order that paused a mandated wage increase."),
    ("BPO worker network petitions DOLE for higher minimum wage", "A network representing BPO employees is asking for a substantial increase to the daily minimum wage."),
    ("Survey: most PH employees respond positively to RTO mandates", "A new survey finds most Filipino office workers report a positive response to return-to-office policies, though many say their office isn't fit for purpose."),
    ("DOLE tightens wage order compliance guidelines nationwide", "New administrative guidelines aim to ensure workers actually receive wage increases mandated under approved wage orders."),
    ("National labor council approves new BPO worker subcommittee", "A tripartite industrial peace council has approved a dedicated subcommittee giving BPO workers a formal channel to raise workplace concerns."),
]


def main():
    client = genai.Client()
    recent_disclosures: list[str] = []
    results = []

    for i, (title, excerpt) in enumerate(SAMPLE_ITEMS):
        if i > 0:
            time.sleep(SECONDS_BETWEEN_CALLS)

        link = f"https://eval.local/drafter/{i}"
        item = NewsScored(
            news_id=compute_news_id(link),
            feed="nation",
            title=title,
            link=link,
            excerpt=excerpt,
            author=None,
            guid=link,
            published_at=datetime.now(timezone.utc),
            scraped_at=datetime.now(timezone.utc),
            score=5.0,
            score_reason="eval fixture",
        )

        try:
            result = draft_one(client, item, recent_disclosures)
        except ClientError as exc:
            if exc.code == 429:
                print(f"[{i}] rate limited, waiting 30s and retrying once")
                time.sleep(30)
                result = draft_one(client, item, recent_disclosures)
            else:
                raise

        disclosure_present = bool(result.archetype_disclosure.strip())
        disclosure_in_draft = result.archetype_disclosure in result.draft_text
        is_repeat = result.archetype_disclosure in recent_disclosures

        recent_disclosures.append(result.archetype_disclosure)
        results.append(
            {
                "title": title,
                "disclosure_present": disclosure_present,
                "disclosure_in_draft": disclosure_in_draft,
                "is_repeat_of_prior": is_repeat,
            }
        )

        print(f"\n=== [{i}] {title} ===")
        print(result.draft_text)
        print(f"-- disclosure_present={disclosure_present} disclosure_in_draft={disclosure_in_draft} is_repeat={is_repeat}")

    total = len(results)
    disclosure_ok = sum(1 for r in results if r["disclosure_present"] and r["disclosure_in_draft"])
    repeats = sum(1 for r in results if r["is_repeat_of_prior"])
    print(f"\n\nSummary: {disclosure_ok}/{total} drafts have a present, embedded disclosure. {repeats} repeated a prior disclosure verbatim.")


if __name__ == "__main__":
    main()
