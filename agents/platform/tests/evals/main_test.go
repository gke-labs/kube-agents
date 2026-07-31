package evals

import (
	"context"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
	"sigs.k8s.io/yaml"
)

func TestScenarios(t *testing.T) {
	testdataDir := "testdata"
	pod := &Pod{
		Name:      "platform-agent-eval",
		Namespace: "default",
	}

	// 1. Clean up any existing pod from previous runs
	cleanupCmd := exec.Command("kubectl", "--context", "kind-kube-agents", "delete", "pod", pod.Name, "--ignore-not-found=true")
	_ = cleanupCmd.Run()

	// 2. Start the test pod in kind-kube-agents, forwarding LLM API keys
	t.Log("Starting platform-agent-eval pod in kind-kube-agents...")
	var envArgs []string
	for _, env := range os.Environ() {
		if strings.HasPrefix(env, "GEMINI_") ||
			strings.HasPrefix(env, "OPENAI_") ||
			strings.HasPrefix(env, "ANTHROPIC_") ||
			strings.HasPrefix(env, "LLM_") {
			envArgs = append(envArgs, "--env="+env)
		}
	}

	runArgs := append([]string{"--context", "kind-kube-agents", "run", pod.Name, "--image=platform-agent:latest", "--image-pull-policy=Never", "--restart=Never", "--command"}, envArgs...)
	runArgs = append(runArgs, "--", "sleep", "3600")

	runCmd := exec.Command("kubectl", runArgs...)
	if output, err := runCmd.CombinedOutput(); err != nil {
		t.Fatalf("failed to run platform-agent-eval pod: %v\nOutput: %s", err, string(output))
	}

	// Setup deferred cleanup to delete the pod when tests finish
	defer func() {
		t.Log("Cleaning up platform-agent-eval pod...")
		deleteCmd := exec.Command("kubectl", "--context", "kind-kube-agents", "delete", "pod", pod.Name, "--ignore-not-found=true")
		_ = deleteCmd.Run()
	}()

	// 3. Wait for the pod to be ready
	t.Log("Waiting for pod to be ready...")
	waitCmd := exec.Command("kubectl", "--context", "kind-kube-agents", "wait", "--for=condition=Ready", "pod/"+pod.Name, "--namespace="+pod.Namespace, "--timeout=60s")
	if output, err := waitCmd.CombinedOutput(); err != nil {
		t.Fatalf("pod did not become ready: %v\nOutput: %s", err, string(output))
	}

	// 4. Run scenarios
	err := filepath.WalkDir(testdataDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if d.Name() == "events.yaml" {
			scenarioPath := filepath.Dir(path)
			scenarioName, err := filepath.Rel(testdataDir, scenarioPath)
			if err != nil {
				scenarioName = filepath.Base(scenarioPath)
			}

			t.Run(scenarioName, func(t *testing.T) {
				// 1. Clean up tmp directory inside the container and recreate it
				pod.ExecWithTimeout(t, 10*time.Second, "rm", "-rf", "/tmp/eval")
				pod.ExecWithTimeout(t, 10*time.Second, "mkdir", "-p", "/tmp/eval")

				// 2. Copy the template files to /tmp/eval inside the container
				pod.ExecWithTimeout(t, 10*time.Second, "cp", "-r", "/opt/platform-template/.", "/tmp/eval/")

				// 3. Read the template config.yaml and parse it
				templateConfigBytes := pod.ExecWithTimeout(t, 10*time.Second, "cat", "/opt/platform-template/config.yaml")
				var configMap map[string]interface{}
				if err := yaml.Unmarshal(templateConfigBytes, &configMap); err != nil {
					t.Fatalf("failed to parse template config.yaml: %v", err)
				}

				// Override model settings using environment variables
				provider := ""
				modelName := ""
				apiKey := ""
				if os.Getenv("GEMINI_API_KEY") != "" {
					provider = "gemini"
					modelName = "gemini-3.5-flash"
					apiKey = os.Getenv("GEMINI_API_KEY")
				} else if os.Getenv("OPENAI_API_KEY") != "" {
					provider = "openai"
					modelName = "gpt-4o-mini"
					apiKey = os.Getenv("OPENAI_API_KEY")
				} else {
					t.Fatalf("No LLM API keys found in environment. Please export GEMINI_API_KEY or OPENAI_API_KEY.")
				}

				secrets := []string{apiKey}

				sanitize := func(s string) string {
					return redactSecrets(s, secrets...)
				}

				configMap["model"] = map[string]interface{}{
					"provider": provider,
					"model":    modelName,
					"api_key":  apiKey,
				}

				newConfigBytes, err := yaml.Marshal(configMap)
				if err != nil {
					t.Fatalf("failed to marshal new config.yaml: %v", err)
				}

				localConfigPath := filepath.Join(t.TempDir(), "config.yaml")
				if err := os.WriteFile(localConfigPath, newConfigBytes, 0644); err != nil {
					t.Fatalf("failed to write local config.yaml: %v", err)
				}

				// Copy local config.yaml to pod
				pod.UploadFile(t, localConfigPath, "/tmp/eval/config.yaml")

				// 4. Copy events.yaml to pod
				pod.UploadFile(t, path, "/tmp/eval/events.yaml")

				// 5. Execute agent chat in pod
				prompt := "Read the Kubernetes events from /tmp/eval/events.yaml. Categorize them into Incidents using the incidents skill, and write the output list of incidents formatted in YAML directly to /tmp/eval/actual.yaml. Do not include extra text outside the YAML."

				// Debugging: Print env and config.yaml in pod
				envOutput := pod.ExecWithTimeout(t, 10*time.Second, "env")
				t.Logf("Pod environment:\n%s", sanitize(string(envOutput)))

				configBytes := pod.ReadFile(t, "/tmp/eval/config.yaml")
				t.Logf("Pod config.yaml content:\n%s", string(configBytes))

				t.Logf("Running agent for scenario %s...", scenarioName)
				output := pod.ExecWithTimeout(t, 5*time.Minute, "env", "HERMES_HOME=/tmp/eval",
					"/opt/hermes/.venv/bin/hermes", "chat", "-q", prompt, "--verbose", "--yolo")
				t.Logf("Agent output:\n%s", string(output))

				// Copy actual.yaml back from pod
				actualBytes := pod.ReadFile(t, "/tmp/eval/actual.yaml")
				var actual []Incident
				if err := yaml.Unmarshal(actualBytes, &actual); err != nil {
					t.Fatalf("failed to unmarshal actual.yaml: %v\nFile content:\n%s", err, string(actualBytes))
				}

				// Parse expectations
				expectedBytes, err := os.ReadFile(filepath.Join(scenarioPath, "_expectations.yaml"))
				if err != nil {
					t.Fatalf("failed to read _expectations.yaml: %v", err)
				}

				var expected []Incident
				if err := yaml.Unmarshal(expectedBytes, &expected); err != nil {
					t.Fatalf("failed to unmarshal _expectations.yaml: %v", err)
				}

				if len(actual) != len(expected) {
					t.Fatalf("incident count mismatch: got %d, expected %d\nActual: %+v\nExpected: %+v", len(actual), len(expected), actual, expected)
				}

				// Normalize actual results
				for i := range expected {
					if expected[i].ID == "" {
						actual[i].ID = ""
					}
					if expected[i].FirstTimestamp == "" {
						actual[i].FirstTimestamp = ""
					}
					if expected[i].LastTimestamp == "" {
						actual[i].LastTimestamp = ""
					}
				}

				if diff := cmp.Diff(expected, actual); diff != "" {
					t.Errorf("mismatch in scenario %s (-want +got):\n%s", scenarioName, diff)
				}
			})
		}
		return nil
	})
	if err != nil {
		t.Fatalf("WalkDir failed: %v", err)
	}
}

