package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// `/gateway feedback export [--since 7d]`: the kept 👍 and 👎 ratings as draft eval
// cases, a tar on stdout (gateway.ExportFeedback). Run in the gateway's pod,
// with its environment, because the gateway's NATS user is the feedback
// bucket's only reader:
//
//	kubectl exec -n kubeagents-system deploy/platform-agent-a2a-gateway -- \
//	    /gateway feedback export --since 7d > feedback.tar
const (
	feedbackCommand       = "feedback"
	feedbackExportCommand = "export"
	feedbackSinceFlag     = "since"
	feedbackSinceUsage    = "only ratings this recent: a Go duration, or a whole number of days such as 7d (default: every kept rating)"
	feedbackDaySuffix     = "d"
	feedbackDay           = 24 * time.Hour
	// feedbackMaxDays bounds a --since in days below time.Duration's
	// overflow.
	feedbackMaxDays    = int(math.MaxInt64 / int64(feedbackDay))
	feedbackClientName = "a2a-gateway-feedback-export"
	feedbackUsage      = "usage: /gateway feedback export [--since 7d] > feedback.tar"
)

var errFeedbackUsage = errors.New(feedbackUsage)

// isFeedbackCommand reports whether the arguments ask for the feedback
// subcommand rather than the gateway.
func isFeedbackCommand(args []string) bool {
	return len(args) > 0 && args[0] == feedbackCommand
}

// feedbackMain runs `feedback export`, writing the tar to out and the line
// per draft to errOut.
func feedbackMain(ctx context.Context, args []string, out, errOut io.Writer, log *slog.Logger) error {
	if len(args) < 2 || args[1] != feedbackExportCommand {
		fmt.Fprintln(errOut, feedbackUsage)
		return errFeedbackUsage
	}
	fs := flag.NewFlagSet(feedbackCommand+" "+feedbackExportCommand, flag.ContinueOnError)
	fs.SetOutput(errOut)
	since := fs.String(feedbackSinceFlag, "", feedbackSinceUsage)
	if err := fs.Parse(args[2:]); err != nil {
		return err
	}
	if fs.NArg() > 0 {
		// A window typed without --since would otherwise export everything.
		fmt.Fprintln(errOut, feedbackUsage)
		return errFeedbackUsage
	}
	window, err := parseSince(*since)
	if err != nil {
		fmt.Fprintln(errOut, err)
		return err
	}
	cfg, err := gateway.FromEnv()
	if err != nil {
		log.Error("config", "err", err)
		return err
	}
	client, err := lib.Connect(ctx, cfg.NATSURL,
		lib.WithName(feedbackClientName),
		lib.WithLogger(log),
		lib.WithNATSOptions(gatewayNATSOptions(cfg)...),
	)
	if err != nil {
		log.Error("nats connect", "err", err)
		return err
	}
	defer client.Close()
	n, err := gateway.ExportFeedback(ctx, client, window, time.Now().UTC(), out, errOut)
	if err != nil {
		log.Error("feedback export", "err", err)
		return err
	}
	fmt.Fprintf(errOut, "%d exported\n", n)
	return nil
}

// parseSince reads --since: empty is every rating, "7d" is seven days, and
// anything else is a Go duration.
func parseSince(s string) (time.Duration, error) {
	if s == "" {
		return 0, nil
	}
	if days, ok := strings.CutSuffix(s, feedbackDaySuffix); ok {
		n, err := strconv.Atoi(days)
		if err != nil || n <= 0 || n > feedbackMaxDays {
			return 0, fmt.Errorf("--%s %q: not a whole number of days", feedbackSinceFlag, s)
		}
		return time.Duration(n) * feedbackDay, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil || d <= 0 {
		return 0, fmt.Errorf("--%s %q: not a positive duration", feedbackSinceFlag, s)
	}
	return d, nil
}
