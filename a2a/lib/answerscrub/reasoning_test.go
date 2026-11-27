package answerscrub

import "testing"

// The reasoning a model wrote as content in the #3773 turn, shortened: a bare
// `thought` line, the model's plan, and the intended ack run together.
const leakedReasoning = "thought\nNow that the task is created, I need to tell the user I am " +
	"checking the kubernetes version of a2a-next-dev.checking the kubernetes version of a2a-next-dev."

func TestOpensWithUnstructuredReasoning(t *testing.T) {
	cases := []struct {
		answer string
		want   bool
	}{
		{leakedReasoning, true},
		{"thought:\nThe user wants to delete the namespace.", true},
		{"thought:The user asked for the delete.", true},
		{"THOUGHT: the plan", true},
		{"thought", true},
		{"thought:", true},
		{"Thought\nthe plan", true},
		{"  THOUGHT  \r\nthe plan", true},
		{"\n\n  \nthought\nthe plan", true},
		{"The cluster runs 1.33.", false},
		{"I thought about it: the cluster runs 1.33.", false},
		{"Thought about it, the cluster runs 1.33.", false},
		{"My thought: the cluster runs 1.33.", false},
		{"thoughts\nthe plan", false},
		// TrimSpace keeps U+001C..U+001F; the Python side of the rule matches it.
		{"\x1cthought\nthe plan", false},
		{"thoughtful answer", false},
		{"thought :the plan", false},
		{"thought about it\nthe plan", false},
		{"The answer.\nthought\nmore", false},
		{"The answer.\nthought: more", false},
		{"thought\rthe plan", false},
		{"\n\nThe cluster runs 1.33.", false},
		{"", false},
		{"\n  \n", false},
	}
	for _, tc := range cases {
		if got := OpensWithUnstructuredReasoning(tc.answer); got != tc.want {
			t.Errorf("OpensWithUnstructuredReasoning(%q) = %v, want %v", tc.answer, got, tc.want)
		}
	}
}
