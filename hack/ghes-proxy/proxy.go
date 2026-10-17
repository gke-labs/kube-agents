// Copyright 2026 The Kubernetes Authors.
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

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"mime"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Config is what the proxy needs to know about the two sides.
type Config struct {
	// PublicHost is the name the clients use for the proxy, as
	// "host" or "host:port". The rewrites write it into URLs.
	PublicHost string
	// UpstreamAPIHost serves the REST and GraphQL APIs (api.github.com).
	UpstreamAPIHost string
	// UpstreamWebHost serves git smart HTTP and the web URLs (github.com).
	UpstreamWebHost string
	// EnterpriseVersion is the value of the X-GitHub-Enterprise-Version
	// response header.
	EnterpriseVersion string
	// MaxRewriteBytes is the largest JSON body the proxy reads whole to
	// rewrite. A larger body goes through as it is.
	MaxRewriteBytes int64
}

const (
	// DefaultEnterpriseVersion is the GitHub Enterprise Server release the
	// proxy says it is.
	DefaultEnterpriseVersion = "3.17.0"
	// DefaultMaxRewriteBytes is the default of Config.MaxRewriteBytes.
	DefaultMaxRewriteBytes = 64 << 20
	// MaxMaxRewriteBytes is the largest Config.MaxRewriteBytes: the proxy
	// holds a body this large in memory, and the read limit adds one to it.
	MaxMaxRewriteBytes = 1 << 30

	enterpriseVersionHeader = "X-GitHub-Enterprise-Version"
	apiPrefix               = "/api/v3"
	graphQLPath             = "/api/graphql"
)

type routeKind int

const (
	routeAPI routeKind = iota + 1
	routeGit
)

func (k routeKind) String() string {
	switch k {
	case routeAPI:
		return "api"
	case routeGit:
		return "git"
	}
	return "none"
}

type route struct {
	kind   routeKind
	target *url.URL
}

type routeKey struct{}

// gitPathRE is the path of a git smart HTTP request:
// /<owner>/<repo>.git/<service endpoint>.
var gitPathRE = regexp.MustCompile(`^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git/(info/refs|git-upload-pack|git-receive-pack)$`)

var gitServices = map[string]bool{"git-upload-pack": true, "git-receive-pack": true}

// classify maps an incoming request to its upstream, or returns false when the
// proxy does not forward it. It decides on the escaped path, the bytes it
// forwards: a decision on the decoded path would accept /api/v3%2Fuser as
// /api/v3/user and then forward a path that does not start with a slash.
func (p *proxy) classify(r *http.Request) (route, bool) {
	escaped := r.URL.EscapedPath()
	switch {
	case escaped == graphQLPath:
		return p.target(routeAPI, p.cfg.UpstreamAPIHost, "/graphql", r.URL.RawQuery), true
	case escaped == apiPrefix || strings.HasPrefix(escaped, apiPrefix+"/"):
		rest := strings.TrimPrefix(escaped, apiPrefix)
		if rest == "" {
			rest = "/"
		}
		return p.target(routeAPI, p.cfg.UpstreamAPIHost, rest, r.URL.RawQuery), true
	}
	// The segment class has no %, so an escaped byte in a git path refuses it.
	m := gitPathRE.FindStringSubmatch(escaped)
	if m == nil || isDotSegment(m[1]) || isDotSegment(m[2]) {
		return route{}, false
	}
	switch m[3] {
	case "info/refs":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			return route{}, false
		}
		if !gitServices[r.URL.Query().Get("service")] {
			return route{}, false
		}
	default:
		if r.Method != http.MethodPost {
			return route{}, false
		}
	}
	return p.target(routeGit, p.cfg.UpstreamWebHost, escaped, r.URL.RawQuery), true
}

func isDotSegment(s string) bool { return s == "." || s == ".." }

func (p *proxy) target(kind routeKind, host, escapedPath, rawQuery string) route {
	u := &url.URL{Scheme: "https", Host: host, RawQuery: rawQuery}
	if unescaped, err := url.PathUnescape(escapedPath); err == nil {
		u.Path = unescaped
		if unescaped != escapedPath {
			u.RawPath = escapedPath
		}
	} else {
		u.Path = escapedPath
	}
	return route{kind: kind, target: u}
}

type proxy struct {
	cfg    Config
	logger *log.Logger
	rp     *httputil.ReverseProxy
}

