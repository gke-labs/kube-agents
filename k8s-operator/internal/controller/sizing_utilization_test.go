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

package controller

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func idleSample(at time.Time) SizingUtilizationSample {
	return SizingUtilizationSample{
		ObservedAt:             at,
		BrokerCPUMilli:         50,
		BrokerCPUObserved:      true,
		BrokerMemoryBytes:      200 << 20,
		BrokerMemoryObserved:   true,
		BrokerChildMemObserved: true,
		BrokerSlotsObserved:    true,
		BrokerSlotCap:          8,
		SidecarMemoryBytes:     100 << 20,
		SidecarMemoryObserved:  true,
		ShellMemoryBytes:       100 << 20,
		ShellMemoryObserved:    true,
	}
}

func TestSizingUtilizationStepUpDimensions(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name        string
		autopilot   bool
		clamped     bool
		override1Gi bool
		startTier   string
		mutate      func(*SizingUtilizationSample)
		wantTier    string
	}{
		{
			name:      "standard broker cpu 75 pct promotes small to medium",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerCPUMilli = 375
				s.BrokerCPUObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "standard broker cpu 74 pct holds small",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerCPUMilli = 370
				s.BrokerCPUObserved = true
			},
			wantTier: sizingProfileSmall,
		},
		{
			name:      "autopilot broker cpu 75 pct skips medium directly to fleet",
			autopilot: true,
			clamped:   true,
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerCPUMilli = 1500
				s.BrokerCPUObserved = true
			},
			wantTier: sizingProfileFleet,
		},
		{
			name:      "autopilot broker memory 75 pct promotes small to medium",
			autopilot: true,
			clamped:   true,
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerMemoryBytes = 1536 << 20
				s.BrokerMemoryObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "broker child memory 75 pct promotes small to medium",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				budget := childMemoryBudgetForLimit(2 << 30)
				s.BrokerChildMemReservedBytes = (budget*75 + 99) / 100
				s.BrokerChildMemBudgetBytes = budget
				s.BrokerChildMemObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "broker cgroup usage 75 pct promotes medium to fleet",
			startTier: sizingProfileMedium,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerCgroupUsageBytes = 3 << 30
				s.BrokerCgroupLimitBytes = 4 << 30
				s.BrokerCgroupObserved = true
			},
			wantTier: sizingProfileFleet,
		},
		{
			name:      "broker watermark memory ratio 0.75 promotes small to medium",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerWatermarkMemoryRatio = 0.75
				s.BrokerWatermarkMemoryObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "broker watermark state ratio 0.95 does not trigger futile bump",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerWatermarkStateRatio = 0.95
				s.BrokerWatermarkStateObserved = true
			},
			wantTier: sizingProfileSmall,
		},
		{
			name:      "broker slots 75 pct (6 of 8) promotes small to medium (12 slots)",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerSlotsInUse = 6
				s.BrokerSlotCap = 8
				s.BrokerSlotsObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "broker slots 75 pct (9 of 12) promotes medium to fleet (16 slots)",
			startTier: sizingProfileMedium,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerSlotsInUse = 9
				s.BrokerSlotCap = 12
				s.BrokerSlotsObserved = true
			},
			wantTier: sizingProfileFleet,
		},
		{
			name:      "broker slots 75 pct (12 of 16) at fleet holds fleet",
			startTier: sizingProfileFleet,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerSlotsInUse = 12
				s.BrokerSlotCap = 16
				s.BrokerSlotsObserved = true
			},
			wantTier: sizingProfileFleet,
		},
		{
			name:        "memory-pinned slots (3 of 4 with 1Gi override) does not trigger futile bump",
			override1Gi: true,
			startTier:   sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.BrokerSlotsInUse = 3
				s.BrokerSlotCap = 4
				s.BrokerSlotsObserved = true
			},
			// Overridden 1Gi proxy memory limit keeps child budget at 4 slots on all tiers.
			wantTier: sizingProfileSmall,
		},
		{
			name:      "sidecar memory 75 pct promotes small to medium on standard",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.SidecarMemoryBytes = 1536 << 20
				s.SidecarMemoryObserved = true
			},
			wantTier: sizingProfileMedium,
		},
		{
			name:      "sidecar memory 75 pct on clamped autopilot does not promote",
			autopilot: true,
			clamped:   true,
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				// On clamped Autopilot agent-api-auth stays 384Mi across all tiers.
				s.SidecarMemoryBytes = 300 << 20
				s.SidecarMemoryObserved = true
			},
			wantTier: sizingProfileSmall,
		},
		{
			name:      "shell memory 75 pct skips medium and promotes small to fleet",
			startTier: sizingProfileSmall,
			mutate: func(s *SizingUtilizationSample) {
				s.ShellMemoryBytes = 1536 << 20
				s.ShellMemoryObserved = true
			},
			wantTier: sizingProfileFleet,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			now := time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)
			agent := sizingAgent(sizingProfileAuto, tc.startTier, ptr.To[int64](3), nil)
			if tc.override1Gi {
				agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{
					Resources: &corev1.ResourceRequirements{
						Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
					},
				}
			}
			scheme := setupScheme()
			objs := sizedWorkloadsOn(agent, func() sizingTier {
				tier, _ := parseSizingTier(tc.startTier)
				return tier
			}(), tc.autopilot)
			if tc.clamped {
				objs = autopilotClamped(objs)
			}
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, objs...)...).WithStatusSubresource(agent).Build()

			currentSample := idleSample(now)
			tc.mutate(&currentSample)

			r := &PlatformAgentReconciler{
				Client:            cl,
				APIReader:         cl,
				Scheme:            scheme,
				Recorder:          record.NewFakeRecorder(8),
				autopilot:         tc.autopilot,
				autopilotResolved: true,
				nowFn:             func() time.Time { return now },
				UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
					return currentSample, nil
				},
			}

			// T+0m: start 5m soak — stays on startTier.
			if err := r.resolveSizingTier(context.Background(), agent); err != nil {
				t.Fatalf("t=0 resolveSizingTier: %v", err)
			}
			if agent.Status.EffectiveSizingTier != tc.startTier {
				t.Fatalf("t=0 tier = %q, want %q before soak finishes", agent.Status.EffectiveSizingTier, tc.startTier)
			}

			// T+4m59s: soak not elapsed yet.
			now = now.Add(sizingStepUpSoakDuration - time.Second)
			currentSample.ObservedAt = now
			if err := r.resolveSizingTier(context.Background(), agent); err != nil {
				t.Fatalf("t=4m59s resolveSizingTier: %v", err)
			}
			if agent.Status.EffectiveSizingTier != tc.startTier {
				t.Fatalf("t=4m59s tier = %q, want %q", agent.Status.EffectiveSizingTier, tc.startTier)
			}

			// T+5m: promotes if saturation can be relieved.
			now = now.Add(time.Second)
			currentSample.ObservedAt = now
			if err := r.resolveSizingTier(context.Background(), agent); err != nil {
				t.Fatalf("t=5m resolveSizingTier: %v", err)
			}
			if agent.Status.EffectiveSizingTier != tc.wantTier {
				t.Fatalf("t=5m tier = %q, want %q (sizing=%+v)", agent.Status.EffectiveSizingTier, tc.wantTier, agent.Status.Sizing)
			}
			if tc.wantTier != tc.startTier && (agent.Status.Sizing == nil || agent.Status.Sizing.Trigger != sizingTriggerUtilization) {
				t.Fatalf("status.sizing = %+v, want trigger=%q", agent.Status.Sizing, sizingTriggerUtilization)
			}
		})
	}
}

