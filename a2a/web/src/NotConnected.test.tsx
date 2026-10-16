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

  it("blames the credential Secret, not the port-forward, on a 503", () => {
    render(
      <NotConnected
        error="503: no console credential at /var/run/secrets/a2a-console/console-password"
        onRetry={() => {}}
      />,
    );
    expect(screen.getByText(/has no credential yet/)).toBeTruthy();
    expect(
      screen.queryByText("kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-console 8080:8080"),
    ).toBeNull();
  });

  it("shows the read-only recipe for a named user with no password, and no inputs", () => {
    render(<NotConnected error={null} namedUser="web" onRetry={() => {}} />);
    expect(screen.getByText(/user=web&pass=/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.querySelector("input")).toBeNull();
  });

  it("tells the named-user screen's Retry to re-read the address, not to ask the server", () => {
    render(<NotConnected error={null} namedUser="web" onRetry={() => {}} />);
    expect(screen.getByText(/removing.*user.*from the url/i)).toBeTruthy();
    expect(screen.getByText(/re-reads this address/i)).toBeTruthy();
    expect(screen.queryByText(/asks the server/i)).toBeNull();
  });
});