// NewHandler returns the proxy. transport makes the upstream calls; logger
// gets one line for each request, and never a header value or a body.
func NewHandler(cfg Config, transport http.RoundTripper, logger *log.Logger) http.Handler {
	if cfg.EnterpriseVersion == "" {
		cfg.EnterpriseVersion = DefaultEnterpriseVersion
	}
	if cfg.MaxRewriteBytes <= 0 {
		cfg.MaxRewriteBytes = DefaultMaxRewriteBytes
	}
	if cfg.MaxRewriteBytes > MaxMaxRewriteBytes {
		cfg.MaxRewriteBytes = MaxMaxRewriteBytes
	}
	p := &proxy{cfg: cfg, logger: logger}
	p.rp = &httputil.ReverseProxy{
		Rewrite:        p.rewriteRequest,
		Transport:      transport,
		FlushInterval:  -1,
		ModifyResponse: p.modifyResponse,
		ErrorHandler:   p.errorHandler,
		ErrorLog:       log.New(io.Discard, "", 0),
	}
	return p
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
	rt, ok := p.classify(r)
	// Deferred, so that a request whose response copy aborts (ReverseProxy
	// panics with http.ErrAbortHandler) still gets its line. Only the method,
	// the route, the path and the status: no query string, no header and no
	// body, because each of them can hold a credential. The path is the
	// escaped one, so a %0A in it cannot start a second log line.
	defer func() {
		p.logger.Printf("%s %s %s %d %dB %s", r.Method, routeName(rt, ok), r.URL.EscapedPath(), rec.status, rec.written, time.Since(start).Round(time.Millisecond))
	}()
	if !ok {
		rec.Header().Set(enterpriseVersionHeader, p.cfg.EnterpriseVersion)
		http.NotFound(rec, r)
		return
	}
	p.rp.ServeHTTP(rec, r.WithContext(context.WithValue(r.Context(), routeKey{}, rt)))
}

func routeName(rt route, ok bool) string {
	if !ok {
		return "refused"
	}
	return rt.kind.String()
}

func (p *proxy) rewriteRequest(pr *httputil.ProxyRequest) {
	rt, _ := pr.In.Context().Value(routeKey{}).(route)
	target := *rt.target
	pr.Out.URL = &target
	// The upstream's own name, not the public host the client used.
	pr.Out.Host = ""
	if rt.kind == routeAPI {
		// Without an Accept-Encoding of the client's, the transport asks for
		// gzip itself and gives back the decoded body, which the JSON rewrite
		// can read. git requests keep their header and their bytes.
		pr.Out.Header.Del("Accept-Encoding")
	}
}

func (p *proxy) modifyResponse(resp *http.Response) error {
	rt, _ := resp.Request.Context().Value(routeKey{}).(route)
	resp.Header.Set(enterpriseVersionHeader, p.cfg.EnterpriseVersion)
	if loc := resp.Header.Get("Location"); loc != "" {
		resp.Header.Set("Location", p.rewriteURL(loc))
	}
	if links := resp.Header.Values("Link"); len(links) > 0 {
		resp.Header.Del("Link")
		for _, l := range links {
			resp.Header.Add("Link", p.rewriteLink(l))
		}
	}
	if rt.kind != routeAPI || !isJSON(resp.Header.Get("Content-Type")) || resp.Header.Get("Content-Encoding") != "" {
		return nil
	}
	// No body to rewrite: keep the upstream's Content-Length as it is.
	if resp.Request.Method == http.MethodHead || resp.StatusCode == http.StatusNoContent || resp.StatusCode == http.StatusNotModified {
		return nil
	}
	return p.rewriteBody(resp)
}

func (p *proxy) rewriteBody(resp *http.Response) error {
	head, err := io.ReadAll(io.LimitReader(resp.Body, p.cfg.MaxRewriteBytes+1))
	if err != nil {
		return err
	}
	if int64(len(head)) > p.cfg.MaxRewriteBytes {
		// Too large to hold: send it on as it is, the bytes read first.
		p.logger.Printf("%s: JSON body larger than %d bytes, not rewritten", resp.Request.URL.EscapedPath(), p.cfg.MaxRewriteBytes)
		resp.Body = struct {
			io.Reader
			io.Closer
		}{io.MultiReader(bytes.NewReader(head), resp.Body), resp.Body}
		return nil
	}
	_ = resp.Body.Close()
	out := rewriteJSONFields(head, p.rewriteURL)
	resp.Body = io.NopCloser(bytes.NewReader(out))
	resp.ContentLength = int64(len(out))
	resp.Header.Set("Content-Length", strconv.Itoa(len(out)))
	return nil
}

func isJSON(contentType string) bool {
	mt, _, err := mime.ParseMediaType(contentType)
	if err != nil {
		return false
	}
	return mt == "application/json" || strings.HasSuffix(mt, "+json")
}

// rewriteURL maps an upstream URL to the URL a GitHub Enterprise Server at
// the public host would give: the API host to https://<host>/api/v3/ (its
// GraphQL endpoint to /api/graphql), the web host to https://<host>/. Any
// other URL is returned unchanged.
func (p *proxy) rewriteURL(s string) string {
	api := "https://" + p.cfg.UpstreamAPIHost
	web := "https://" + p.cfg.UpstreamWebHost
	pub := "https://" + p.cfg.PublicHost
	switch {
	case s == api+"/graphql" || strings.HasPrefix(s, api+"/graphql?"):
		return pub + graphQLPath + strings.TrimPrefix(s, api+"/graphql")
	case s == api:
		return pub + apiPrefix
	case strings.HasPrefix(s, api+"/"):
		return pub + apiPrefix + "/" + strings.TrimPrefix(s, api+"/")
	case s == web:
		return pub
	case strings.HasPrefix(s, web+"/"):
		return pub + "/" + strings.TrimPrefix(s, web+"/")
	}
	return s
}

