#!/bin/sh
# Air-gapped egress firewall: install a default-drop nftables allowlist as root,
# then drop to the agent user. no-new-privileges keeps the agent from undoing it.
set -eu

ALLOW="${OPENSCIENTIST_FIREWALL_ALLOW:-}"

# Resolve hosts to IPv4 now (network still open) and pin the IPs. DNS stays open
# so the agent can re-resolve.
resolve_ipv4() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1 }' | sort -u
}

accept_rules=""
old_ifs="$IFS"
IFS=','
for entry in $ALLOW; do
    [ -n "$entry" ] || continue
    host="${entry%:*}"
    port="${entry##*:}"
    if [ -z "$host" ] || [ -z "$port" ]; then
        continue
    fi
    for ip in $(resolve_ipv4 "$host"); do
        accept_rules="${accept_rules}        ip daddr ${ip} tcp dport ${port} accept
"
    done
done
IFS="$old_ifs"

nft -f - <<NFT
table inet airgap {
    chain output {
        type filter hook output priority 0; policy drop;
        oif "lo" accept
        ct state established,related accept
        udp dport 53 accept
        tcp dport 53 accept
${accept_rules}    }
}
NFT

# Drop root and every capability, then run the agent as the unprivileged user.
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all \
    python /agent-entrypoint.py
