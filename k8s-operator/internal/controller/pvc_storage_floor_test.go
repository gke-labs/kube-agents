package controller

import (
	"bytes"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	k8syaml "k8s.io/apimachinery/pkg/util/yaml"
	sigsyaml "sigs.k8s.io/yaml"
)

// TestRenderedPVCsMeetHyperdiskBalancedMinimum guards against reintroducing a
// sub-4Gi PersistentVolumeClaim or StatefulSet volumeClaimTemplate anywhere in
// the operator's rendered golden fixtures, static integration overlays, or
// standalone examples. On C3-only GKE clusters whose default StorageClass is
// hyperdisk-balanced, pd.csi.storage.gke.io rejects CreateVolume below 4 GB
// with HTTP 400 InvalidArgument instead of rounding up.
func TestRenderedPVCsMeetHyperdiskBalancedMinimum(t *testing.T) {
	floor := resource.MustParse("4Gi")

	roots := []string{
		"../testing/testdata/platform/expected",
		"../../config/integrations",
		"../../../examples",
	}

	var paths []string
	for _, root := range roots {
		err := filepath.WalkDir(filepath.Clean(root), func(path string, d fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if d.IsDir() {
				return nil
			}
			ext := strings.ToLower(filepath.Ext(path))
			if ext == ".yaml" || ext == ".yml" {
				paths = append(paths, path)
			}
			return nil
		})
		if err != nil {
			t.Fatalf("walk %s: %v", root, err)
		}
	}

	checked := 0
	for _, rel := range paths {
		raw, err := os.ReadFile(rel)
		if err != nil {
			t.Fatalf("read %s: %v", rel, err)
		}
		dec := k8syaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), 4096)
		for {
			var doc map[string]any
			if err := dec.Decode(&doc); err != nil {
				if err == io.EOF {
					break
				}
				t.Fatalf("decode %s: %v", rel, err)
			}
			if doc == nil {
				continue
			}
			buf, err := sigsyaml.Marshal(doc)
			if err != nil {
				t.Fatalf("marshal %s: %v", rel, err)
			}
			kind, _ := doc["kind"].(string)
			switch kind {
			case "PersistentVolumeClaim":
				var pvc corev1.PersistentVolumeClaim
				if err := sigsyaml.Unmarshal(buf, &pvc); err != nil {
					t.Fatalf("unmarshal PVC in %s: %v", rel, err)
				}
				req := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
				checked++
				if req.Cmp(floor) < 0 {
					t.Errorf("%s PVC %s requests %s, below Hyperdisk Balanced 4Gi floor", rel, pvc.Name, req.String())
				}
			case "StatefulSet":
				var sts appsv1.StatefulSet
				if err := sigsyaml.Unmarshal(buf, &sts); err != nil {
					t.Fatalf("unmarshal StatefulSet in %s: %v", rel, err)
				}
				for _, vct := range sts.Spec.VolumeClaimTemplates {
					req := vct.Spec.Resources.Requests[corev1.ResourceStorage]
					checked++
					if req.Cmp(floor) < 0 {
						t.Errorf("%s StatefulSet %s volumeClaimTemplate %s requests %s, below Hyperdisk Balanced 4Gi floor",
							rel, sts.Name, vct.Name, req.String())
					}
				}
			}
		}
	}
	if checked == 0 {
		t.Fatal("checked 0 claims; fixture roots may have moved")
	}
}