func TestSizingUtilizationTransientSpikeResetsSoak(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 18, 13, 0, 0, 0, time.UTC)
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](2), nil)
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()

	sample := idleSample(now)
	sample.BrokerMemoryBytes = 1600 << 20 // ~78% of 2Gi

	r := &PlatformAgentReconciler{
		Client:    cl,
		APIReader: cl,
		Scheme:    scheme,
		Recorder:  record.NewFakeRecorder(8),
		nowFn:     func() time.Time { return now },
		UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
			sample.ObservedAt = now
			return sample, nil
		},
	}

	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	if agent.Status.Sizing == nil || agent.Status.Sizing.ScaleUpSustainedSince == nil {
		t.Fatalf("expected ScaleUpSustainedSince to be recorded, got %+v", agent.Status.Sizing)
	}

	// Dip below 75% at t=4m resets the soak timer.
	now = now.Add(4 * time.Minute)
	sample.BrokerMemoryBytes = 1200 << 20 // ~58% of 2Gi
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	if agent.Status.Sizing.ScaleUpSustainedSince != nil {
		t.Fatalf("expected ScaleUpSustainedSince reset on dip, got %+v", agent.Status.Sizing.ScaleUpSustainedSince)
	}

	// Resume >= 75% at t=4m30s: at t=7m (<5m from 4m30s) it must still be small.
	now = now.Add(30 * time.Second)
	sample.BrokerMemoryBytes = 1600 << 20
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2*time.Minute + 30*time.Second)
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	if agent.Status.EffectiveSizingTier != sizingProfileSmall {
		t.Fatalf("tier = %q at t=7m after reset soak, want small", agent.Status.EffectiveSizingTier)
	}

	// Complete the fresh 5m soak at t=9m30s.
	now = now.Add(2*time.Minute + 30*time.Second)
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	if agent.Status.EffectiveSizingTier != sizingProfileMedium {
		t.Fatalf("tier = %q after fresh 5m soak, want medium", agent.Status.EffectiveSizingTier)
	}
}

