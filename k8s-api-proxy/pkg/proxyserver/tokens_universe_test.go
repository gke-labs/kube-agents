package proxyserver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestResolveKubeProxyUniverseDomainAndEndpoints(t *testing.T) {
	const customUniverse = "universe.example.test"

	got, err := resolveKubeProxyUniverseDomain(context.Background(), " "+customUniverse+" ", false, "", nil)
	if err != nil || got != customUniverse {
		t.Fatalf("explicit universe = (%q, %v), want (%q, nil)", got, err, customUniverse)
	}

	if _, err := resolveKubeProxyUniverseDomain(context.Background(), "https://bad/domain", false, "", nil); err == nil {
		t.Fatal("expected error for invalid explicit universe domain")
	}

	metaOK := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(gceMetadataHeaderName) != gceMetadataHeaderValue {
			t.Errorf("missing Metadata-Flavor header")
		}
		_, _ = w.Write([]byte(customUniverse + "\n"))
	}))
	defer metaOK.Close()

	got, err = resolveKubeProxyUniverseDomain(context.Background(), "", true, metaOK.URL, metaOK.Client())
	if err != nil || got != customUniverse {
		t.Fatalf("metadata 200 universe = (%q, %v), want (%q, nil)", got, err, customUniverse)
	}

	meta404 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer meta404.Close()

	got, err = resolveKubeProxyUniverseDomain(context.Background(), "", true, meta404.URL, meta404.Client())
	if err != nil || got != defaultUniverse {
		t.Fatalf("metadata 404 universe = (%q, %v), want (%q, nil)", got, err, defaultUniverse)
	}

	flakyCalls := 0
	metaFlaky := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		flakyCalls++
		if flakyCalls == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(customUniverse))
	}))
	defer metaFlaky.Close()
	got, err = resolveKubeProxyUniverseDomain(context.Background(), "", true, metaFlaky.URL, metaFlaky.Client())
	if err != nil || got != customUniverse {
		t.Fatalf("metadata flaky->200 universe = (%q, %v), want (%q, nil)", got, err, customUniverse)
	}

	meta503 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer meta503.Close()
	if _, err := resolveKubeProxyUniverseDomain(context.Background(), "", true, meta503.URL, meta503.Client()); err == nil {
		t.Fatal("expected error when all GCE metadata probe attempts fail with 503")
	}

	if got := stsURLForUniverse(customUniverse); got != "https://sts."+customUniverse+"/v1/token" {
		t.Fatalf("stsURLForUniverse = %q", got)
	}
	if got := iamCredentialsURLForUniverse(customUniverse); got != "https://iamcredentials."+customUniverse+"/v1" {
		t.Fatalf("iamCredentialsURLForUniverse = %q", got)
	}
	if got := containerAPIURLForUniverse(customUniverse, ""); got != "https://container."+customUniverse {
		t.Fatalf("containerAPIURLForUniverse = %q", got)
	}
	if got := containerAPIURLForUniverse(defaultUniverse, ""); got != "" {
		t.Fatalf("containerAPIURLForUniverse(default) = %q, want empty", got)
	}
	if got := containerAPIURLForUniverse(customUniverse, "https://override.example.test"); got != "https://override.example.test" {
		t.Fatalf("explicit containerAPIURL override = %q", got)
	}
}

func TestResolveKubeProxyUniverseDomainRefusesADomainUnderGoogleapis(t *testing.T) {
	// <service>.mtls.googleapis.com is a host of the commercial universe, so a
	// domain under googleapis.com names no other universe.
	for _, under := range []string{"mtls.googleapis.com", " MTLS.GoogleAPIs.com. ", "a.b.googleapis.com"} {
		if got, err := resolveKubeProxyUniverseDomain(context.Background(), under, false, "", nil); err == nil {
			t.Errorf("explicit %q = (%q, nil), want an error", under, got)
		}
	}

	meta := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("mtls.googleapis.com"))
	}))
	defer meta.Close()
	if got, err := resolveKubeProxyUniverseDomain(context.Background(), "", true, meta.URL, meta.Client()); err == nil {
		t.Errorf("metadata answer under googleapis.com = (%q, nil), want an error", got)
	}

	// The commercial domain itself, and a domain that only looks like it, are as before.
	for raw, want := range map[string]string{
		" GoogleAPIs.com. ":      defaultUniverse,
		"notgoogleapis.com":      "notgoogleapis.com",
		"googleapis.com.example": "googleapis.com.example",
	} {
		if got, err := resolveKubeProxyUniverseDomain(context.Background(), raw, false, "", nil); err != nil || got != want {
			t.Errorf("explicit %q = (%q, %v), want (%q, nil)", raw, got, err, want)
		}
	}
}
