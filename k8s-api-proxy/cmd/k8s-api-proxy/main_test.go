package main

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

const testIntKey = "KUBE_PROXY_TEST_INT"

func TestEnvIntOrDefault(t *testing.T) {
	cases := []struct {
		name, raw string
		want      int
		alert     bool
	}{
		{name: "unset", raw: "", want: 7},
		{name: "integer", raw: " 9100 ", want: 9100},
		{name: "not an integer", raw: "80x", want: 7, alert: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(testIntKey, tc.raw)
			var buf bytes.Buffer
			prev := log.Writer()
			log.SetOutput(&buf)
			t.Cleanup(func() { log.SetOutput(prev) })

			if got := envIntOrDefault(testIntKey, 7); got != tc.want {
				t.Errorf("envIntOrDefault = %d, want %d", got, tc.want)
			}
			out := buf.String()
			if !tc.alert {
				if out != "" {
					t.Errorf("logged %q, want nothing", out)
				}
				return
			}
			// The ALERT names the key it was given and the fallback it used,
			// not the metrics port's variable or 0.
			for _, want := range []string{"ALERT", testIntKey + `="80x"`, "using 7"} {
				if !strings.Contains(out, want) {
					t.Errorf("logged %q, want it to contain %q", out, want)
				}
			}
		})
	}
}
