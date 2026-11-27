# core-agent-harness recordings

Each `.jsonl` is one turn's stdout from `a2a/cmd/core-agent-harness`, built from this tree and run against a scripted loopback Gemini endpoint with networking off. The `event` lines' content is core-agent's own conversation events.

| File                 | Turn                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------- |
| `text-only.jsonl`    | a text answer, no tools                                                                  |
| `tool-step.jsonl`    | one `bash` call that succeeds                                                            |
| `tool-error.jsonl`   | one `bash` call whose command exits 3: a tool result, not failed, carrying the exit code |
| `tool-refused.jsonl` | one `bash` call the policy doesn't allow: a failed tool result                           |
| `unknown-tool.jsonl` | a call to a tool the turn wasn't offered: a failed tool result                           |
| `resumed-turn.jsonl` | a second turn whose envelope carried `text-only.jsonl`'s event lines as its history      |
