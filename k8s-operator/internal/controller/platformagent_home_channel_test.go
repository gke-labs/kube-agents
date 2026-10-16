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
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TestHomeChannelUnsetMessageFollowsTheGatewaysBackend: the advisory exists
// exactly when the next stack's gateway holds a chat backend whose home
// channel is empty (#3320). Today's mode posts through the agent, so it never
// applies there; with both chats enabled the gateway holds Chat, so only
// Chat's home channel counts.
func TestHomeChannelUnsetMessageFollowsTheGatewaysBackend(t *testing.T) {
	slackHome := func(mode, home string) func() string {
		return func() string {
			a := slackTestAgent(mode, true)
			a.Spec.Integration.Slack.HomeChannel = home
			return homeChannelUnsetMessage(a)
		}
	}
	chatHome := func(mode, home string) func() string {
		return func() string {
			a := gchatTestAgent(mode, true)
			a.Spec.Integration.GoogleChat.HomeChannel = home
			return homeChannelUnsetMessage(a)
		}
	}
	bothChatHomeOnly := func() string {
		a := chatAndSlackTestAgent("next")
		a.Spec.Integration.GoogleChat.HomeChannel = "spaces/AAA"
		return homeChannelUnsetMessage(a)
	}
	for _, tc := range []struct {
		name  string
		msg   func() string
		names []string // empty: no condition
	}{
		{"next slack, no home", slackHome("next", ""), []string{"Slack", "spec.integration.slack.homeChannel", "SLACK_HOME_CHANNEL"}},
		{"next slack, blank home", slackHome("next", "  "), []string{"spec.integration.slack.homeChannel"}},
		{"next slack, home set", slackHome("next", "C0123ABCD"), nil},
		{"today slack, no home", slackHome("today", ""), nil},
		{"next chat, no home", chatHome("next", ""), []string{"Google Chat", "spec.integration.googleChat.homeChannel", "GOOGLE_CHAT_HOME_CHANNEL"}},
		{"next chat, home set", chatHome("next", "spaces/AAA"), nil},
		{"today chat, no home", chatHome("", ""), nil},
		{"next chat and slack, chat home set", bothChatHomeOnly, nil},
		{"next, no chat", func() string { return homeChannelUnsetMessage(a2aTestAgent()) }, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := tc.msg()
			if len(tc.names) == 0 {
				if got != "" {
					t.Fatalf("want no condition, got %q", got)
				}
				return
			}
			for _, want := range tc.names {
				if !strings.Contains(got, want) {
					t.Errorf("message does not name %q: %q", want, got)
				}
			}
			if !strings.Contains(got, "dropped") {
				t.Errorf("message must say the posts are dropped, not just that a field is empty: %q", got)
			}
		})
	}
}

// TestHomeChannelUnsetConditionSetsAndClears: written True with the reason and
// message, read back as current, and removed once the message is "".
func TestHomeChannelUnsetConditionSetsAndClears(t *testing.T) {
	a := slackTestAgent("next", true)
	msg := homeChannelUnsetMessage(a)
	if homeChannelUnsetConditionCurrent(a, msg) {
		t.Fatal("a CR with no condition reads as current for a non-empty message")
	}
	setHomeChannelUnsetCondition(a, msg, metav1.Now())
	cond := meta.FindStatusCondition(a.Status.Conditions, homeChannelUnsetConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != homeChannelUnsetReason || cond.Message != msg {
		t.Fatalf("condition = %+v, want True/%s with the message", cond, homeChannelUnsetReason)
	}
	if !homeChannelUnsetConditionCurrent(a, msg) {
		t.Error("the condition just written does not read as current: every pass would write status")
	}
	if homeChannelUnsetConditionCurrent(a, msg+" reworded") {
		t.Error("a reworded message reads as current: a release's new wording would never reach the CR")
	}
	setHomeChannelUnsetCondition(a, "", metav1.Now())
	if meta.FindStatusCondition(a.Status.Conditions, homeChannelUnsetConditionType) != nil {
		t.Error("the condition survived an empty message")
	}
	if !homeChannelUnsetConditionCurrent(a, "") {
		t.Error("an absent condition does not read as current for an empty message")
	}
}
