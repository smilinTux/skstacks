# OpenBao server config (Swarm / standalone Raft node). One per node; node_id is
# the stable task slot (BAO_RAFT_NODE_ID), addrs use the routable per-task
# hostname (BAO_CLUSTER_ADDR_HOST). Init/unseal via ../bootstrap.sh (PGP, no-catch-22).
ui = true

listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_disable   = 0
  tls_cert_file = "/openbao/tls/tls.crt"
  tls_key_file  = "/openbao/tls/tls.key"
}

storage "raft" {
  path    = "/openbao/data"
  node_id = "{{ env "BAO_RAFT_NODE_ID" }}"
  # Peers auto-join the leader over the overlay network DNS name `openbao`.
  retry_join { leader_api_addr = "https://openbao:8200" }
}

# Advertise the per-task routable hostname (BAO_CLUSTER_ADDR_HOST={{.Task.Name}}),
# NOT the bare node_id — peers must resolve & dial these to form Raft quorum.
api_addr     = "https://{{ env "BAO_CLUSTER_ADDR_HOST" }}:8200"
cluster_addr = "https://{{ env "BAO_CLUSTER_ADDR_HOST" }}:8201"
disable_mlock = false