type Pod struct {
	Name      string
	Namespace string
}

func (p *Pod) ExecWithTimeout(t *testing.T, timeout time.Duration, argv ...string) []byte {
	ctx, cancel := context.WithTimeout(t.Context(), timeout)
	defer cancel()
	kubectlArgs := []string{
		"--context", "kind-kube-agents", "exec", "--namespace", p.Namespace, p.Name, "--",
	}
	kubectlArgs = append(kubectlArgs, argv...)
	command := exec.CommandContext(ctx, "kubectl", kubectlArgs...)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("failed to exec %v: %v\nOutput: %s", argv, err, string(output))
	}
	return output
}

func (p *Pod) ReadFile(t *testing.T, path string) []byte {
	timeout := 10 * time.Second
	ctx, cancel := context.WithTimeout(t.Context(), timeout)
	defer cancel()
	command := exec.CommandContext(ctx, "kubectl", "--context", "kind-kube-agents", "exec", "--namespace", p.Namespace, p.Name, "--", "cat", path)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("failed to read %s: %v\nOutput: %s", path, err, string(output))
	}
	return output
}

func (p *Pod) UploadFile(t *testing.T, localPath string, remotePath string) {
	timeout := 10 * time.Second
	ctx, cancel := context.WithTimeout(t.Context(), timeout)
	defer cancel()
	command := exec.CommandContext(ctx, "kubectl", "--context", "kind-kube-agents", "cp", "--namespace", p.Namespace, localPath, p.Name+":"+remotePath)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("failed to upload %s -> %s: %v\nOutput: %s", localPath, remotePath, err, string(output))
	}
}

// redactSecrets removes secret data from output.
func redactSecrets(s string, secrets ...string) string {
	for _, secret := range secrets {
		s = strings.ReplaceAll(s, secret, "[SECRET]")
	}
	return s
}
