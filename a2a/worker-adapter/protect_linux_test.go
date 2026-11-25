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
// adapter's /proc/<pid>/environ or mem (where a harness's history would
// sit), nor open its fds through /proc/<pid>/fd. Run in a re-exec of the test binary, so the
// test process itself stays dumpable.
func TestProtectProcessMakesTheEnvironUnreadableToChildren(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root reads /proc whatever the dumpable flag")
	}
	if os.Getenv("WA_PROTECT_CHILD") == "1" {
		self := "/proc/" + strconv.Itoa(os.Getpid())
		// Readable before: the refusals below then come from the flag.
		if out, err := exec.Command("/bin/cat", self+"/environ").CombinedOutput(); err != nil || len(out) == 0 {
			t.Fatalf("a same-user child could not read environ even before protectProcess: %v %q", err, out)
		}
		// Whether a same-user child can open mem at all before the flag
		// (Yama's ptrace_scope can refuse it to a child either way).
		memOpenableBefore := exec.Command("/bin/sh", "-c", "exec 3<"+self+"/mem").Run() == nil
		if err := protectProcess(); err != nil {
			t.Fatal(err)
		}
		if d, err := processDumpable(); err != nil || d {
			t.Fatalf("dumpable = %v, %v after protectProcess", d, err)
		}
		for _, f := range []string{"environ", "mem", "fd/1"} {
			// mem and fd/1 are only opened, not read: reading mem at
			// offset 0 fails on any process, since address 0 is never
			// mapped, so only the open shows the refusal.
			cmd := exec.Command("/bin/dd", "if="+self+"/"+f, "of=/dev/null", "bs=1", "count=1", "status=none")
			switch f {
			case "mem":
				if !memOpenableBefore {
					t.Logf("mem: a same-user child could not open it even before protectProcess (Yama ptrace_scope); not checked here")
					continue
				}
				cmd = exec.Command("/bin/sh", "-c", "exec 3<"+self+"/mem")
			case "fd/1":
				cmd = exec.Command("/bin/sh", "-c", "exec 3>"+self+"/fd/1")
			}
			if out, err := cmd.CombinedOutput(); err == nil {
				t.Fatalf("a same-user child opened the protected %s (%q)", f, out)
			}
		}
		return
	}
	cmd := exec.Command(os.Args[0], "-test.run", "^TestProtectProcessMakesTheEnvironUnreadableToChildren$", "-test.count=1")
	cmd.Env = append(os.Environ(), "WA_PROTECT_CHILD=1", "WA_PROTECT_SECRET=s3cret")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("child: %v\n%s", err, out)
	}
}
