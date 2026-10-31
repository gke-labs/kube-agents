// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Transcript, { fencedBlock } from "./Transcript.tsx";
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
