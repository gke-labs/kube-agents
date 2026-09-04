# Login shells are used by the agent terminal executor. Keep credential-aware
# CLI names ahead of the native binaries that exist only for the paired proxy.
#
# Gated on CREDENTIAL_PROXY_URL rather than applied unconditionally: the
# shims are useless without it anyway (credential_proxy_client.py refuses to
# run without the endpoint), and selfimprove_run.py's run_agent() strips both
# the shim directory from PATH *and* CREDENTIAL_PROXY_URL from a non-forge
# turn's subprocess environment specifically so that turn cannot reach the
# forging credential. A login shell opened inside that turn (e.g. the
# terminal tool's `bash -l`) re-sources this file and, unconditionally, undid
# the PATH half of that removal -- leaving only the env-var removal as the
# actual boundary. Checking the same variable this file exists to gate access
# to keeps the two removals in agreement instead of one silently re-arming.
if [ -n "${CREDENTIAL_PROXY_URL:-}" ]; then
    export PATH="/opt/credential-proxy/bin:${PATH}"
fi
