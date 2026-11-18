// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Transcript, { fencedBlock, replyLabel } from "./Transcript.tsx";
import type { ChatEntry } from "./model.ts";

afterEach(cleanup);

const BLOCK =
  "kube-agents usage & feedback summary (2026-10-22)\nenvironment:\n  mode: next";
const SUMMARY_NOTICE = "```\n" + BLOCK + "\n```";

function notice(id: string, text: string): ChatEntry {
  return {
    id,
    kind: "notice",
    session: "gateway",
    text,
    correlationId: "corr-" + id,
  };
}

describe("fencedBlock", () => {
  it("returns the first fenced block without its fences", () => {
    expect(fencedBlock(SUMMARY_NOTICE)).toBe(BLOCK);
    expect(fencedBlock("before\n```\none\n```\nafter ```\ntwo\n```")).toBe(
      "one",
    );
  });

  it("returns null for text with no closed fence", () => {
    expect(fencedBlock("no fence here")).toBeNull();
    expect(fencedBlock("```\nopen only")).toBeNull();
    expect(fencedBlock("```\nL1 ```")).toBeNull();
    expect(fencedBlock("```\n```")).toBeNull();
    expect(fencedBlock("```\n\n```")).toBeNull();
    expect(fencedBlock("```\r\n\r\n```")).toBeNull();
    expect(fencedBlock("```\n \n\t\n```")).toBeNull();
  });

  it("reads fences by line: an inline triple backtick does not open, an indented or tagged line closes or not", () => {
    expect(fencedBlock("use ```kubectl``` now\nthen:\n```\nblock\n```")).toBe("block");
    expect(fencedBlock("```\nL1\n  ```")).toBe("L1");
    expect(fencedBlock("```\nL1\n```yaml\nL2\n```")).toBe("L1\n```yaml\nL2");
    expect(fencedBlock("```\nL1\n``` ")).toBe("L1");
  });

  it("skips a language tag, keeps an inline triple backtick, and joins CRLF lines with LF", () => {
    expect(fencedBlock("```text\nL1\n```")).toBe("L1");
    expect(fencedBlock("```\na ``` b\nc\n```")).toBe("a ``` b\nc");
    expect(fencedBlock("```\r\nL1\r\nL2\r\n```")).toBe("L1\nL2");
  });
});

describe("replyLabel", () => {
  it("names a report by its first non-blank line with the Markdown marks stripped", () => {
    expect(replyLabel("\n\n## **Pods** on `seeded-a`\nmore")).toBe("Pods on seeded-a");
    expect(replyLabel("- __one__ pod is *pending*")).toBe("one pod is pending");
    expect(replyLabel("> quoted line")).toBe("quoted line");
    expect(replyLabel("1. first item")).toBe("first item");
    // An underscore inside a name is not an emphasis mark.
    expect(replyLabel("`kanban_notifier.py` saw _two_ pods in payments_api")).toBe("kanban_notifier.py saw two pods in payments_api");
    // A line that is only marks is skipped, not stripped to an empty label.
    expect(replyLabel("```\nkubectl get pods\n```")).toBe("kubectl get pods");
    // A fence with a language tag is still a fence, not a line named after the tag.
    expect(replyLabel("```yaml\napiVersion: v1\n```")).toBe("apiVersion: v1");
    expect(replyLabel("***\n> **payments_api** is down")).toBe("payments_api is down");
    expect(replyLabel(">\n---\nreal line")).toBe("real line");
    // The cut applies after the strip, so marks never eat the budget.
    expect(replyLabel("**" + "p".repeat(70) + "**")).toBe("p".repeat(60) + "…");
    expect(replyLabel("   \n\t")).toBe("");
  });
});

describe("Transcript reply button", () => {
  it("offers a reply only on a report with a root task, and only to a pane that takes replies", async () => {
    const report = { ...notice("n1", "two pods pending"), replyTo: "task-root" };
    const onReply = vi.fn();
    const stale = { ...notice("n3", "an older report"), replyTo: "task-old", correlationId: "corr-old" };
    const { unmount } = render(
      <Transcript
        entries={[report, notice("n2", "⏳ submitted…"), stale]}
        empty=""
        reply={{ conversation: report.correlationId, onReply }}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: /^reply under: / });
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("aria-label")).toBe("reply under: two pods pending");
    // Presentation: the button sits in the entry's body under the text, not as a flex sibling in the
    // meta gutter beside it, and it is not the footer's uppercase pill.
    const body = buttons[0].parentElement!;
    expect(body.classList.contains("chat-body")).toBe(true);
    expect(body.querySelector(".chat-text")?.textContent).toBe("two pods pending");
    expect(buttons[0].closest(".chat-usage, .chat-note")).toBeNull();
    expect(buttons[0].classList.contains("chat-reply")).toBe(true);
    expect(buttons[0].classList.contains("probe-button")).toBe(false);
    expect(buttons[0].textContent).toBe("↩ reply");
    await userEvent.click(buttons[0]);
    expect(onReply).toHaveBeenCalledWith(report);
    unmount();
    // The session view renders the same list without a reply target.
    render(<Transcript entries={[report]} empty="" />);
    expect(screen.queryByRole("button", { name: /^reply under: / })).toBeNull();
  });
});

describe("Transcript copy button", () => {
  let writeText: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  it("copies a notice's fenced block, fences stripped, on click", async () => {
    render(<Transcript entries={[notice("n1", SUMMARY_NOTICE)]} empty="" />);
    const button = screen.getByRole("button", { name: /^copy$/i });
    await userEvent.click(button);
    expect(writeText).toHaveBeenCalledWith(BLOCK);
    expect(await screen.findByRole("button", { name: /copied/i })).toBeTruthy();
  });

  it("offers no copy button on a notice without a fenced block", () => {
    render(
      <Transcript
        entries={[
          notice("n2", "📝 To report a problem with kube-agents itself: …"),
        ]}
        empty=""
      />,
    );
    expect(screen.queryByRole("button", { name: /copy/i })).toBeNull();
  });

  it("says so when the clipboard refuses", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    render(<Transcript entries={[notice("n3", SUMMARY_NOTICE)]} empty="" />);
    await userEvent.click(screen.getByRole("button", { name: /^copy$/i }));
    expect(
      await screen.findByRole("button", { name: /copy failed/i }),
    ).toBeTruthy();
  });
});
