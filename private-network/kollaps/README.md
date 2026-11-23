# Chia with Kollaps

This deployment targets a **single dedicated Linux machine running Docker Swarm**.
Each of the Chia nodes has one container, one overlay-network IP, and its
own data volume. Each farmer creates and loads 32 k19 plots by default.
Each farmer also runs a wallet for its existing key. To generate timed
transaction workloads, follow the [transaction generator guide](../TRANSACTIONS.md).

The supplied topology emulates a switch with a bidirectional 1 Mbps link and
5 ms latency to each node. A node-to-node path has 10 ms one-way latency
(approximately 20 ms network RTT). Chia peers still form a full mesh; the
emulated network paths go through the switch. Local farmer/harvester/timelord
traffic stays on loopback inside each container.

## Build

Run the following commands on the dedicated machine, from this Chia repository's
root. Use x86_64 Linux with a kernel newer than 5.15, Docker Engine, Swarm support,
`git`, and `grep`. Kollaps requires kernel traffic control and eBPF support.
The Kollaps images are built from its official Dockerfiles, without local
Docker Desktop or WSL adaptations.

```bash
git clone --branch master --depth 1 --recurse-submodules https://github.com/miguelammatos/Kollaps.git ../Kollaps
bash private-network/kollaps/build.sh ../Kollaps
```

The source inspected for this integration was revision
`bc744a6c7fe746d415128af09282eb83c9683454`. Record the actual revision printed by
`build.sh` and image digests when collecting experimental results.

## Prepare and deploy

On a dedicated Docker host, initialize Swarm if it is not already active:

```bash
docker swarm init
```

Stop other experimental workloads on the host before measurements to avoid
resource contention. Prepare and deploy the stack:

```bash
bash private-network/kollaps/prepare.sh
docker stack deploy --resolve-image never -c private-network/kollaps/stack.yaml chia-kollaps
docker stack services chia-kollaps
```

`prepare.sh` prepares the Docker network, creates separate `chia-kollaps-*` data volumes,
and runs the official Kollaps deployment generator. It then adds Chia volumes
and plot counts while preserving Kollaps' UUID, labels, paused entrypoints,
and DNSRR endpoints. Kollaps mounts `topology.xml` from its absolute path on
the dedicated host, following the upstream deployment mechanism. Keep this
repository at that path while the experiment is deployed. Generate `stack.yaml`
on the dedicated machine; do not copy a stack generated on another host.

`topology.xml` is the source of truth for nodes and network conditions. The
preparation script reads it without rewriting it. Each non-supervisor service
must declare a unique node name and a command with its role and matching name:

```xml
<service name="farmer-5" image="chia-private:local" command="['farmer', 'farmer-5']"/>
<service name="full-node-3" image="chia-private:local" command="['full_node', 'full-node-3']"/>
```

Add a link for each new service under `<links>`, choosing its own conditions:

```xml
<link origin="full-node-3" dest="switch" latency="5" upload="1Mbps" download="1Mbps" network="kollaps_network"/>
```

Supported Chia roles are `farmer`, `timelord`, and `full_node`. Declare one
service per node (`replicas` must be 1), so each has its own identity and volume.
Node names need not be sequential. The script derives volumes and distributes
the complete node/role map for Chia peer configuration and transaction discovery.
Standalone full nodes run only `chia start node`, without farming or wallets.
`FARMER_COUNT` and `FULL_NODE_COUNT` are no longer used; add/remove services and
links in the XML instead. There is no intermediate generated topology.

Plot quantity remains separate: `PLOT_COUNT=64 bash private-network/kollaps/prepare.sh`
sets 64 k19 plots per farmer (default 32); plot size remains fixed at k19.
Rebuild the Chia image and regenerate/redeploy the stack to apply this change.
Removing nodes retains their existing volumes. A coordinated transaction run
requires at least two farmers. Full-mesh peer connections grow quadratically;
more plots also change proof eligibility with the fixed consensus settings.

