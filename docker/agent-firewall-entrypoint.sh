#!/bin/sh
# Air-gapped egress firewall: install a default-drop nftables allowlist as root,
# then drop to the agent user. no-new-privileges keeps the agent from undoing it.
set -eu

# One entry per line, so the default IFS splits both this list and getent's addresses.
# Splitting on commas alone folded a host's several A records into one word, which nft
# rejected as a rule spanning several lines.
ALLOW=$(printf '%s' "${OPENSCIENTIST_FIREWALL_ALLOW:-}" | tr ',' '\n')

# Resolve hosts to IPv4 now (network still open) and pin the IPs.
resolve_ipv4() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1 }' | sort -u
}

accept_rules=""
for entry in $ALLOW; do
    host="${entry%:*}"
    port="${entry##*:}"
    # An entry with no colon lands here as port=host, so rejecting a non-numeric
    # port covers that too. Anything malformed would render as invalid nft syntax.
    case "$port" in
        '' | *[!0-9]*) continue ;;
    esac
    [ -n "$host" ] || continue
    for ip in $(resolve_ipv4 "$host"); do
        accept_rules="${accept_rules}        ip daddr ${ip} tcp dport ${port} accept
"
    done
done

# No port 53 rule: the resolver on 127.0.0.11 is reached over lo, and the allowlist
# pins destinations by IP, so an open 53 buys no reachability while letting a job pick
# its own nameserver and exfiltrate in the query name. Names that resolver forwards
# upstream still leave, from outside this namespace where no rule here sees them.
nft -f - <<NFT
table inet airgap {
    chain output {
        type filter hook output priority 0; policy drop;
        oif "lo" accept
        ct state established,related accept
${accept_rules}    }
}
NFT

# Drop root and every capability, then run the agent as the unprivileged user.
# setpriv changes uid/gid but not HOME; Docker set HOME=/root at container
# start (derived from the --user root this script needs for nftables), which
# the agent uid can't read. Reset it so HOME-relative lookups (e.g. asyncpg's
# default SSL client-key probe) don't hit a permission error instead of a
# clean "not found".
export HOME=/home/agent
if [ "$#" -eq 0 ]; then
    set -- python /agent-entrypoint.py
fi
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all "$@"
