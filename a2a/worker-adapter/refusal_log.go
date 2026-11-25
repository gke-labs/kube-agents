package workeradapter

import (
	"bytes"
	"log/slog"
	"regexp"
	"strconv"
	"sync"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// refusalLineMax caps a line refusalLines holds; a longer one is dropped
// whole, never logged in part.
const refusalLineMax = 512

// refusalLine is a harness's refused-call line exactly: the prefix, a
// Go-quoted tool name and a reason of plain words. Nothing else on the
// harness's stderr, which can carry model or tool text, is logged.
var refusalLine = regexp.MustCompile(`^` + regexp.QuoteMeta(coreagentwire.RefusalLogPrefix) + `("(?:[^"\\]|\\.){1,200}") \(([a-z' ]{1,64})\)(?: call ("(?:[^"\\]|\\.){1,200}"))?$`)

// refusalLines is a harness stderr writer that logs each refused-call line
// the harness writes (coreagentwire.RefusalLogPrefix), with its tool and
// reason as fields, and drops everything else.
type refusalLines struct {
	log  *slog.Logger
	mu   sync.Mutex
	buf  []byte
	skip bool // inside a line already over refusalLineMax
}

func (r *refusalLines) Write(b []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := len(b)
	for len(b) > 0 {
		i := bytes.IndexByte(b, '\n')
		chunk := b
		if i >= 0 {
			chunk = b[:i]
		}
		if !r.skip {
			if len(r.buf)+len(chunk) > refusalLineMax {
				r.skip, r.buf = true, r.buf[:0]
			} else {
				r.buf = append(r.buf, chunk...)
			}
		}
		if i < 0 {
			break
		}
		if !r.skip {
			r.emit(r.buf)
		}
		r.buf, r.skip = r.buf[:0], false
		b = b[i+1:]
	}
	return n, nil
}

func (r *refusalLines) emit(line []byte) {
	m := refusalLine.FindSubmatch(line)
	if m == nil || r.log == nil {
		return
	}
	tool, err := strconv.Unquote(string(m[1]))
	if err != nil {
		return
	}
	attrs := []any{"tool", tool, "reason", string(m[2])}
	if len(m[3]) > 0 {
		call, err := strconv.Unquote(string(m[3]))
		if err != nil {
			return
		}
		attrs = append(attrs, "call", call)
	}
	r.log.Info("the harness refused a tool call", attrs...)
}
