#!/bin/sh
# setpriv drops the run user under no-new-privileges, so it cannot undo the rules.
set -eu

MODE=${OPENSCIENTIST_FIREWALL_MODE:-airgap}
RUN_AS=${OPENSCIENTIST_FIREWALL_RUN_AS:-agent}

# One entry per line, so the default IFS splits both this list and getent's addresses.
# Splitting on commas alone folded a host's several A records into one word, which nft
# rejected as a rule spanning several lines.
ALLOW=$(printf '%s' "${OPENSCIENTIST_FIREWALL_ALLOW:-}" | tr ',' '\n')

# Resolve hosts to IPv4 now (network still open) and pin the IPs.
resolve_ipv4() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1 }' | sort -u
}

accept_rules=""
host_records=""
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
        host_records="${host_records}${ip} ${host}
"
    done
done

if [ "$MODE" != "restrict" ] && [ -n "$host_records" ]; then
    printf '%s' "$host_records" | tee -a /etc/hosts > /dev/null
fi

# /proc/net/route stores the gateway little-endian, so the octets read reversed.
gateway_rule=""
if [ "$MODE" = "restrict" ]; then
    # The command substitution strips the newline, so a non-empty rule gets
    # one back or it fuses with the chain's closing brace.
    gateway_rule=$(awk '
        function hex2dec(h,  i, c, v) {
            v = 0
            for (i = 1; i <= length(h); i++) {
                c = substr(h, i, 1)
                v = v * 16 + index("0123456789abcdef", tolower(c)) - 1
            }
            return v
        }
        NR > 1 && $2 == "00000000" {
            h = $3
            print "        ip daddr " hex2dec(substr(h, 7, 2)) "." hex2dec(substr(h, 5, 2)) "." hex2dec(substr(h, 3, 2)) "." hex2dec(substr(h, 1, 2)) " drop"
            exit
        }' /proc/net/route)
    if [ -n "$gateway_rule" ]; then
        gateway_rule="${gateway_rule}
"
    fi
fi

# Outside a compose stack /etc/resolv.conf names the host's resolvers, which sit in a
# private range, so pin their port 53 above the range drops.
dns_rules=""
if [ "$MODE" = "restrict" ]; then
    for ns in $(awk '$1 == "nameserver" { print $2 }' /etc/resolv.conf | sort -u); do
        case "$ns" in
            *:*) family=ip6 ;;
            *) family=ip ;;
        esac
        dns_rules="${dns_rules}        ${family} daddr ${ns} udp dport 53 accept
        ${family} daddr ${ns} tcp dport 53 accept
"
    done
fi

if [ "$MODE" = "restrict" ]; then
    # The exceptions precede the range drops, so a web service on the bridge
    # stays open on its port although the bridge itself is a private range.
    nft -f - <<NFT
table inet airgap {
    chain output {
        type filter hook output priority 0; policy accept;
        oif "lo" accept
        ct state established,related accept
${accept_rules}${dns_rules}        ip daddr 169.254.0.0/16 drop
        ip daddr 10.0.0.0/8 drop
        ip daddr 172.16.0.0/12 drop
        ip daddr 192.168.0.0/16 drop
        ip daddr 100.64.0.0/10 drop
        ip6 daddr fe80::/10 drop
        ip6 daddr fc00::/7 drop
        ip6 daddr fd00:ec2::254 drop
${gateway_rule}    }
}
NFT
else
    # Docker forwards embedded DNS outside this namespace, including after DNAT.
    nft -f - <<NFT
table inet airgap {
    chain output {
        type filter hook output priority 0; policy drop;
        ip daddr 127.0.0.11 drop
        oif "lo" accept
        ct state established,related accept
${accept_rules}    }
}
NFT
fi

# The harness and its children must not leave world-readable files in the job
# tree, so set the umask here; setpriv drops it down to the run user.
umask 0007

# setpriv keeps Docker's HOME=/root (root entrypoint), unreadable by the run uid.
export HOME=/home/${RUN_AS}
if [ "$#" -eq 0 ]; then
    set -- python /agent-entrypoint.py
fi
exec setpriv --reuid=${RUN_AS} --regid=jobs --init-groups --inh-caps=-all --bounding-set=-all "$@"
