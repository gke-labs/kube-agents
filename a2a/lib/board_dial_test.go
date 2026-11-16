package lib

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// TestBoardDialFixtureLandsInEveryField reads the canonical dial snapshot
// (testdata/board_dial.json, the shape session-kv's GET /v1/board sends) and
// checks every field reaches lib.Board, so a renamed json tag on either side
// fails here rather than as an empty panel.
func TestBoardDialFixtureLandsInEveryField(t *testing.T) {
	raw, err := os.ReadFile("testdata/board_dial.json")
	if err != nil {
		t.Fatal(err)
	}
	var b Board
	if err := json.Unmarshal(raw, &b); err != nil {
		t.Fatal(err)
	}
	want := &BoardDial{
		Enabled: true, Level: DialLevelPropose, SetBy: "U0123", SetAt: 1763300000, Admins: []string{"U0123"},
		Kinds: []BoardDialKind{{Kind: "no-pdb", Title: "Disruption budgets", Environment: "dev", Repos: []BoardDialRepo{{
			Repo: "org/infra", RepoKey: "github.com/org/infra", Clean: 3, Needed: 5, Days: 1, DaysNeeded: 2, Ready: false,
			State: DialStatePending, PendingPR: &BoardDialPR{Number: 35, URL: "https://github.com/org/infra/pull/35"},
		}}}},
	}
	if !reflect.DeepEqual(b.Dial, want) {
		t.Errorf("dial = %+v, want %+v", b.Dial, want)
	}
	// And back: every key the fixture holds is written again under the same name.
	var fixture, again map[string]any
	_ = json.Unmarshal(raw, &fixture)
	out, err := json.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(out, &again)
	if !reflect.DeepEqual(fixture["dial"], again["dial"]) {
		t.Errorf("the dial does not round-trip:\nfixture %v\nwritten %v", fixture["dial"], again["dial"])
	}
}

// TestBoardDialActionCarriesItsRow pins the dial fields of a board action as
// session-kv's POST /v1/board/actions reads them, and that a dial action
// sends no tasks.
func TestBoardDialActionCarriesItsRow(t *testing.T) {
	out, err := json.Marshal(BoardAction{Action: DialActionRequest, User: "U0123", By: "Jayanti",
		Kind: "no-pdb", Environment: "dev", RepoKey: "github.com/org/infra"})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"action":"dial.request","by":"Jayanti","user":"U0123","kind":"no-pdb","environment":"dev","repo_key":"github.com/org/infra"}`
	if string(out) != want {
		t.Errorf("action = %s, want %s", out, want)
	}
	var res BoardActionResult
	if err := json.Unmarshal([]byte(`{"action":"dial.request","ok":false,"message":"Only an install admin can change the dial."}`), &res); err != nil {
		t.Fatal(err)
	}
	if res.OK || res.Message != "Only an install admin can change the dial." {
		t.Errorf("result = %+v", res)
	}
}
