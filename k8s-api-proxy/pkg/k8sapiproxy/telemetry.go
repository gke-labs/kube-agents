package k8sapiproxy

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	tracerName = "github.com/gke-labs/kube-agents/k8s-api-proxy"
)

// SetupOTel initializes OpenTelemetry tracing from standard OTEL_* environment variables.
// When OTEL_SDK_DISABLED is "true" or OTEL_EXPORTER_OTLP_ENDPOINT is unset, SetupOTel
// returns a no-op TracerProvider so the proxy does not attempt background exports to localhost.
func SetupOTel(ctx context.Context) (func(context.Context) error, trace.TracerProvider, error) {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("OTEL_SDK_DISABLED")), "true") {
		tp := noop.NewTracerProvider()
		return func(context.Context) error { return nil }, tp, nil
	}

	endpoint := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
	tracesEndpoint := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
	if endpoint == "" && tracesEndpoint == "" {
		tp := noop.NewTracerProvider()
		return func(context.Context) error { return nil }, tp, nil
	}

	exporter, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create OTLP HTTP trace exporter: %w", err)
	}

	res, err := resource.New(
		ctx,
		resource.WithFromEnv(),
		resource.WithTelemetrySDK(),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create OpenTelemetry resource: %w", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	return tp.Shutdown, tp, nil
}

func startRequestSpan(tp trace.TracerProvider, r *http.Request, mode Mode) (context.Context, trace.Span) {
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	prop := otel.GetTextMapPropagator()
	ctx := prop.Extract(r.Context(), propagation.HeaderCarrier(r.Header))

	tracer := tp.Tracer(tracerName)
	ctx, span := tracer.Start(
		ctx,
		"k8s.api.proxy",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("http.request.method", r.Method),
			attribute.String("kube_proxy.mode", sanitizeModeLabel(mode)),
		),
	)
	return ctx, span
}

func annotateRequestSpanInfo(span trace.Span, clusterID string, info *request.RequestInfo, dryRun, secretRedacted bool) {
	if span == nil || !span.IsRecording() {
		return
	}
	attrs := []attribute.KeyValue{
		attribute.String("k8s.cluster.id", clusterID),
		attribute.Bool("k8s.dry_run", dryRun),
		attribute.Bool("kube_proxy.secret_redacted", secretRedacted),
	}
	if info != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(nil, info)
		span.SetName(fmt.Sprintf("k8s.api.proxy %s %s", verbLabel, resLabel))
		attrs = append(attrs,
			attribute.String("k8s.verb", verbLabel),
			attribute.String("k8s.resource", resLabel),
			attribute.String("k8s.subresource", subLabel),
			attribute.Bool("k8s.is_resource_request", info.IsResourceRequest),
		)
		if info.APIGroup != "" {
			attrs = append(attrs, attribute.String("k8s.api_group", info.APIGroup))
		}
		if info.APIVersion != "" {
			attrs = append(attrs, attribute.String("k8s.api_version", info.APIVersion))
		}
		if info.Namespace != "" {
			attrs = append(attrs, attribute.String("k8s.namespace", info.Namespace))
		}
	}
	span.SetAttributes(attrs...)
}

func finishRequestSpan(span trace.Span, outcome string, reason RejectionReason, statusCode int) {
	if span == nil {
		return
	}
	if span.IsRecording() {
		attrs := []attribute.KeyValue{
			attribute.String("kube_proxy.outcome", outcome),
			attribute.Int("http.response.status_code", statusCode),
		}
		if reason != ReasonNone {
			attrs = append(attrs, attribute.String("kube_proxy.rejection_reason", string(reason)))
		}
		span.SetAttributes(attrs...)
		if statusCode >= http.StatusBadRequest {
			if reason != ReasonNone {
				span.SetStatus(codes.Error, string(reason))
			} else {
				span.SetStatus(codes.Error, http.StatusText(statusCode))
			}
		} else {
			span.SetStatus(codes.Ok, "")
		}
	}
	span.End()
}

func startUpstreamSpan(ctx context.Context, tp trace.TracerProvider, clusterID, method string) (context.Context, trace.Span) {
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	tracer := tp.Tracer(tracerName)
	return tracer.Start(
		ctx,
		"k8s.api.upstream",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("k8s.cluster.id", clusterID),
			attribute.String("http.request.method", method),
		),
	)
}