The script refuses to prepare an already deployed experiment or a multi-node
Swarm. Run one Kollaps experiment at a time on this Docker host.

## Run and verify

Open **http://<dedicated-host-ip>:8088**, or use an SSH tunnel:

```bash
ssh -L 8088:localhost:8088 user@dedicated-host
```

With the tunnel, open **http://localhost:8088**. Wait until all Chia containers show ready,
then click **Start**. Kollaps holds the application entrypoints until this
point. All Chia nodes use `chia-private:local`, whose entrypoint is
the shared `../entrypoint.sh`; it receives
the role and node name, checks the experiment UUID, initializes each node's
own private CA and certificates on first startup, configures Chia, and starts
the services. Creating plots on a fresh volume takes
additional time after Start.
Existing data volumes retain their current certificates, including any CA
imported by an earlier deployment; this change does not rotate certificates.
Fresh data volumes generate independent private CAs. The old shared CA volume
is no longer mounted or used and is not deleted automatically.
For measured runs, prepare plots in a warm-up run, then remove and redeploy
the stack with its volumes preserved.

```bash
docker ps --filter label=com.docker.stack.namespace=chia-kollaps
docker ps --filter name=god_
```

For each running Chia container ID from the first command:

```bash
docker exec <container-id> chia peer full_node -c
docker exec <container-id> chia rpc full_node get_blockchain_state
```

With N farmers and M standalone full nodes, each full node should have
N + M FULL_NODE peers. Each farmer should also
have a local FARMER connection, and the timelord node a local TIMELORD
connection. Compare peak height and hash across all nodes after allowing
blocks to propagate. Check plot loading on the farmer containers:

```bash
docker exec <farmer-container-id> chia rpc farmer get_harvesters
```

Container connectivity alone does not demonstrate that emulation is active.
The upstream emulation core reports a path as active only when its sampled
throughput exceeds 1% of that path's maximum bandwidth (`process_usage` in
`kollaps/emulationcore/src/graph.rs`). For the supplied 1 Mbps paths, that is
more than 10 Kbps per source/destination path. Chia's intermittent protocol
traffic can remain below this threshold even while blocks propagate; an empty
Active flows table alone does not establish that traffic capture has failed.
The iperf3 example generates sustained traffic and is more likely to exceed
this threshold. Do not lower experimental bandwidth just to populate the UI:
that changes the experiment. Verify counters and observed latency separately.
Check the `god_*` container logs for emulation-core failures and inspect the
traffic-control rules in each node's network namespace before recording
results. Validate observed latency/bandwidth against the XML topology.
Application stdout launched through Kollaps' exec mechanism may not appear
in `docker service logs`; Chia logs remain at `/data/chia/log/debug.log`.

## Change conditions and stop

Edit `topology.xml` to change link latency, upload/download bandwidth, `jitter`
or `drop`. For example, setting each link's latency to 50 gives 100 ms one-way
latency between nodes. Changes require stopping and regenerating the stack;
editing XML does not change an already running experiment.

Use **Stop** in the dashboard for a clean application shutdown, then:

```bash
docker stack rm chia-kollaps
```

Wait for its services and the matching `god_*` container to exit before
preparing another experiment. If a `god_*` container remains, identify the one
belonging to this experiment by its `god<KOLLAPS_UUID>` label and stop that
specific container before redeploying. Volumes are retained. For independent trials,
restore the same blockchain snapshot or deliberately reset the experiment
volumes; simply restarting continues the previous chain.

Kollaps' bootstrapper accesses the Docker socket and creates a privileged
container sharing the host PID/network namespaces to shape traffic. This is
part of Kollaps' architecture. Use an isolated experimental Docker host.

The topology and scripts have been checked locally. End-to-end emulation and
Chia connectivity must still be verified on the dedicated machine.

References: [Docker installation](https://kollaps.dev/docker.html),
[running experiments](https://kollaps.dev/howtorun.html),
[topology reference](https://kollaps.dev/topologydescription.html).
