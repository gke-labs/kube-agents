package evals

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type Incident struct {
	ID              string           `json:"id" yaml:"id"`
	Title           string           `json:"title" yaml:"title"`
	Class           string           `json:"class" yaml:"class"`
	Severity        string           `json:"severity" yaml:"severity"`
	Status          string           `json:"status" yaml:"status"`
	FirstTimestamp  string           `json:"firstTimestamp" yaml:"firstTimestamp"`
	LastTimestamp   string           `json:"lastTimestamp" yaml:"lastTimestamp"`
	InvolvedObjects []InvolvedObject `json:"involvedObjects" yaml:"involvedObjects"`
	Events          []EventRef       `json:"events" yaml:"events"`
	Description     string           `json:"description" yaml:"description"`
}

type InvolvedObject struct {
	APIVersion string `json:"apiVersion" yaml:"apiVersion"`
	Kind       string `json:"kind" yaml:"kind"`
	Namespace  string `json:"namespace" yaml:"namespace"`
	Name       string `json:"name" yaml:"name"`
	UID        string `json:"uid" yaml:"uid"`
}

type EventRef struct {
	Name string `json:"name" yaml:"name"`
	UID  string `json:"uid" yaml:"uid"`
}

// CategorizeEvents processes raw Kubernetes events and groups/categorizes them into Incidents.
func CategorizeEvents(events []corev1.Event) ([]Incident, error) {
	var incidents []Incident

	for _, event := range events {
		if event.Reason == "DeletingNode" {
			obj := InvolvedObject{
				APIVersion: event.InvolvedObject.APIVersion,
				Kind:       event.InvolvedObject.Kind,
				Namespace:  event.InvolvedObject.Namespace,
				Name:       event.InvolvedObject.Name,
				UID:        string(event.InvolvedObject.UID),
			}

			if obj.APIVersion == "" {
				obj.APIVersion = "v1"
			}

			eventRef := EventRef{
				Name: event.Name,
				UID:  string(event.UID),
			}

			incident := Incident{
				Title:           fmt.Sprintf("Node %s deleted because it does not exist in the cloud provider", event.InvolvedObject.Name),
				Class:           "NodeLifecycle",
				Severity:        "Info",
				Status:          "Active",
				FirstTimestamp:  formatTime(event.FirstTimestamp),
				LastTimestamp:   formatTime(event.LastTimestamp),
				InvolvedObjects: []InvolvedObject{obj},
				Events:          []EventRef{eventRef},
				Description:     event.Message,
			}

			// Generate a deterministic ID: sha256 hash of involved object UID + class
			hash := sha256.Sum256([]byte(obj.UID + ":" + incident.Class))
			incident.ID = hex.EncodeToString(hash[:])

			incidents = append(incidents, incident)
		}
	}

	return incidents, nil
}

func formatTime(t metav1.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.Time.UTC().Format("2006-01-02T15:04:05Z")
}
