//go:build linux

package workeradapter

import (
	"os"
	"os/exec"
	"strconv"
	"testing"
)

// TestProtectProcessMakesTheEnvironUnreadableToChildren: once the adapter is
// not dumpable, a child running as the same user can no longer read the
// adapter's /proc/<pid>/environ. Run in a re-exec of the test binary, so the
// test process itself stays dumpable.
func TestProtectProcessMakesTheEnvironUnreadableToChildren(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root reads /proc whatever the dumpable flag")
	}
	if os.Getenv("WA_PROTECT_CHILD") == "1" {
		if err := protectProcess(); err != nil {
			t.Fatal(err)
		}
		if d, err := processDumpable(); err != nil || d {
			t.Fatalf("dumpable = %v, %v after protectProcess", d, err)
		}
		out, err := exec.Command("/bin/cat", "/proc/"+strconv.Itoa(os.Getpid())+"/environ").CombinedOutput()
		if err == nil {
			t.Fatalf("a same-user child read the protected environ: %d bytes", len(out))
		}
		return
	}
	cmd := exec.Command(os.Args[0], "-test.run", "^TestProtectProcessMakesTheEnvironUnreadableToChildren$", "-test.count=1")
	cmd.Env = append(os.Environ(), "WA_PROTECT_CHILD=1", "WA_PROTECT_SECRET=s3cret")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("child: %v\n%s", err, out)
	}
}
