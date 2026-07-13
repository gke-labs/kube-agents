# Login shells are used by the agent terminal executor. Keep credential-aware
# CLI names ahead of the native binaries that exist only for the paired proxy.
export PATH="/opt/credential-proxy/bin:${PATH}"