func TestSizingUtilizationStepDownCooldownDeadbandAndFloor(t *testing.T) {
	t.Parallel()

	t.Run("fleet steps down one tier at a time honoring 25m soak and 30m cooldown", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 14, 0, 0, 0, time.UTC)
		now := t0
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](3), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileFleet,
			DesiredTier:        sizingProfileFleet,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()

		sample := idleSample(now) // <= 55% of medium and small across all dimensions
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				sample.ObservedAt = now
				return sample, nil
			},
		}

		// Start soak at t0.
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		// At t0+26m: 25m soak satisfied, but 30m cooldown has not elapsed yet.
		now = t0.Add(26 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("t+26m tier = %q, want fleet while cooldown remains", agent.Status.EffectiveSizingTier)
		}

		// At t0+30m: steps down fleet -> medium (never skipping directly to small).
		now = t0.Add(sizingTransitionCooldown)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("t+30m tier = %q, want medium", agent.Status.EffectiveSizingTier)
		}

		// Another 30m cooldown + 25m soak from t0+40m -> medium -> small at t0+65m.
		now = t0.Add(40 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = t0.Add(64 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("t+64m tier = %q, want medium while 25m soak has 1m remaining", agent.Status.EffectiveSizingTier)
		}
		now = t0.Add(65 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("t+65m tier = %q, want small", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("deadband between 56 and 74 pct of lower tier holds current tier", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 15, 0, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](2), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()

		sample := idleSample(now)
		sample.BrokerMemoryBytes = 1230 << 20 // ~60% of small (2Gi): sits in 55%..75% deadband
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				sample.ObservedAt = now
				return sample, nil
			},
		}

		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q inside deadband, want medium", agent.Status.EffectiveSizingTier)
		}
		if agent.Status.Sizing.ScaleDownSustainedSince != nil {
			t.Fatalf("ScaleDownSustainedSince = %v inside deadband, want nil", agent.Status.Sizing.ScaleDownSustainedSince)
		}
	})

	t.Run("cluster count floor blocks step down below earned tier", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 16, 0, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerClusterCount,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
		sample := idleSample(now)
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				sample.ObservedAt = now
				return sample, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q with 12 clusters, want medium floor", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("missing metrics never trigger step down", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 17, 0, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](2), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileFleet,
			DesiredTier:        sizingProfileFleet,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				// Omit shell memory observation while fleet -> medium shrinks shell 4Gi -> 2Gi.
				s := idleSample(now)
				s.ShellMemoryObserved = false
				return s, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("tier = %q with missing shell metric, want fleet", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("missing broker child memory metric blocks step down even when pod metrics are idle", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 17, 15, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](2), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileFleet,
			DesiredTier:        sizingProfileFleet,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				// PodMetrics (CPU, memory, sidecar, shell) are present and idle,
				// but /metrics failed so BrokerChildMemObserved is false.
				s := idleSample(now)
				s.BrokerChildMemObserved = false
				return s, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("tier = %q with missing broker child memory metric, want fleet", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("missing broker slots metric blocks step down when slot cap shrinks", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 17, 20, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](2), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(now)
				s.BrokerSlotsObserved = false
				return s, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q with missing broker slots metric, want medium", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("broker slots above 55 pct of lower tier blocks step down", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 17, 25, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](2), nil)
		lastTrans := metav1.NewTime(t0)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(now)
				// 6/12 is 50% of medium, but 6/8 is 75% (>55%) of small.
				s.BrokerSlotsInUse = 6
				s.BrokerSlotCap = 12
				return s, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(45 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q with 6 active slots (>55%% of 8), want medium", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("scaleToZero skips missing sidecar memory while non-scaled gateway stays fail-closed", func(t *testing.T) {
		t.Parallel()
		t0 := time.Date(2026, 11, 18, 17, 30, 0, 0, time.UTC)
		now := t0.Add(time.Hour)
		agentPaused := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](2), nil)
		agentPaused.Spec.Deployment.ScaleToZero = ptr.To(true)
		lastTrans := metav1.NewTime(t0)
		agentPaused.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		scheme := setupScheme()
		clPaused := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agentPaused).WithStatusSubresource(agentPaused).Build()
		rPaused := &PlatformAgentReconciler{
			Client:    clPaused,
			APIReader: clPaused,
			Scheme:    scheme,
			Recorder:  record.NewFakeRecorder(8),
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(now)
				s.SidecarMemoryBytes = 0
				s.SidecarMemoryObserved = false
				return s, nil
			},
		}
		if err := rPaused.resolveSizingTier(context.Background(), agentPaused); err != nil {
			t.Fatal(err)
		}
		now = now.Add(sizingStepDownSoakDuration)
		if err := rPaused.resolveSizingTier(context.Background(), agentPaused); err != nil {
			t.Fatal(err)
		}
		if agentPaused.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("scaleToZero tier = %q without sidecar metrics, want small", agentPaused.Status.EffectiveSizingTier)
		}

		// Without scaleToZero, missing sidecar memory must remain fail-closed.
		nowActive := t0.Add(time.Hour)
		agentActive := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](2), nil)
		agentActive.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileMedium,
			DesiredTier:        sizingProfileMedium,
			Trigger:            sizingTriggerUtilization,
			LastTransitionTime: &lastTrans,
		}
		clActive := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agentActive).WithStatusSubresource(agentActive).Build()
		rActive := &PlatformAgentReconciler{
			Client:    clActive,
			APIReader: clActive,
			Scheme:    scheme,
			nowFn:     func() time.Time { return nowActive },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(nowActive)
				s.SidecarMemoryBytes = 0
				s.SidecarMemoryObserved = false
				return s, nil
			},
		}
		if err := rActive.resolveSizingTier(context.Background(), agentActive); err != nil {
			t.Fatal(err)
		}
		nowActive = nowActive.Add(45 * time.Minute)
		if err := rActive.resolveSizingTier(context.Background(), agentActive); err != nil {
			t.Fatal(err)
		}
		if agentActive.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("active gateway tier = %q with missing sidecar metric, want medium", agentActive.Status.EffectiveSizingTier)
		}
	})
}

