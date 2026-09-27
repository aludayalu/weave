# Task detection prompt (runs on Databricks model gateway)

Input: the three silver tables (`silver.discord_messages`, `silver.jira_changelog_events`,
`silver.github_commits` + `silver.github_pull_requests`, `silver.documents`) restricted to
a candidate time range. Output: JSON only.

```
You are segmenting a software team's raw work history into TASKS.

You are given, for one repository over a date range:
  - discord_messages: (ts_utc, channel, author_name, content, is_thread, attachment_ids)
  - jira_changelog_events: (issue_key, event_ts_utc, author, field, from_value, to_value)
  - github_commits: (sha, authored_ts_utc, author_name, message, is_merge, parent_shas, changed_files)
  - github_pull_requests: (pr_number, branch, opened_ts_utc, merged_ts_utc, merged, author, title)
  - documents: (filename, doc_type, extracted_text, linked_issue_key)

DEFINITION OF A TASK
A task is one unit of engineering work with a single start and a single end in this
team's history. It is NOT one PR, one ticket, or one message.

  start = the moment the work is first raised, which is almost always a chat message
          in which a person asks for the work or reports the problem. Ticket creation
          and first commits are usually LATER than the chat message; the chat message
          is the source of truth for the start.
  end   = the moment the work is considered finished: the chat message that announces
          the fix is merged / ticket is closed / deploy is green, and the merge commit
          or ticket closing transition that supports it. Discussion may continue after
          this point; that is a follow-up, not part of the task.

EVIDENCE RULES
  1. Discord is the authoritative stream. Jira and git corroborate but never move a
     boundary that chat clearly places elsewhere.
  2. A boundary must be supported by a real signal: an explicit statement by a person,
     or a merge/close event. Do not invent windows from silence.
  3. Unrelated chatter inside a window (standups, creds rotation, releases) is NOT a task.
  4. Two issues discussed in one conversation are two tasks only if they are worked and
     closed separately; otherwise it is one task with a larger scope.
  5. A ticket created weeks after the chat, and merged weeks after that, belongs to the
     task it was raised for — not a new task.
  6. Never emit a window that starts before its first cited message or ends after its
     last cited evidence.

OUTPUT FORMAT
Return a JSON array. No prose, no markdown fence. One object per task, ordered by start.

[
  {
    "task_id": "stable-slug",
    "title": "short human title",
    "start_ts_utc": "2026-07-07T14:12:00Z",
    "end_ts_utc": "2026-07-10T21:40:00Z",
    "confidence": 0.0-1.0,
    "summary": "one or two sentences describing the problem and the fix",
    "participants": ["Priya Sharma", "Marcus Lee"],
    "evidence": {
      "discord": {
        "channel": "billing",
        "start_message_id": "1223...",
        "end_message_id": "1311...",
        "supporting_message_ids": ["1240...", "1288..."]
      },
      "jira": {"issue_keys": ["MER-101"], "closing_event_ts_utc": "2026-07-10T21:31:00Z"},
      "github": {"merge_shas": ["a1b2..."], "pr_numbers": [1], "branch": "feature/..."},
      "documents": ["MER-101-client-report.pdf"]
    }
  }
]
```

`start_ts_utc` / `end_ts_utc` must be exact timestamps taken from the cited records, in
UTC. Confidence reflects boundary certainty: 1.0 when chat and merge agree exactly,
0.6 when a boundary is inferred from a single stream.
