# Integrity sweep: 1 runs, 1 arms

Flags per arm (medium or worse; info-level findings are in the JSON only).

| arm                      | runs | C01 | H03 | P03 |
| ------------------------ | ---- | --- | --- | --- |
| kubeagents_claude-opus-5 | 1    | 1   | 1   | 1   |

First-pass classification: harness-induced=1

## Runs to adjudicate (severe, then high)

| run                        | arm                      | task          | outcome | check    | attempt | evidence                                                                                                                                                                             |
| -------------------------- | ------------------------ | ------------- | ------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| run_20260918_181403_840299 | kubeagents_claude-opus-5 | b-0011-gitops | 1.0     | H03 high |         | 1 abort/provider-death signature(s), 0 benign; first 'rate limit': no pull request at all. Every other\n 75: # non-zero exit -- an expired token, a 502, a rate limit -- means the l |
