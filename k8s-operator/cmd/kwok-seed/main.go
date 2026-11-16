// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Command kwok-seed fills a KWOK cluster with Namespaces, Nodes, Pods,
// Services and Events of the shapes the fake API server generator produces
// (internal/testing/fakeapiserver), at the counts a profile asks for, so the
// scale lane (tests/scale/kwok) measures the stock readers against a cluster
// of a real one's shape. Pods carry the Node they run on, so the cluster
// needs no scheduler; kwok-controller marks them Running. A second run over
// the same cluster counts what is already there as existed and exits zero.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/metadata"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

const (
	programName = "kwok-seed"

	// defaultWorkers sits in the band (32 to 64) where a client-go pool
	// reached 200 to 225 Pod creates per second against a binary-runtime
	// kwok cluster on 8 vCPU; beyond it the requests only queue at the API
	// server, whose etcd writes are the limit.
	defaultWorkers = 48
	defaultSeed    = 0
	// The client-side limiter is set well above what the pool can drive, so
	// the API server's priority and fairness is the only throttle in play.
	defaultQPS             = 5000
	defaultBurst           = 10000
	defaultNamespacePrefix = "team"
	defaultWaitRunning     = true
	// defaultWaitTimeout bounds the wait for kwok to mark the Pods Running.
	// kwok-controller plays its pod-ready stage at about 60 pods a second
	// with its default parallelism (measured on 8 vCPU), so 60,000 pods
	// would take about 17 minutes after the last create; the lane's
	// kwok-cluster.sh raises that parallelism so the controller keeps pace
	// with the seeder, and thirty minutes still covers a cluster created
	// without it on a slow runner.
	defaultWaitTimeout = 30 * time.Minute
	defaultSkipEvents  = false

	// runningPollInterval spaces the Running counts; runningPageSize is the
	// page the production readers use (--chunk-size=500), so a count costs
	// the API server what one of their lists does.
	runningPollInterval  = 2 * time.Second
	runningPageSize      = 500
	runningFieldSelector = "status.phase=Running"
	podsResource         = "pods"

	// The ServiceAccount admission plugin rejects a Pod whose namespace has
	// no `default` ServiceAccount yet, and kube-controller-manager creates
	// one per new namespace a moment after it appears. The plugin retries
	// for about two seconds on its own, which a slow runner can exceed, so
	// the seeder waits for the controller before the Pod phase.
	defaultServiceAccountName   = "default"
	serviceAccountFieldSelector = "metadata.name=" + defaultServiceAccountName
	serviceAccountsResource     = "serviceaccounts"
	serviceAccountWaitTimeout   = 2 * time.Minute
	serviceAccountPollInterval  = 500 * time.Millisecond

	// progressInterval is how often a running phase logs its counts, so a
	// five-minute Pod phase in a CI log shows it is moving.
	progressInterval = 10 * time.Second

	// wireContentType and wireAcceptContentTypes ask for protobuf first and
	// JSON when the server cannot, as the event watcher does; the bodies
	// sent here are the generator's full Pods, a fraction of their JSON.
	wireContentType        = runtime.ContentTypeProtobuf
	wireAcceptContentTypes = runtime.ContentTypeProtobuf + "," + runtime.ContentTypeJSON

	// summaryFileMode is owner-only: gosec's G306 asks for 0600 or less on a
	// written file, and only the run that wrote the summary reads it back.
	summaryFileMode = 0o600

	phaseNamespaces = "namespaces"
	phaseNodes      = "nodes"
	phasePods       = "pods"
	phaseServices   = "services"
	phaseEvents     = "events"

	markdownHeader    = "| phase | created | existed | failed | seconds | per_second |"
	markdownSeparator = "|---|---:|---:|---:|---:|---:|"
	markdownRowFormat = "| %s | %d | %d | %d | %.2f | %.1f |\n"
	markdownRunning   = "\npods_running_after_seconds: %.2f\n"
	markdownError     = "first_error: %s\n"
	// noError is what the Markdown line shows for an empty first_error; the
	// JSON keeps the empty string.
	noError = "-"

	exitFailure = 1
	exitUsage   = 2
)

// flags is the command line, parsed.
type flags struct {
	kubeconfig      string
	kubeContext     string
	profilePath     string
	workers         int
	seed            int64
	qps             float64
	burst           int
	namespacePrefix string
	summaryOut      string
	waitRunning     bool
	waitTimeout     time.Duration
	skipEvents      bool
}

