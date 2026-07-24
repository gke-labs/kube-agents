# SOP: k8s-event-watcher Daily Activity Recap

**Purpose:** Summarizes the daily operational activity, suppressed duplicates, and intercepted warning incidents from `k8s-event-watcher`.

---

## Execution Instructions for Platform Agent

1. **Execute the Python reporting engine directly:**
   ```bash
   python3 /opt/data/scripts/eod_report_generator.py
   ```

2. **Deliver Script Output Verbatim (Strict Chat Formatting Rules):**
   * Output the exact text returned by `python3 /opt/data/scripts/eod_report_generator.py` directly to the user.
   * **STRICT RULE: NEVER convert or rewrite the output into a Markdown table (`| ... |`).** Tables wrap and break line formatting on Google Chat and Slack cards.
   * Preserve the hierarchical bullet list format emitted by the script.
