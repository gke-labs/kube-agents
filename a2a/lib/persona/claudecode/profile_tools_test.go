package claudecode

import (
	"os"
	"regexp"
	"slices"
	"testing"
)

// profileServerTools reads mcp_policy.PROFILE_SERVER_TOOLS, the servers and
// tools the credential broker's relay lets a profile worker call.
func profileServerTools(t *testing.T) map[string][]string {
	t.Helper()
	src, err := os.ReadFile("../../../../agents/platform/scripts/mcp_policy.py")
	if err != nil {
		t.Fatal(err)
	}
	block := regexp.MustCompile(`(?s)\nPROFILE_SERVER_TOOLS: [^=]*= \{(.*?)\n\}`).FindSubmatch(src)
	if block == nil {
		t.Fatal("mcp_policy.py has no PROFILE_SERVER_TOOLS table")
	}
	tables := map[string][]string{}
	for _, m := range regexp.MustCompile(`"([a-z_]+)": frozenset\(\{([^}]*)\}\)`).FindAllSubmatch(block[1], -1) {
		var tools []string
		for _, q := range regexp.MustCompile(`"([a-z_0-9]+)"`).FindAllSubmatch(m[2], -1) {
			tools = append(tools, string(q[1]))
		}
		slices.Sort(tools)
		tables[string(m[1])] = tools
	}
	if len(tables) == 0 {
		t.Fatal("read no servers from PROFILE_SERVER_TOOLS")
	}
	return tables
}