// phaseResult is what one phase produced: created and existed are objects
// in place when it ended, failed the requests that produced neither, and
// per_second counts objects in place over the phase's wall time. FirstError
// is the phase's own first failure, kept because the run's first_error names
// only the earliest phase that failed and a later phase can fail for a
// reason of its own.
type phaseResult struct {
	Created    int64   `json:"created"`
	Existed    int64   `json:"existed"`
	Failed     int64   `json:"failed"`
	Seconds    float64 `json:"seconds"`
	PerSecond  float64 `json:"per_second"`
	FirstError string  `json:"first_error,omitempty"`
}

// phases holds the results in the order the phases run.
type phases struct {
	Namespaces phaseResult `json:"namespaces"`
	Nodes      phaseResult `json:"nodes"`
	Pods       phaseResult `json:"pods"`
	Services   phaseResult `json:"services"`
	Events     phaseResult `json:"events"`
}

// summary is the run's JSON, the shape baseline.sh embeds in its results.
// PodsRunningAfterSeconds is the wait phase's own wall time, from the end of
// the Events phase until the Running count reached the Pods in place, and 0
// when --wait-running=false. FirstError is empty when nothing failed.
type summary struct {
	Profile                 profile `json:"profile"`
	Seed                    int64   `json:"seed"`
	Phases                  phases  `json:"phases"`
	PodsRunningAfterSeconds float64 `json:"pods_running_after_seconds"`
	FirstError              string  `json:"first_error"`
}

// phaseRow is one line of the Markdown table.
type phaseRow struct {
	name   string
	result phaseResult
}

func (s summary) rows() []phaseRow {
	return []phaseRow{
		{phaseNamespaces, s.Phases.Namespaces},
		{phaseNodes, s.Phases.Nodes},
		{phasePods, s.Phases.Pods},
		{phaseServices, s.Phases.Services},
		{phaseEvents, s.Phases.Events},
	}
}

func (s summary) failed() int64 {
	var n int64
	for _, row := range s.rows() {
		n += row.result.Failed
	}
	return n
}

// firstError keeps the first failure of the run, the one a reader wants
// when thousands follow it (a missing ServiceAccount, a rejected field).
type firstError struct {
	mu  sync.Mutex
	err error
}

func (f *firstError) record(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err == nil {
		f.err = err
	}
}

func (f *firstError) get() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.err
}

// createFunc creates the i-th object of a phase. An AlreadyExists error is
// an object an earlier run left, counted as existed rather than failed.
type createFunc func(ctx context.Context, i int) error

func main() {
	f, set, err := parseFlags(os.Args[1:])
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(exitUsage)
	}
	prof, err := resolveProfile(f.profilePath, set)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(exitUsage)
	}
	cfg, err := restConfig(f)
	if err != nil {
		log.Print(err)
		os.Exit(exitFailure)
	}
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		log.Printf("kubernetes client: %v", err)
		os.Exit(exitFailure)
	}
	metaClient, err := metadata.NewForConfig(cfg)
	if err != nil {
		log.Printf("metadata client: %v", err)
		os.Exit(exitFailure)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	s, runErr := seed(ctx, clientset, metaClient, f, prof)
	printMarkdown(os.Stdout, s)
	if f.summaryOut != "" {
		if err := writeSummary(f.summaryOut, s); err != nil {
			log.Print(err)
			os.Exit(exitFailure)
		}
	}
	if runErr != nil {
		log.Print(runErr)
		os.Exit(exitFailure)
	}
}