func TestSizingSurgeQuotaPodsAndRollback(t *testing.T) {
	t.Parallel()

	t.Run("pods quota blocks surge until room is added", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		quota := &corev1.ResourceQuota{
			ObjectMeta: metav1.ObjectMeta{Name: "pod-cap", Namespace: agent.Namespace},
			Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("3")}},
			Status:     corev1.ResourceQuotaStatus{Used: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("3")}},
		}
		stored, _ := resolveSizing(t, agent, quota)
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("tier = %q with full pods quota, want small", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByQuotaReason || !strings.Contains(cond.Message, "on pods") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}

		// Opening 1 pod slot allows the surge step.
		quota.Spec.Hard[corev1.ResourcePods] = resource.MustParse("4")
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(stored, quota)...).WithStatusSubresource(stored).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8), OperatorNamespace: "kubeagents-system"}
		if err := r.resolveSizingTier(context.Background(), stored); err != nil {
			t.Fatal(err)
		}
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q after freeing 1 pod slot, want medium", stored.Status.EffectiveSizingTier)
		}
	})

	t.Run("pending surge pod past 2m rolls back tier without dropping running pod", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 0, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](3), nil)
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		activePod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-small-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				Conditions: []corev1.PodCondition{{
					Type:   corev1.PodReady,
					Status: corev1.ConditionTrue,
				}},
			},
		}
		pendingPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-surge",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(agent, "test").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}

		scheme := setupScheme()
		recorder := record.NewFakeRecorder(8)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activePod, pendingPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          recorder,
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}

		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("tier = %q after stalled surge rollback, want small", agent.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByCapacityReason || !strings.Contains(cond.Message, pendingPod.Name) {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if len(events) == 0 || !strings.Contains(events[len(events)-1], sizingCappedByCapacityReason) {
			t.Fatalf("events = %q, want %s warning", events, sizingCappedByCapacityReason)
		}

		// Verify active running pod was never deleted by the rollback.
		stillRunning := &corev1.Pod{}
		if err := cl.Get(context.Background(), client.ObjectKeyFromObject(activePod), stillRunning); err != nil {
			t.Fatalf("active broker pod missing after surge rollback: %v", err)
		}
		if stillRunning.DeletionTimestamp != nil {
			t.Fatalf("active broker pod marked for deletion on surge rollback")
		}
	})

	t.Run("off-cluster operator uses Recreate so concurrent running and pending pods do not trigger surge stall rollback", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 0, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		activePod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-small-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		pendingPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-pending",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(agent, "test").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activePod, pendingPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			Recorder:  record.NewFakeRecorder(8),
			nowFn:     func() time.Time { return now },
		}
		if stall, wait := r.sizingSurgeStalled(context.Background(), agent, sizingTierMedium, now); stall != nil || wait != 0 {
			t.Fatalf("sizingSurgeStalled off-cluster = (%+v, %s), want (nil, 0)", stall, wait)
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q, want medium when off-cluster operator skips surge stall checks", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("fleet pending surge pod does not double roll back medium to small and preserves SurgeUnschedulable", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 0, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		agent.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:          sizingProfileFleet,
			DesiredTier:         sizingProfileFleet,
			Trigger:             sizingTriggerClusterCount,
			LastObservedPeakPct: ptr.To[int32](82),
		}
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		activePod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil), "medium").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		pendingFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-fleet-surge",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(agent, "fleet").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}

		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activePod, pendingFleetPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}

		// Pass 1 rolls back fleet -> medium and sets SurgeUnschedulable.
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("pass 1 tier = %q, want medium", agent.Status.EffectiveSizingTier)
		}
		// Pass 2 immediately afterwards (while step-back hold and lingering fleet surge pod exist)
		// must keep medium AND preserve the SurgeUnschedulable condition.
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("pass 2 tier = %q, want medium (must not double-rollback to small)", agent.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByCapacityReason {
			t.Fatalf("SizingCappedByQuota after pass 2 = %+v, want SurgeUnschedulable preserved", cond)
		}
		// Even if in-memory step-back hold were absent and demand returned to medium
		// while lingering fleet surge pod remained, Pass 3 must still not double-roll medium -> small.
		r.steppedBackAt.Delete(client.ObjectKeyFromObject(agent))
		agent.Status.Usage.ClustersRegistered = ptr.To[int64](3)
		agent.Status.Usage.ClustersMonitored = ptr.To[int64](3)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("pass 3 tier = %q, want medium even without in-memory hold", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("medium to small step-down pending surge pod rolls back up to medium", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 30, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](3), nil)
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		activeMediumPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil), "medium").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		pendingSmallPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-small-surge",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(agent, "small").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient pods",
					LastTransitionTime: since,
				}},
			},
		}

		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activeMediumPod, pendingSmallPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}

		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q after medium->small surge stall, want rollback to medium", agent.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByCapacityReason || !strings.Contains(cond.Message, "transitioning to the small tier") || !strings.Contains(cond.Message, "active medium tier stays") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
	})

	t.Run("fleet to medium step-down quota refusal rolls back up to fleet and keeps active pod", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 45, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](3), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierMedium)
		proxyDep := workloads[0].(*appsv1.Deployment)
		proxyDep.Status.Conditions = []appsv1.DeploymentCondition{{
			Type:    appsv1.DeploymentReplicaFailure,
			Status:  corev1.ConditionTrue,
			Reason:  "FailedCreate",
			Message: `pods "` + credentialProxyName(agent) + `-med-" is forbidden: exceeded quota: pod-cap, requested: pods=1, used: pods=3, limited: pods=3`,
		}}
		activeFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-fleet-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileFleet, nil, nil), "fleet").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}

		scheme := setupScheme()
		objs := append([]client.Object{agent, activeFleetPod}, workloads...)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}

		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("tier = %q after fleet->medium quota refusal, want rollback to fleet", agent.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByQuotaReason || !strings.Contains(cond.Message, "the fleet tier stays") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
	})

	t.Run("surge stall matches bare tier resources when invalid CR override is stripped", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 18, 55, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		// Invalid at medium (6Gi request > 4Gi tier limit), so reconciler strips it and renders bare medium (4Gi).
		agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{
			Resources: &corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("6Gi")},
			},
		}
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		activeSmallPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-small-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil), "small").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		bareMediumAgent := sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil)
		pendingBareMediumPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-bare-surge",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(bareMediumAgent, "medium").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient memory",
					LastTransitionTime: since,
				}},
			},
		}

		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activeSmallPod, pendingBareMediumPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}

		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("tier = %q, want small when stripped bare medium surge pod stalls", agent.Status.EffectiveSizingTier)
		}
	})

	t.Run("clamped Autopilot fleet pending surge pod rolls back and clamped fleet step-down rolls back to fleet", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 19, 10, 0, 0, time.UTC)

		// (a) Step-up to fleet on clamped Autopilot: admission clamps Limits.cpu 8 -> 4.
		agentUp := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		clampedMediumWorkloads := autopilotClamped(sizedWorkloadsOn(agentUp, sizingTierMedium, true))
		activeMediumPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agentUp) + "-medium-active",
				Namespace:         agentUp.Namespace,
				Labels:            credentialProxySelector(agentUp),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   clampedMediumWorkloads[0].(*appsv1.Deployment).Spec.Template.Spec,
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		fleetDepSpec := buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileFleet, nil, nil), "fleet").Spec.Template.Spec
		for i := range fleetDepSpec.Containers {
			if fleetDepSpec.Containers[i].Name == credentialProxyContainerName {
				fleetDepSpec.Containers[i].Resources.Limits[corev1.ResourceCPU] = resource.MustParse("4")
			}
		}
		pendingClampedFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agentUp) + "-fleet-clamped-surge",
				Namespace:         agentUp.Namespace,
				Labels:            credentialProxySelector(agentUp),
				CreationTimestamp: since,
			},
			Spec: fleetDepSpec,
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}
		scheme := setupScheme()
		objsUp := append([]client.Object{agentUp, activeMediumPod, pendingClampedFleetPod}, clampedMediumWorkloads...)
		clUp := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objsUp...).WithStatusSubresource(agentUp).Build()
		rUp := &PlatformAgentReconciler{
			Client:            clUp,
			APIReader:         clUp,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			autopilot:         true,
			autopilotResolved: true,
			nowFn:             func() time.Time { return now },
		}
		if err := rUp.resolveSizingTier(context.Background(), agentUp); err != nil {
			t.Fatal(err)
		}
		if agentUp.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("clamped Autopilot surge stall tier = %q, want rollback to medium", agentUp.Status.EffectiveSizingTier)
		}

		// (b) Step-down fleet -> medium on clamped Autopilot with quota refusal rolls back to fleet (not small).
		agentDown := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](3), nil)
		medWorkloads := autopilotClamped(sizedWorkloadsOn(agentDown, sizingTierMedium, true))
		medProxyDep := medWorkloads[0].(*appsv1.Deployment)
		medProxyDep.Status.Conditions = []appsv1.DeploymentCondition{{
			Type:    appsv1.DeploymentReplicaFailure,
			Status:  corev1.ConditionTrue,
			Reason:  "FailedCreate",
			Message: `pods "` + credentialProxyName(agentDown) + `-med-" is forbidden: exceeded quota: pod-cap, requested: pods=1, used: pods=3, limited: pods=3`,
		}}
		activeClampedFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agentDown) + "-fleet-active",
				Namespace:         agentDown.Namespace,
				Labels:            credentialProxySelector(agentDown),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *fleetDepSpec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		objsDown := append([]client.Object{agentDown, activeClampedFleetPod}, medWorkloads...)
		clDown := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objsDown...).WithStatusSubresource(agentDown).Build()
		rDown := &PlatformAgentReconciler{
			Client:            clDown,
			APIReader:         clDown,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			autopilot:         true,
			autopilotResolved: true,
			nowFn:             func() time.Time { return now },
		}
		if err := rDown.resolveSizingTier(context.Background(), agentDown); err != nil {
			t.Fatal(err)
		}
		if agentDown.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("clamped Autopilot step-down refusal tier = %q, want rollback to fleet", agentDown.Status.EffectiveSizingTier)
		}
	})

	t.Run("small to fleet two-tier stall or quota refusal rolls directly back to active small tier", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 19, 15, 0, 750_000_000, time.UTC)
		since := metav1.NewTime(now.Truncate(time.Second))
		// Step 1: confirm sub-second transition timestamps truncate to whole seconds so a pod created
		// at the same second matches minCreatedAt rather than failing Before(minCreatedAt).
		setupAgent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](3), nil)
		setupCl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(setupAgent).WithStatusSubresource(setupAgent).Build()
		rTime := &PlatformAgentReconciler{
			Client: setupCl, APIReader: setupCl, Scheme: setupScheme(),
			nowFn: func() time.Time { return now },
		}
		setupAgent.Status.EffectiveSizingTier = sizingProfileSmall
		setupAgent.Status.Usage.ClustersRegistered = ptr.To[int64](25)
		if err := rTime.resolveSizingTier(context.Background(), setupAgent); err != nil {
			t.Fatal(err)
		}
		if setupAgent.Status.Sizing == nil || setupAgent.Status.Sizing.LastTransitionTime == nil {
			t.Fatalf("expected LastTransitionTime to be recorded")
		}
		if !setupAgent.Status.Sizing.LastTransitionTime.Time.Equal(since.Time) {
			t.Fatalf("LastTransitionTime = %v, want second-truncated %v", setupAgent.Status.Sizing.LastTransitionTime.Time, since.Time)
		}

		// Step 2: two-tier jump small -> fleet stalls with pending fleet surge pod + active small pod ->
		// rolls straight back to small (not untested medium), even while a prior step-back hold is active.
		later := since.Add(sizingSurgePendingTimeout + time.Second)
		agentStall := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		agentStall.Status.Sizing = &agentv1alpha1.SizingStatus{
			ActiveTier:         sizingProfileFleet,
			DesiredTier:        sizingProfileFleet,
			Trigger:            sizingTriggerClusterCount,
			LastTransitionTime: &since,
		}
		activeSmallPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name: credentialProxyName(agentStall) + "-small-active", Namespace: agentStall.Namespace,
				Labels: credentialProxySelector(agentStall), CreationTimestamp: metav1.NewTime(since.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil), "small").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		pendingFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name: credentialProxyName(agentStall) + "-fleet-surge", Namespace: agentStall.Namespace,
				Labels: credentialProxySelector(agentStall), CreationTimestamp: since,
			},
			Spec: *buildCredentialProxyDeployment(agentStall, "fleet").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type: corev1.PodScheduled, Status: corev1.ConditionFalse,
					Reason: string(corev1.PodReasonUnschedulable), Message: "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}
		scheme := setupScheme()
		clStall := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agentStall, activeSmallPod, pendingFleetPod).WithStatusSubresource(agentStall).Build()
		rStall := &PlatformAgentReconciler{
			Client: clStall, APIReader: clStall, Scheme: scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return later },
		}
		// Seed a prior stepBackHold to confirm SurgeUnschedulable is never blinded for 10m on new surge pods.
		rStall.steppedBackAt.Store(client.ObjectKeyFromObject(agentStall), later.Add(-time.Minute))
		if err := rStall.resolveSizingTier(context.Background(), agentStall); err != nil {
			t.Fatal(err)
		}
		if agentStall.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("small->fleet surge stall tier = %q, want direct rollback to active small", agentStall.Status.EffectiveSizingTier)
		}

		// Step 3: two-tier small -> fleet quota refusal also rolls back to active small.
		agentRefused := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		fleetWorkloads := sizedWorkloadsAt(agentRefused, sizingTierFleet)
		fleetProxyDep := fleetWorkloads[0].(*appsv1.Deployment)
		fleetProxyDep.Status.Conditions = []appsv1.DeploymentCondition{{
			Type: appsv1.DeploymentReplicaFailure, Status: corev1.ConditionTrue, Reason: "FailedCreate",
			Message: `pods "` + credentialProxyName(agentRefused) + `-flt-" is forbidden: exceeded quota: cpu-cap, requested: requests.cpu=4, used: requests.cpu=4, limited: requests.cpu=6`,
		}}
		objsRefused := append([]client.Object{agentRefused, activeSmallPod.DeepCopy()}, fleetWorkloads...)
		clRefused := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objsRefused...).WithStatusSubresource(agentRefused).Build()
		rRefused := &PlatformAgentReconciler{
			Client: clRefused, APIReader: clRefused, Scheme: scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			autopilot:         false,
			autopilotResolved: true,
			nowFn:             func() time.Time { return later },
		}
		if err := rRefused.resolveSizingTier(context.Background(), agentRefused); err != nil {
			t.Fatal(err)
		}
		if agentRefused.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("small->fleet quota refusal tier = %q, want direct rollback to active small", agentRefused.Status.EffectiveSizingTier)
		}
	})

	t.Run("mid-rollout active old-tier broker pod pauses utilization soak to prevent false escalation", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 19, 20, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](3), nil)
		activeSmallPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-small-old-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil), "small").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activeSmallPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(now)
				s.BrokerWatermarkMemoryRatio = 0.85
				s.BrokerWatermarkMemoryObserved = true
				s.BrokerChildMemReservedBytes = 1600 << 20
				s.BrokerChildMemBudgetBytes = 1856 << 20
				return s, nil
			},
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		now = now.Add(6 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q during mid-rollout while small pod active, want medium (no false escalation to fleet)", agent.Status.EffectiveSizingTier)
		}
		if agent.Status.Sizing != nil && agent.Status.Sizing.ScaleUpSustainedSince != nil {
			t.Fatalf("ScaleUpSustainedSince = %v while old small pod active, want nil", agent.Status.Sizing.ScaleUpSustainedSince)
		}
	})

	t.Run("bursting Autopilot ephemeral-storage adjustment keeps Limits.cpu=8 matching and clones status on mismatched cluster jump", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 19, 30, 0, 0, time.UTC)

		// (a) Pod bursting Autopilot writes resource-adjustment only for ephemeral-storage,
		// so fleet pod still keeps Limits.cpu=8 and Pending surge past 2m must roll back to medium.
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		since := metav1.NewTime(now.Add(-sizingSurgePendingTimeout - time.Second))
		burstingAdj := `{"input":{"containers":[{"name":"envoy-credential-proxy","requests":{"cpu":"4","memory":"8Gi"},"limits":{"cpu":"8","memory":"8Gi","ephemeral-storage":"8Gi"}}]},"output":{"containers":[{"name":"envoy-credential-proxy","requests":{"cpu":"4","memory":"8Gi","ephemeral-storage":"8Gi"},"limits":{"cpu":"8","memory":"8Gi","ephemeral-storage":"8Gi"}}]}}`
		activeMedPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-medium-active",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil), "medium").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		pendingBurstingFleetPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-fleet-burst-surge",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				CreationTimestamp: since,
				Annotations:       map[string]string{autopilotResourceAdjustmentAnnotation: burstingAdj},
			},
			Spec: *buildCredentialProxyDeployment(agent, "fleet").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodPending,
				Conditions: []corev1.PodCondition{{
					Type:               corev1.PodScheduled,
					Status:             corev1.ConditionFalse,
					Reason:             string(corev1.PodReasonUnschedulable),
					Message:            "0/3 nodes are available: Insufficient cpu",
					LastTransitionTime: since,
				}},
			},
		}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, activeMedPod, pendingBurstingFleetPod).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{
			Client:            cl,
			APIReader:         cl,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("bursting Autopilot surge stall tier = %q, want rollback to medium", agent.Status.EffectiveSizingTier)
		}

		// (b) brokerMismatched + cluster count >= 25 clones prevStatus cleanly and updates DesiredTier/Trigger.
		agentJump := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), nil)
		origStatus := &agentv1alpha1.SizingStatus{
			ActiveTier:  sizingProfileMedium,
			DesiredTier: sizingProfileMedium,
			Trigger:     sizingTriggerUtilization,
		}
		agentJump.Status.Sizing = origStatus.DeepCopy()
		activeSmallPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agentJump) + "-small-active",
				Namespace:         agentJump.Namespace,
				Labels:            credentialProxySelector(agentJump),
				CreationTimestamp: metav1.NewTime(now.Add(-time.Hour)),
			},
			Spec:   *buildCredentialProxyDeployment(sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil), "small").Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		clJump := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agentJump, activeSmallPod).WithStatusSubresource(agentJump).Build()
		rJump := &PlatformAgentReconciler{
			Client:            clJump,
			APIReader:         clJump,
			Scheme:            scheme,
			Recorder:          record.NewFakeRecorder(8),
			OperatorNamespace: "kubeagents-system",
			nowFn:             func() time.Time { return now },
		}
		rJump.sizingUtilState.Store(client.ObjectKeyFromObject(agentJump), origStatus)
		if err := rJump.resolveSizingTier(context.Background(), agentJump); err != nil {
			t.Fatal(err)
		}
		if agentJump.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("tier = %q, want fleet on cluster jump during rollout", agentJump.Status.EffectiveSizingTier)
		}
		if agentJump.Status.Sizing == nil || agentJump.Status.Sizing.DesiredTier != sizingProfileFleet || agentJump.Status.Sizing.Trigger != sizingTriggerBoth {
			t.Fatalf("status.sizing = %+v, want DesiredTier=fleet Trigger=both", agentJump.Status.Sizing)
		}
		if origStatus.DesiredTier != sizingProfileMedium {
			t.Fatalf("origStatus mutated in place: %+v", origStatus)
		}
	})

	t.Run("pruned status.sizing CRD strips field cleanly without write storm", func(t *testing.T) {
		t.Parallel()
		now := time.Date(2026, 11, 18, 19, 0, 0, 0, time.UTC)
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](2), nil)
		writes := 0
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).
			WithInterceptorFuncs(interceptor.Funcs{
				SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
					writes++
					if err := c.SubResource(sub).Update(ctx, obj, opts...); err != nil {
						return err
					}
					if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
						// Simulate an older served CRD pruning status.sizing.
						pa.Status.Sizing = nil
						_ = c.Status().Update(ctx, pa)
					}
					return nil
				},
			}).Build()
		r := &PlatformAgentReconciler{
			Client:    cl,
			APIReader: cl,
			Scheme:    scheme,
			nowFn:     func() time.Time { return now },
			UtilizationReader: func(context.Context, *agentv1alpha1.PlatformAgent) (SizingUtilizationSample, error) {
				s := idleSample(now)
				s.BrokerMemoryBytes = 1600 << 20
				return s, nil
			},
		}

		// Pass 1 starts soak and learns status.sizing was pruned (1 write).
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		// Pass 2 at +2m does not rewrite status because prunedSizingStatus suppresses churn.
		now = now.Add(2 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if writes != 1 {
			t.Fatalf("writes = %d while soaking on pruned CRD, want 1", writes)
		}
		// Pass 3 at +5m promotes via in-memory sizingUtilState.
		now = now.Add(3 * time.Minute)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("tier = %q after 5m soak under pruned CRD, want medium", agent.Status.EffectiveSizingTier)
		}
	})
}

