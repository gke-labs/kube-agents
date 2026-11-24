package workeradapter

import (
	"errors"
	"strings"
)

// procState is the state field of a /proc/<pid>/stat line, which follows
// the parenthesised command name; the name itself may hold spaces and
// parentheses, so the last ')' ends it.
func procState(stat string) (byte, error) {
	i := strings.LastIndexByte(stat, ')')
	if i < 0 || i+2 >= len(stat) {
		return 0, errors.New("no state field")
	}
	return stat[i+2], nil
}