// parseFlags parses args into flags and the count flags that were set,
// keyed by name, so a count given on the command line overrides the
// profile's and one left at its zero default does not.
func parseFlags(args []string) (flags, map[string]int, error) {
	var f flags
	fs := flag.NewFlagSet(programName, flag.ContinueOnError)
	fs.StringVar(&f.kubeconfig, "kubeconfig", "", "path of the kubeconfig for the kwok cluster (required)")
	fs.StringVar(&f.kubeContext, "context", "", "kubeconfig context; empty uses the file's current context")
	fs.StringVar(&f.profilePath, "profile", "", "JSON profile holding the counts: nodes, pods, namespaces, services, events")
	counts := make(map[string]*int, len(countFields))
	for _, c := range countFields {
		counts[c.name] = new(int)
		fs.IntVar(counts[c.name], c.name, 0, c.name+" count; overrides the profile's")
	}
	fs.IntVar(&f.workers, "workers", defaultWorkers, "concurrent create requests")
	fs.Int64Var(&f.seed, "seed", defaultSeed, "generator seed; one seed gives one set of objects")
	fs.Float64Var(&f.qps, "qps", defaultQPS, "client-side request rate limit")
	fs.IntVar(&f.burst, "burst", defaultBurst, "client-side request burst")
	fs.StringVar(&f.namespacePrefix, "namespace-prefix", defaultNamespacePrefix, "namespaces are <prefix>-000 .. <prefix>-(N-1)")
	fs.StringVar(&f.summaryOut, "summary-out", "", "write the JSON summary to this path; stdout carries the Markdown lines")
	fs.BoolVar(&f.waitRunning, "wait-running", defaultWaitRunning, "wait until every seeded pod is Running")
	fs.DurationVar(&f.waitTimeout, "wait-timeout", defaultWaitTimeout, "how long --wait-running waits before failing")
	fs.BoolVar(&f.skipEvents, "skip-events", defaultSkipEvents, "skip the Events phase")
	if err := fs.Parse(args); err != nil {
		return flags{}, nil, err
	}
	set := map[string]int{}
	fs.Visit(func(fl *flag.Flag) {
		if p, ok := counts[fl.Name]; ok {
			set[fl.Name] = *p
		}
	})
	return f, set, f.validate()
}

func (f flags) validate() error {
	switch {
	case f.kubeconfig == "":
		return errors.New("--kubeconfig is required")
	case f.workers <= 0:
		return errors.New("--workers must be greater than zero")
	case f.qps <= 0 || f.burst <= 0:
		return errors.New("--qps and --burst must be greater than zero")
	case f.namespacePrefix == "":
		return errors.New("--namespace-prefix must not be empty")
	case f.waitTimeout <= 0:
		return errors.New("--wait-timeout must be greater than zero")
	}
	return nil
}

// restConfig is the client config for the kwok cluster: the named kubeconfig
// file only (never the default search, so ~/.kube/config stays out of it),
// the flags' limiter, and protobuf on the wire.
func restConfig(f flags) (*rest.Config, error) {
	rules := &clientcmd.ClientConfigLoadingRules{ExplicitPath: f.kubeconfig}
	overrides := &clientcmd.ConfigOverrides{CurrentContext: f.kubeContext}
	cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(rules, overrides).ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("kubeconfig %s: %w", f.kubeconfig, err)
	}
	cfg.QPS = float32(f.qps)
	cfg.Burst = f.burst
	cfg.ContentType = wireContentType
	cfg.AcceptContentTypes = wireAcceptContentTypes
	cfg.UserAgent = programName
	return cfg, nil
}

