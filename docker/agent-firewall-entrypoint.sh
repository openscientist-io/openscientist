#!/bin/sh
# Air-gapped egress firewall: install a default-drop nftables allowlist as root,
# then drop to the agent user. no-new-privileges keeps the agent from undoing it.
set -eu

ALLOW="${OPENSCIENTIST_FIREWALL_ALLOW:-}"

# Resolve hosts to IPv4 now (network still open) and pin the IPs.
resolve_ipv4() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1 }' | sort -u
}

# DNS to this container's own resolvers only. Port 53 to any destination used to
# be accepted so the agent could re-resolve, but that bought no reachability --
# the allowlist below pins destinations by IP, so a name that re-resolves to some
# other address is dropped anyway -- while letting any process in here reach an
# arbitrary nameserver and carry data out in the query name.
#
# This narrows that channel rather than closing it, and the difference matters.
# Under Docker the resolver is the embedded one on 127.0.0.11, which answers
# compose service names from its own table but forwards everything else to the
# host's upstream servers -- and it forwards from outside this namespace, so
# those queries never traverse this chain and are still leaving. What is blocked
# from here on is the direct path: a query aimed straight at 8.8.8.8 or any other
# resolver of the caller's choosing. Closing the forwarded path as well takes an
# internal Docker network for the job container, which is a topology change.
dns_rules=""
# The `|| [ -n "$keyword" ]` keeps the last line when the file has no trailing
# newline: read fails there, and dropping it silently would leave a container
# with no resolver rule at all.
while read -r keyword address _rest || [ -n "$keyword" ]; do
    [ "$keyword" = "nameserver" ] || continue
    # Every allowlist rule here is IPv4, so an IPv6 resolver would need an
    # ip6 daddr match; skipping it just drops one entry from the resolver list.
    case "$address" in
        *:*) continue ;;
    esac
    dns_rules="${dns_rules}        ip daddr ${address} udp dport 53 accept
        ip daddr ${address} tcp dport 53 accept
"
done < /etc/resolv.conf

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
${dns_rules}${accept_rules}    }
}
NFT

# Drop root and every capability, then run the agent as the unprivileged user.
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all \
    python /agent-entrypoint.py
