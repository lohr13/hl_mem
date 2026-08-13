"""Create a proper LongMemEval fixture from the real dataset, keeping only answer sessions."""

import json
import os

with open("evaluation/longmemeval/longmemeval_s_cleaned.json", "r", encoding="utf-8") as f:
    data = json.load(f)

subset = []
for i in range(3):
    item = dict(data[i])
    # Keep only the first few sessions + answer sessions to keep it small
    answer_ids = item.get("answer_session_ids", [])
    if isinstance(answer_ids, int):
        answer_ids = [answer_ids]
    elif isinstance(answer_ids, list):
        answer_ids = [int(x) if isinstance(x, str) and x.isdigit() else x for x in answer_ids]

    sessions = item["haystack_sessions"]
    session_ids = item["haystack_session_ids"]
    dates = item["haystack_dates"]

    # Keep first 3 sessions + answer sessions
    keep_indices = set(range(min(3, len(sessions))))
    for j, sid in enumerate(session_ids):
        if sid in answer_ids:
            keep_indices.add(j)

    keep_sorted = sorted(keep_indices)
    item["haystack_sessions"] = [sessions[j] for j in keep_sorted]
    item["haystack_session_ids"] = [session_ids[j] for j in keep_sorted]
    item["haystack_dates"] = [dates[j] for j in keep_sorted]

    subset.append(item)
    print(f"Record {i}: kept {len(keep_sorted)} sessions (from {len(sessions)})")

with open("tests/fixtures/longmemeval_official_small.json", "w", encoding="utf-8") as out:
    json.dump(subset, out, ensure_ascii=False)

size = os.path.getsize("tests/fixtures/longmemeval_official_small.json")
print(f"Wrote {len(subset)} records, {size} bytes")