func TestCollectLiveUtilizationPodMetricsAndScrape(t *testing.T) {
	t.Parallel()
	promPayload := strings.Join([]string{
		fmt.Sprintf("# TYPE %s gauge", brokerMetricSlotsInUse),
		fmt.Sprintf("%s 6", brokerMetricSlotsInUse),
		fmt.Sprintf("# TYPE %s gauge", brokerMetricSlotCap),
		fmt.Sprintf("%s 8", brokerMetricSlotCap),
		fmt.Sprintf("# TYPE %s gauge", brokerMetricChildMemReserved),
		fmt.Sprintf("%s %d", brokerMetricChildMemReserved, 900<<20),
		fmt.Sprintf("# TYPE %s gauge", brokerMetricChildMemBudget),
		fmt.Sprintf("%s %d", brokerMetricChildMemBudget, 1856<<20),
		fmt.Sprintf("# TYPE %s gauge", brokerMetricCgroupMemory),
		fmt.Sprintf(`%s{kind="current"} %d`, brokerMetricCgroupMemory, 1500<<20),
		fmt.Sprintf(`%s{kind="peak"} %d`, brokerMetricCgroupMemory, 2000<<20),
		fmt.Sprintf(`%s{kind="limit"} %d`, brokerMetricCgroupMemory, 2<<30),
		fmt.Sprintf("# TYPE %s gauge", brokerMetricWatermarkRatio),
		fmt.Sprintf(`%s{resource="memory"} 0.78`, brokerMetricWatermarkRatio),
		fmt.Sprintf(`%s{resource="state"} 0.25`, brokerMetricWatermarkRatio),
		"",
	}, "\n")

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != sizingMetricsScrapePath {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(promPayload))
	}))
	defer srv.Close()

	parsedURL, err := url.Parse(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(parsedURL.Port())
	if err != nil {
		t.Fatal(err)
	}

	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](3), nil)
	brokerPod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      credentialProxyName(agent) + "-abc",
			Namespace: agent.Namespace,
			Labels:    credentialProxySelector(agent),
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			PodIP: parsedURL.Hostname(),
			Conditions: []corev1.PodCondition{{
				Type:   corev1.PodReady,
				Status: corev1.ConditionTrue,
			}},
		},
	}

	pmProxy := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": podMetricsAPIGroup + "/" + podMetricsAPIVersion,
		"kind":       "PodMetrics",
		"metadata": map[string]any{
			"name":      brokerPod.Name,
			"namespace": agent.Namespace,
			"labels":    toAnyMap(credentialProxySelector(agent)),
		},
		"containers": []any{
			map[string]any{
				"name": credentialProxyContainerName,
				"usage": map[string]any{
					"cpu":    "420m",
					"memory": "1550Mi",
				},
			},
		},
	}}
	pmShell := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": podMetricsAPIGroup + "/" + podMetricsAPIVersion,
		"kind":       "PodMetrics",
		"metadata": map[string]any{
			"name":      shellSandboxName(agent) + "-0",
			"namespace": agent.Namespace,
			"labels":    toAnyMap(shellSandboxSelector(agent)),
		},
		"containers": []any{
			map[string]any{
				"name": shellSandboxContainerName,
				"usage": map[string]any{
					"cpu":    "100m",
					"memory": "900Mi",
				},
			},
		},
	}}

	pmExecTier := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": podMetricsAPIGroup + "/" + podMetricsAPIVersion,
		"kind":       "PodMetrics",
		"metadata": map[string]any{
			"name":      credentialProxyExecTierName(agent) + "-abcde",
			"namespace": agent.Namespace,
			"labels":    toAnyMap(credentialProxyExecTierSelector(agent)),
		},
		"containers": []any{
			map[string]any{
				"name": credentialProxyContainerName,
				"usage": map[string]any{
					"cpu":    "1950m",
					"memory": "1950Mi",
				},
			},
		},
	}}

	scheme := runtime.NewScheme()
	if err := appsv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	pmGVK := schema.GroupVersionKind{Group: podMetricsAPIGroup, Version: podMetricsAPIVersion, Kind: podMetricsListKind}
	scheme.AddKnownTypeWithName(pmGVK, &unstructured.UnstructuredList{})

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, brokerPod, pmProxy, pmExecTier, pmShell).Build()

	// Route test client port 8766 to httptest.Server's ephemeral port.
	rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		cloned := req.Clone(req.Context())
		cloned.URL.Host = fmt.Sprintf("%s:%d", parsedURL.Hostname(), port)
		return http.DefaultTransport.RoundTrip(cloned)
	})

	r := &PlatformAgentReconciler{
		Client:              cl,
		APIReader:           cl,
		Scheme:              scheme,
		BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
	}

	got := r.readLiveSizingUtilization(context.Background(), agent)
	if !got.BrokerCPUObserved || got.BrokerCPUMilli != 420 {
		t.Fatalf("BrokerCPU = %d (%v), want 420", got.BrokerCPUMilli, got.BrokerCPUObserved)
	}
	wantShellMem := resource.MustParse("900Mi")
	if !got.ShellMemoryObserved || got.ShellMemoryBytes != wantShellMem.Value() {
		t.Fatalf("ShellMemory = %d (%v), want 900Mi", got.ShellMemoryBytes, got.ShellMemoryObserved)
	}
	if !got.BrokerCgroupObserved || got.BrokerCgroupUsageBytes != 1500<<20 {
		t.Fatalf("BrokerCgroup = %d (%v), want 1500Mi (not high=2000Mi)", got.BrokerCgroupUsageBytes, got.BrokerCgroupObserved)
	}
	if !got.BrokerWatermarkMemoryObserved || got.BrokerWatermarkMemoryRatio != 0.78 {
		t.Fatalf("Watermark = %v (%v), want 0.78", got.BrokerWatermarkMemoryRatio, got.BrokerWatermarkMemoryObserved)
	}
	if !got.BrokerSlotsObserved || got.BrokerSlotsInUse != 6 || got.BrokerSlotCap != 8 {
		t.Fatalf("Slots = %d/%d (%v), want 6/8", got.BrokerSlotsInUse, got.BrokerSlotCap, got.BrokerSlotsObserved)
	}

	// Off-cluster (OperatorNamespace unset and BrokerControlClient nil): must not dial :8766.
	rOffCluster := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	var offSample SizingUtilizationSample
	rOffCluster.collectBrokerMetricsSample(context.Background(), agent, &offSample)
	if offSample.BrokerSlotsObserved || offSample.BrokerCgroupObserved || offSample.BrokerWatermarkMemoryObserved {
		t.Fatalf("expected off-cluster collectBrokerMetricsSample to skip :8766 scrape, got %+v", offSample)
	}
}

func toAnyMap(in map[string]string) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
