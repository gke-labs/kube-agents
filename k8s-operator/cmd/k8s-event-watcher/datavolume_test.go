package main

import (
	"errors"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// The two series the operator folds into status.storageStatus, under the
// names and label it reads, with the statfs reading as their values.
func TestDataVolumeCollector_ExportsTotalAndFree(t *testing.T) {
	reg := prometheus.NewRegistry()
	reg.MustRegister(newDataVolumeCollector("/opt/data", func(path string) (uint64, uint64, error) {
		if path != "/opt/data" {
			t.Errorf("statfs path = %q, want /opt/data", path)
		}
		return 10737418240, 1073741824, nil
	}))
	want := `
# HELP kubeagents_data_volume_bytes ` + dataVolumeHelp + `
# TYPE kubeagents_data_volume_bytes gauge
kubeagents_data_volume_bytes{kind="free"} 1.073741824e+09
kubeagents_data_volume_bytes{kind="total"} 1.073741824e+10
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(want), dataVolumeMetric); err != nil {
		t.Error(err)
	}
}

// A failed statfs exports no series rather than a zero, which the operator
// would read as a full volume, and the next good read exports again.
func TestDataVolumeCollector_AFailedReadExportsNothing(t *testing.T) {
	fail := true
	reg := prometheus.NewRegistry()
	reg.MustRegister(newDataVolumeCollector("/opt/data", func(string) (uint64, uint64, error) {
		if fail {
			return 0, 0, errors.New("no such file or directory")
		}
		return 2, 1, nil
	}))
	if n, err := testutil.GatherAndCount(reg, dataVolumeMetric); err != nil || n != 0 {
		t.Errorf("failing read: %d series (err %v), want 0", n, err)
	}
	fail = false
	if n, err := testutil.GatherAndCount(reg, dataVolumeMetric); err != nil || n != 2 {
		t.Errorf("recovered read: %d series (err %v), want 2", n, err)
	}
}

// The real statfs on a directory that exists: a non-zero size, and free space
// no larger than it.
func TestDataVolumeStatfs_ReadsARealFilesystem(t *testing.T) {
	total, free, err := dataVolumeStatfs(t.TempDir())
	if err != nil {
		t.Fatalf("statfs: %v", err)
	}
	if total == 0 || free > total {
		t.Errorf("total=%d free=%d, want total > 0 and free <= total", total, free)
	}
}

// Off unless asked for: the flag is empty by default, so a watcher started
// without it exports no volume series.
func TestParseFlags_DataVolumeDefaultsOff(t *testing.T) {
	f, err := parseFlags(nil)
	if err != nil {
		t.Fatal(err)
	}
	if f.dataVolume != "" {
		t.Errorf("--data-volume default = %q, want empty", f.dataVolume)
	}
	f, err = parseFlags([]string{"--data-volume=/opt/data"})
	if err != nil {
		t.Fatal(err)
	}
	if f.dataVolume != "/opt/data" {
		t.Errorf("--data-volume = %q, want /opt/data", f.dataVolume)
	}
}
