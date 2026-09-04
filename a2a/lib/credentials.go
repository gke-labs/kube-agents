package lib

import "github.com/nats-io/nats.go"

// WithUserPassword authenticates as one of the deployment's static per-role
// users and pins the inbox prefix that user is granted.
//
// Both halves are required, and the second one is the trap: push delivery and
// every JetStream API request come back on an inbox subject, and the
// deployment gives each user its own prefix (_INBOX.<user>.>) precisely so
// that no agent can subscribe to another's replies. nats.go's default inbox is
// _INBOX.<nuid>, which that grant does not cover - so a client that sets only
// the password authenticates fine, publishes fine, and then hangs on the first
// reply with an authorization violation the server logs and the client sees as
// a timeout. Every component dials through here so none of them has to
// rediscover that.
//
// Static per-component users are the playground posture: the product answer is
// the auth callout validating a KSA token per agent identity. What survives
// the switch is the deny-by-default subject lists, which are already exact.
func WithUserPassword(user, password string) ClientOption {
	return func(o *clientOptions) {
		if user == "" {
			return
		}
		o.natsOpts = append(o.natsOpts,
			nats.UserInfo(user, password),
			nats.CustomInboxPrefix("_INBOX."+user),
		)
	}
}
