package mcp

import (
	"strings"
	"testing"
)

func TestInertExportsMatchInspectPRRendering(t *testing.T) {
	const body = "### Next step\nrun ``` this\x1b[31m\n"
	var got, want strings.Builder
	WriteInertBlock(&got, body, fenceText, maxDescriptionBytes, 0, maxProseLineBytes)
	writeBlock(&want, body, fenceText, maxDescriptionBytes, 0, maxProseLineBytes)
	if got.String() != want.String() {
		t.Fatalf("WriteInertBlock = %q; want %q", got.String(), want.String())
	}
	if f := InertField("a|b`c"); f != field("a|b`c") {
		t.Fatalf("InertField = %q; want %q", f, field("a|b`c"))
	}
}
