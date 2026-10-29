/*
Copyright 2026 Google LLC

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

// The channel feeds: event triage sessions under #alerts and scheduled
// checks under #scheduled, each with its summary line and reply count.

package main

import (
	"context"
	"fmt"
	"net/http"
)

const (
	// channelListLimit is how many sessions one channel request reads from
	// Hermes, newest activity first. It is Hermes' own maximum page size.
	channelListLimit = 200
	// channelPostsLimit is how many posts a channel returns.
	channelPostsLimit = 50
)

// channelKinds maps a channel name to the session kind it lists.
var channelKinds = map[string]string{
	"alerts":    kindEventTriage,
	"scheduled": kindScheduled,
}

// handleChannelPosts lists a channel's newest posts, newest activity first.
// It reads only sessions the gateway API created, so a chat platform's
// session never reaches a channel even when its title looks like one.
func (s *server) handleChannelPosts(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	kind, ok := channelKinds[name]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_channel", "The console has two channels: alerts and scheduled.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), sessionListTimeout)
	defer cancel()
	listed, failure := s.listSessions(ctx, fmt.Sprintf("/api/sessions?source=%s&limit=%d", sourceAPIServer, channelListLimit))
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return
	}
	posts := make([]recentSession, 0, channelPostsLimit)
	for _, sess := range listed {
		if sess.Kind == kind && len(posts) < channelPostsLimit {
			posts = append(posts, sess)
		}
	}
	s.attachSummaries(r.Context(), posts)
	writeJSON(w, http.StatusOK, map[string]any{"channel": name, "posts": posts})
}
