// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NotConnected from "./NotConnected.tsx";

afterEach(cleanup);

describe("NotConnected", () => {
  it("says how to reach the console server, with no error when there is none", () => {
    render(<NotConnected error={null} onRetry={() => {}} />);
    expect(
      screen.getByText("kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-console 8080:8080"),
    ).toBeTruthy();
    expect(screen.getByText("http://localhost:8080")).toBeTruthy();
    expect(screen.getByText(/\?ws=ws:\/\/localhost:9222&user=console&pass=dev-console/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    // No password field: the credential comes from the server now.
    expect(document.querySelector("input")).toBeNull();
  });

  it("puts the error on top and retries on request", async () => {
    const onRetry = vi.fn();
    render(<NotConnected error="503: no console credential at /x" onRetry={onRetry} />);
    expect(screen.getByRole("alert").textContent).toBe("503: no console credential at /x");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