// rewriteLink rewrites each <URL> of an RFC 8288 Link header value.
func (p *proxy) rewriteLink(v string) string {
	var b strings.Builder
	for {
		open := strings.IndexByte(v, '<')
		if open < 0 {
			b.WriteString(v)
			return b.String()
		}
		end := strings.IndexByte(v[open:], '>')
		if end < 0 {
			b.WriteString(v)
			return b.String()
		}
		b.WriteString(v[:open+1])
		b.WriteString(p.rewriteURL(v[open+1 : open+end]))
		b.WriteByte('>')
		v = v[open+end+1:]
	}
}

func (p *proxy) errorHandler(w http.ResponseWriter, r *http.Request, err error) {
	// A *url.Error names the upstream URL, query string included; log the
	// cause alone.
	var uerr *url.Error
	if errors.As(err, &uerr) {
		err = uerr.Err
	}
	if !errors.Is(err, context.Canceled) {
		// A transport error can quote the request URI (the HTTP/2 encoder
		// does), so any query string in it is cut out.
		p.logger.Printf("%s %s: upstream error: %s", r.Method, r.URL.EscapedPath(), redactQuery(err.Error()))
	}
	w.WriteHeader(http.StatusBadGateway)
}

// queryRE is a query string inside an error message: from the ? to the next
// space or quote.
var queryRE = regexp.MustCompile(`\?[^\s"'\x60]*`)

func redactQuery(s string) string { return queryRE.ReplaceAllString(s, "?<redacted>") }

// rewriteFields are the JSON fields whose string values the proxy rewrites.
// The agent reads html_url as the web URL of what it wrote, and the token
// minter posts to access_tokens_url; nothing else is changed, so issue,
// comment and pull request bodies and diff text reach the agent as GitHub
// wrote them.
var rewriteFields = map[string]bool{"html_url": true, "access_tokens_url": true}

type jsonFrame struct {
	object    bool
	expectKey bool
	key       string
	keyEnd    int64
}

// rewriteJSONFields returns data with the string value of every
// rewriteFields member, at any depth, passed through fn. All other bytes are
// kept as they are. Data that is not one valid JSON value is returned
// unchanged.
func rewriteJSONFields(data []byte, fn func(string) string) []byte {
	type splice struct {
		start, end int64
		value      string
	}
	var splices []splice
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var stack []*jsonFrame
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return data
		}
		var top *jsonFrame
		if len(stack) > 0 {
			top = stack[len(stack)-1]
		}
		if d, ok := tok.(json.Delim); ok {
			switch d {
			case '{', '[':
				if top != nil && top.object {
					top.expectKey = true
				}
				stack = append(stack, &jsonFrame{object: d == '{', expectKey: true})
			default:
				if len(stack) == 0 {
					return data
				}
				stack = stack[:len(stack)-1]
			}
			continue
		}
		if top != nil && top.object && top.expectKey {
			key, _ := tok.(string)
			top.key, top.keyEnd, top.expectKey = key, dec.InputOffset(), false
			continue
		}
		if top != nil && top.object {
			top.expectKey = true
			if s, ok := tok.(string); ok && rewriteFields[top.key] {
				if repl := fn(s); repl != s {
					end := dec.InputOffset()
					quote := bytes.IndexByte(data[top.keyEnd:end], '"')
					if quote < 0 {
						return data
					}
					splices = append(splices, splice{start: top.keyEnd + int64(quote), end: end, value: repl})
				}
			}
		}
	}
	if dec.More() || len(stack) != 0 {
		return data
	}
	if len(splices) == 0 {
		return data
	}
	var out bytes.Buffer
	out.Grow(len(data) + 64*len(splices))
	var last int64
	for _, s := range splices {
		out.Write(data[last:s.start])
		out.Write(encodeJSONString(s.value))
		last = s.end
	}
	out.Write(data[last:])
	return out.Bytes()
}

func encodeJSONString(s string) []byte {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return bytes.TrimRight(b.Bytes(), "\n")
}

// statusRecorder notes the status and the byte count for the access log.
// Unwrap lets http.ResponseController reach Flush on the real writer, so the
// streamed git responses are flushed as they arrive.
type statusRecorder struct {
	http.ResponseWriter
	status      int
	written     int64
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(code int) {
	// ReverseProxy forwards a 1xx reply (100 Continue) through WriteHeader
	// before the final status; log the final one.
	if !r.wroteHeader && code >= 200 {
		r.status, r.wroteHeader = code, true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	r.wroteHeader = true
	n, err := r.ResponseWriter.Write(b)
	r.written += int64(n)
	return n, err
}

func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }
