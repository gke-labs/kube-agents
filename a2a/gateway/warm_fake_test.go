package gateway

import (
	"context"
	"slices"
	"time"
)

// The fake spawner's warm pods (warm_pool.go).

func (s *fakeSpawner) SpawnWarm(_ context.Context, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.warmSpawnErr != nil {
		return s.warmSpawnErr
	}
	s.warmSpawns = append(s.warmSpawns, name)
	s.warm = append(s.warm, warmPod{Name: name, Created: time.Now()})
	return nil
}

func (s *fakeSpawner) WarmPods(context.Context) ([]warmPod, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.warmErr != nil {
		return nil, s.warmErr
	}
	return append([]warmPod(nil), s.warm...), nil
}

// setWarm replaces the warm pods the fake lists.
func (s *fakeSpawner) setWarm(p ...warmPod) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.warm = p
}

func (s *fakeSpawner) warmSpawned() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.warmSpawns...)
}

// dropWarm removes the named pods from the fake's warm list, as a delete
// that completed would.
func (s *fakeSpawner) dropWarm(names ...string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.warm = slices.DeleteFunc(s.warm, func(p warmPod) bool { return slices.Contains(names, p.Name) })
}
