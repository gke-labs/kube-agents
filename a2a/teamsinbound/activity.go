package teamsinbound

import (
	"encoding/json"
	"errors"
	"strings"
)

// activity is the part of a Bot Framework activity the receiver reads. The
// broker validates the whole shape again on arrival (validate_teams_activity);
// this reads only what the token check and the tenant pin need.
type activity struct {
	ChannelID    string `json:"channelId"`
	ServiceURL   string `json:"serviceUrl"`
	Conversation struct {
		TenantID string `json:"tenantId"`
	} `json:"conversation"`
	ChannelData struct {
		Tenant struct {
			ID string `json:"id"`
		} `json:"tenant"`
	} `json:"channelData"`
}

// errNotAnObject is decodeActivity's answer to a body that parses as JSON
// but is not an object: null, which Unmarshal into a struct leaves untouched
// without an error, or a scalar.
var errNotAnObject = errors.New("the activity is not a JSON object")

// decodeActivity reads the fields above from the request body. A body that
// is not a JSON object is an error; absent fields are empty strings, which
// the checks that need them refuse.
func decodeActivity(body []byte) (activity, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return activity{}, err
	}
	if raw == nil {
		return activity{}, errNotAnObject
	}
	var a activity
	if err := json.Unmarshal(body, &a); err != nil {
		return activity{}, err
	}
	return a, nil
}

// tenantID is the tenant an activity names, as the broker reads it:
// channelData.tenant.id first, conversation.tenantId otherwise.
func (a activity) tenantID() string {
	if id := strings.TrimSpace(a.ChannelData.Tenant.ID); id != "" {
		return id
	}
	return strings.TrimSpace(a.Conversation.TenantID)
}