// seed runs the phases in order and returns the summary, with an error when
// a request failed, the Running wait timed out, or the run was interrupted.
func seed(ctx context.Context, client kubernetes.Interface, metaClient metadata.Interface, f flags, prof profile) (summary, error) {
	gen := fakeapiserver.NewGenerator(f.seed, fakeapiserver.Sizes{Nodes: prof.Nodes, Pods: prof.Pods, Events: prof.Events})
	lay := layout{prefix: f.namespacePrefix, namespaces: prof.Namespaces, pods: prof.Pods}
	core := client.CoreV1()
	names := lay.namespaceNames()
	errs := &firstError{}
	s := summary{Profile: prof, Seed: f.seed}

	s.Phases.Namespaces = runPhase(ctx, phaseNamespaces, prof.Namespaces, f.workers, errs, func(ctx context.Context, i int) error {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: names[i]}}
		_, err := core.Namespaces().Create(ctx, ns, metav1.CreateOptions{})
		return err
	})
	s.Phases.Nodes = runPhase(ctx, phaseNodes, prof.Nodes, f.workers, errs, func(ctx context.Context, i int) error {
		node := gen.Node(i)
		prepareNode(node)
		_, err := core.Nodes().Create(ctx, node, metav1.CreateOptions{})
		return err
	})
	if inPlace := s.Phases.Namespaces.Created + s.Phases.Namespaces.Existed; inPlace > 0 {
		if err := waitForDefaultServiceAccounts(ctx, metaClient, names, inPlace, serviceAccountWaitTimeout); err != nil {
			// Not fatal on its own: the Pod phase reports what the admission
			// plugin then makes of it, with the namespace in the first error.
			log.Printf("warning: %v", err)
		}
	}
	s.Phases.Pods = runPhase(ctx, phasePods, prof.Pods, f.workers, errs, func(ctx context.Context, i int) error {
		pod := gen.Pod(i)
		lay.preparePod(pod, i)
		_, err := core.Pods(pod.Namespace).Create(ctx, pod, metav1.CreateOptions{})
		return err
	})
	s.Phases.Services = runPhase(ctx, phaseServices, prof.Services, f.workers, errs, func(ctx context.Context, i int) error {
		svc := lay.serviceFor(i, gen.Pod(i).Labels[appLabel])
		_, err := core.Services(svc.Namespace).Create(ctx, svc, metav1.CreateOptions{})
		return err
	})
	if !f.skipEvents {
		s.Phases.Events = runPhase(ctx, phaseEvents, prof.Events, f.workers, errs, func(ctx context.Context, i int) error {
			ev := gen.Event(i)
			lay.prepareEvent(ev, i)
			_, err := core.Events(ev.Namespace).Create(ctx, ev, metav1.CreateOptions{})
			return err
		})
	}
	if f.waitRunning && ctx.Err() == nil {
		want := s.Phases.Pods.Created + s.Phases.Pods.Existed
		seconds, err := waitRunning(ctx, metaClient, names, want, f.waitTimeout)
		s.PodsRunningAfterSeconds = seconds
		if err != nil {
			errs.record(err)
		}
	}
	if err := ctx.Err(); err != nil {
		errs.record(fmt.Errorf("interrupted: %w", err))
	}
	if err := errs.get(); err != nil {
		s.FirstError = err.Error()
	}
	if failed := s.failed(); failed > 0 {
		return s, fmt.Errorf("%d requests failed; first: %w", failed, errs.get())
	}
	if err := errs.get(); err != nil {
		return s, err
	}
	return s, nil
}

// runPhase creates count objects with a pool of workers reading indexes from
// one channel, and counts the outcomes. An interrupted context stops the
// feed; the creates in flight then fail and are counted as such.
func runPhase(ctx context.Context, name string, count, workers int, errs *firstError, create createFunc) phaseResult {
	start := time.Now()
	var created, existed, failed atomic.Int64
	phaseErrs := &firstError{}
	indexes := make(chan int)
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range indexes {
				err := create(ctx, i)
				switch {
				case err == nil:
					created.Add(1)
				case apierrors.IsAlreadyExists(err):
					existed.Add(1)
				default:
					failed.Add(1)
					err = fmt.Errorf("%s %d: %w", name, i, err)
					errs.record(err)
					phaseErrs.record(err)
				}
			}
		}()
	}
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(progressInterval)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				log.Printf("%s: %d created, %d existed, %d failed of %d", name, created.Load(), existed.Load(), failed.Load(), count)
			}
		}
	}()
feed:
	for i := range count {
		select {
		case indexes <- i:
		case <-ctx.Done():
			break feed
		}
	}
	close(indexes)
	wg.Wait()
	close(done)

	elapsed := time.Since(start)
	r := phaseResult{Created: created.Load(), Existed: existed.Load(), Failed: failed.Load(), Seconds: elapsed.Seconds()}
	if inPlace := r.Created + r.Existed; inPlace > 0 && elapsed > 0 {
		r.PerSecond = float64(inPlace) / elapsed.Seconds()
	}
	if err := phaseErrs.get(); err != nil {
		r.FirstError = err.Error()
	}
	log.Printf("%s: %d created, %d existed, %d failed in %.2fs (%.0f/s)", name, r.Created, r.Existed, r.Failed, r.Seconds, r.PerSecond)
	if r.FirstError != "" {
		log.Printf("%s: first failure: %s", name, r.FirstError)
	}
	return r
}

