/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package webhook

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const storageSizePath = "spec.storage.size"

// storageAgent is a valid PlatformAgent whose spec.storage.size is size, or
// which has no storage block when size is "".
func storageAgent(size string) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "my-project", ClusterName: "my-cluster"},
		},
	}
	if size != "" {
		agent.Spec.Storage = &agentv1alpha1.PlatformAgentStorageSpec{Size: ptr.To(resource.MustParse(size))}
	}
	return agent
}

func TestStorageSizeValidation(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{}

	t.Run("refuses a lower size with a message on spec.storage.size", func(t *testing.T) {
		_, err := val.ValidateUpdate(ctx, storageAgent("80Gi"), storageAgent("60Gi"))
		assertFieldError(t, err, storageSizePath)
		for _, want := range []string{"cannot lower the data volume size from 80Gi to 60Gi", "cannot shrink", "Keep 80Gi or raise it"} {
			if err != nil && !strings.Contains(err.Error(), want) {
				t.Errorf("error does not say %q: %v", want, err)
			}
		}
	})

	t.Run("refuses a size below the default when the stored object had none", func(t *testing.T) {
		_, err := val.ValidateUpdate(ctx, storageAgent(""), storageAgent("40Gi"))
		assertFieldError(t, err, storageSizePath)
	})

	t.Run("allows a raise", func(t *testing.T) {
		if _, err := val.ValidateUpdate(ctx, storageAgent("60Gi"), storageAgent("80Gi")); err != nil {
			t.Errorf("a raise was refused: %v", err)
		}
		if _, err := val.ValidateUpdate(ctx, storageAgent(""), storageAgent("60Gi")); err != nil {
			t.Errorf("a first size above the default was refused: %v", err)
		}
	})

	t.Run("allows the same size written another way", func(t *testing.T) {
		if _, err := val.ValidateUpdate(ctx, storageAgent("1Ti"), storageAgent("1024Gi")); err != nil {
			t.Errorf("an equal size was refused: %v", err)
		}
	})

	t.Run("allows removing the field with a warning", func(t *testing.T) {
		warnings, err := val.ValidateUpdate(ctx, storageAgent("80Gi"), storageAgent(""))
		if err != nil {
			t.Fatalf("removing spec.storage.size was refused: %v", err)
		}
		joined := strings.Join(warnings, "\n")
		for _, want := range []string{"spec.storage.size was removed", "80Gi", "keep their current size"} {
			if !strings.Contains(joined, want) {
				t.Errorf("warnings do not say %q: %v", want, warnings)
			}
		}
	})

	t.Run("removing a size below the default warns of nothing", func(t *testing.T) {
		warnings, err := val.ValidateUpdate(ctx, storageAgent("20Gi"), storageAgent(""))
		if err != nil {
			t.Fatalf("removing spec.storage.size was refused: %v", err)
		}
		if len(warnings) != 0 {
			t.Errorf("the default is a raise here, yet the update warned: %v", warnings)
		}
	})

	t.Run("refuses a size of zero or less on create and update", func(t *testing.T) {
		for _, size := range []string{"0", "-1Gi"} {
			_, err := val.ValidateCreate(ctx, storageAgent(size))
			assertFieldError(t, err, storageSizePath)
			if err != nil && !strings.Contains(err.Error(), "greater than zero") {
				t.Errorf("create with %s: error does not say why: %v", size, err)
			}
			_, err = val.ValidateUpdate(ctx, storageAgent(""), storageAgent(size))
			assertFieldError(t, err, storageSizePath)
		}
	})

	t.Run("refuses a size above 64Ti on create and update", func(t *testing.T) {
		for _, size := range []string{"64Ti", "65536Gi"} {
			if _, err := val.ValidateCreate(ctx, storageAgent(size)); err != nil {
				t.Errorf("create with %s, the largest disk, was refused: %v", size, err)
			}
		}
		for _, size := range []string{"65537Gi", "9Ei"} {
			_, err := val.ValidateCreate(ctx, storageAgent(size))
			assertFieldError(t, err, storageSizePath)
			if err != nil && !strings.Contains(err.Error(), "at most 64Ti") {
				t.Errorf("create with %s: error does not say the limit: %v", size, err)
			}
			_, err = val.ValidateUpdate(ctx, storageAgent("80Gi"), storageAgent(size))
			assertFieldError(t, err, storageSizePath)
		}
	})

	t.Run("an out-of-range size already stored does not block other updates", func(t *testing.T) {
		for _, size := range []string{"0", "65Ti"} {
			if _, err := val.ValidateUpdate(ctx, storageAgent(size), storageAgent(size)); err != nil {
				t.Errorf("an update that keeps the stored %s was refused: %v", size, err)
			}
		}
	})

	t.Run("lowering a stored size above 64Ti to 64Ti is allowed", func(t *testing.T) {
		if _, err := val.ValidateUpdate(ctx, storageAgent("65Ti"), storageAgent("64Ti")); err != nil {
			t.Errorf("65Ti to 64Ti was refused: %v", err)
		}
		_, err := val.ValidateUpdate(ctx, storageAgent("65Ti"), storageAgent("1Ti"))
		assertFieldError(t, err, storageSizePath)
	})

	t.Run("allows a positive size on create, also below the default", func(t *testing.T) {
		if _, err := val.ValidateCreate(ctx, storageAgent("20Gi")); err != nil {
			t.Errorf("create with 20Gi was refused: %v", err)
		}
	})

	t.Run("a nil stored object checks nothing but the value", func(t *testing.T) {
		if _, err := val.ValidateUpdate(ctx, nil, storageAgent("20Gi")); err != nil {
			t.Errorf("an update with no stored object was refused: %v", err)
		}
	})
}
