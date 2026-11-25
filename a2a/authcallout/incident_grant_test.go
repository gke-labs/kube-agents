package authcallout

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestTheIncidentRouteIsTheGatewaysAndTheBridgesAlone: on a server running
// the shipped nats.conf and identity map, the gateway's incident write
// reaches the bridge and the bridge's answer reaches the gateway; every other
// principal the render carries is refused both the write and the answer, on
// publish and on subscribe; and neither end may play the other. A principal
// that could write the request could have the bridge attach a report to a
// thread, and one that could answer could tell the gateway it was kept. sys
// holds no subject list, so its own account is its boundary: what it
// publishes never reaches the bridge, and it never sees the gateway's write.
func TestTheIncidentRouteIsTheGatewaysAndTheBridgesAlone(t *testing.T) {
	h, others := connectEveryRenderedPrincipal(t)
	gw, gv := connectStatic(t, h, "gateway", renderedGatewayPassword)
	var bridge scheduledReportsPrincipal
	for _, p := range others {
		if p.name == "bridge" {
			bridge = p
		}
	}
	if bridge.nc == nil {
		t.Fatal("the render carries no bridge")
	}
	reply := lib.IncidentReplyPrefix + "conformance-1"
	request := lib.IncidentSubject

	// The route works end to end: the gateway asks, the bridge answers.
	asks, err := bridge.nc.SubscribeSync(request)
	if err != nil {
		t.Fatal(err)
	}
	answers, err := gw.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	if err := bridge.nc.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := gw.PublishRequest(request, reply, []byte(`{"platform":"slack"}`)); err != nil {
		t.Fatal(err)
	}
	ask, err := asks.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("the bridge did not receive the gateway's incident write: %v", err)
	}
	if err := ask.Respond([]byte(`{"ok":true}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := answers.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("the gateway did not receive the bridge's answer: %v", err)
	}

	// Neither end may play the other.
	checkPublish(t, gw, gv, map[string]bool{reply: true})
	if !subscribeRefused(t, gw, gv, request) {
		t.Error("the gateway may read incident writes")
	}
	checkPublish(t, bridge.nc, bridge.v, map[string]bool{request: true})
	if !subscribeRefused(t, bridge.nc, bridge.v, lib.IncidentReplyPrefix+">") {
		t.Error("the bridge may read incident answers")
	}

	// Nobody else holds either side.
	for _, p := range others {
		if p.name == "bridge" || p.name == "sys" {
			continue
		}
		t.Run(p.name, func(t *testing.T) {
			checkPublish(t, p.nc, p.v, map[string]bool{request: true, lib.IncidentReplyPrefix + p.name: true})
			if !subscribeRefused(t, p.nc, p.v, request) {
				t.Errorf("%s may read incident writes", p.name)
			}
			if !subscribeRefused(t, p.nc, p.v, lib.IncidentReplyPrefix+">") {
				t.Errorf("%s may read incident answers", p.name)
			}
		})
	}

	for _, p := range others {
		if p.name != "sys" {
			continue
		}
		t.Run(p.name, func(t *testing.T) {
			seen, err := p.nc.SubscribeSync(request)
			if err != nil {
				t.Fatal(err)
			}
			if err := p.nc.Flush(); err != nil {
				t.Fatal(err)
			}
			if err := p.nc.Publish(request, []byte(`{"platform":"slack"}`)); err != nil {
				t.Fatal(err)
			}
			if err := p.nc.Flush(); err != nil {
				t.Fatal(err)
			}
			if msg, err := asks.NextMsg(500 * time.Millisecond); err == nil {
				t.Errorf("sys's incident write reached the bridge: %q", msg.Data)
			}
			// Its own publish echoes on its own subscription; drained, so
			// only the gateway's write below could arrive.
			_, _ = seen.NextMsg(200 * time.Millisecond)
			if err := gw.Publish(request, []byte(`{"platform":"slack"}`)); err != nil {
				t.Fatal(err)
			}
			if err := gw.Flush(); err != nil {
				t.Fatal(err)
			}
			if msg, err := seen.NextMsg(500 * time.Millisecond); err == nil {
				t.Errorf("sys saw the gateway's incident write: %q", msg.Data)
			}
		})
	}
}