// waitForDefaultServiceAccounts returns once want of the namespaces in names
// have their `default` ServiceAccount, or after timeout (the run passes
// serviceAccountWaitTimeout). want is what the Namespaces phase put in
// place: a namespace it failed to create never gets one, and waiting on it
// would only run the clock out.
func waitForDefaultServiceAccounts(ctx context.Context, client metadata.Interface, names []string, want int64, timeout time.Duration) error {
	resource := corev1.SchemeGroupVersion.WithResource(serviceAccountsResource)
	start := time.Now()
	for {
		have, err := countPaged(ctx, client, resource, serviceAccountFieldSelector, inNamespaces(names))
		if err != nil {
			return fmt.Errorf("count default ServiceAccounts: %w", err)
		}
		elapsed := time.Since(start)
		if have >= want {
			log.Printf("default ServiceAccount present in all %d namespaces after %.1fs", want, elapsed.Seconds())
			return nil
		}
		if elapsed >= timeout {
			return fmt.Errorf("default ServiceAccount present in %d of %d namespaces after %s", have, want, timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(serviceAccountPollInterval):
		}
	}
}

// inNamespaces keeps the objects in one of the namespaces in names. Both
// waits count through it: the cluster has pods and ServiceAccounts of its
// own, and a reused one has those of an earlier run under another prefix,
// and neither may stand in for the objects this run put in place.
func inNamespaces(names []string) func(*metav1.PartialObjectMetadata) bool {
	ours := make(map[string]bool, len(names))
	for _, name := range names {
		ours[name] = true
	}
	return func(m *metav1.PartialObjectMetadata) bool { return ours[m.Namespace] }
}

// waitRunning polls the count of Running Pods in the namespaces in names
// until it reaches want or timeout passes, and returns the seconds it took
// either way.
func waitRunning(ctx context.Context, client metadata.Interface, names []string, want int64, timeout time.Duration) (float64, error) {
	resource := corev1.SchemeGroupVersion.WithResource(podsResource)
	start := time.Now()
	for {
		running, err := countPaged(ctx, client, resource, runningFieldSelector, inNamespaces(names))
		if err != nil {
			return time.Since(start).Seconds(), fmt.Errorf("count Running pods: %w", err)
		}
		elapsed := time.Since(start)
		if running >= want {
			log.Printf("%d of %d pods Running after %.2fs", running, want, elapsed.Seconds())
			return elapsed.Seconds(), nil
		}
		if elapsed >= timeout {
			return elapsed.Seconds(), fmt.Errorf("%d of %d pods Running after %s", running, want, timeout)
		}
		log.Printf("%d of %d pods Running after %.1fs", running, want, elapsed.Seconds())
		select {
		case <-ctx.Done():
			return time.Since(start).Seconds(), ctx.Err()
		case <-time.After(runningPollInterval):
		}
	}
}

// countPaged counts the objects of resource across all namespaces matching
// selector, page by page at runningPageSize, keeping those keep admits (all
// when keep is nil). Metadata-only lists keep the count to a fraction of
// the full objects' bytes.
func countPaged(ctx context.Context, client metadata.Interface, resource schema.GroupVersionResource, selector string, keep func(*metav1.PartialObjectMetadata) bool) (int64, error) {
	var total int64
	opts := metav1.ListOptions{FieldSelector: selector, Limit: runningPageSize}
	for {
		list, err := client.Resource(resource).List(ctx, opts)
		if err != nil {
			return 0, err
		}
		for i := range list.Items {
			if keep == nil || keep(&list.Items[i]) {
				total++
			}
		}
		if list.Continue == "" {
			return total, nil
		}
		opts.Continue = list.Continue
	}
}

// printMarkdown writes the summary as the table and lines a step log shows.
func printMarkdown(w io.Writer, s summary) {
	fmt.Fprintln(w, markdownHeader)
	fmt.Fprintln(w, markdownSeparator)
	for _, row := range s.rows() {
		fmt.Fprintf(w, markdownRowFormat, row.name, row.result.Created, row.result.Existed, row.result.Failed, row.result.Seconds, row.result.PerSecond)
	}
	fmt.Fprintf(w, markdownRunning, s.PodsRunningAfterSeconds)
	firstError := s.FirstError
	if firstError == "" {
		firstError = noError
	}
	fmt.Fprintf(w, markdownError, firstError)
}

func writeSummary(path string, s summary) error {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("summary: %w", err)
	}
	if err := os.WriteFile(path, append(data, '\n'), summaryFileMode); err != nil {
		return fmt.Errorf("summary: %w", err)
	}
	return nil
}
