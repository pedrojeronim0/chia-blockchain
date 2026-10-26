# Chia with Kollaps

This deployment targets a **single dedicated Linux machine running Docker Swarm**.
Each of the four Chia nodes has one container, one overlay-network IP, and its
own data volume. Each farmer creates and loads 32 k19 plots by default.
Each farmer also runs a wallet for its existing key. To generate timed
transaction workloads, follow the [transaction generator guide](../TRANSACTIONS.md).

The supplied topology emulates a switch with a bidirectional 100 Mbps link and
5 ms latency to each node. A node-to-node path has 10 ms one-way latency
(approximately 20 ms network RTT). Chia peers still form a full mesh; the
emulated network paths go through the switch. Local farmer/harvester/timelord
traffic stays on loopback inside each container.

## Build

Run the following commands on the dedicated machine, from this Chia repository's
root. Use x86_64 Linux with a kernel newer than 5.15, Docker Engine, Swarm support,
`git`, and `rg`. Kollaps requires kernel traffic control and eBPF support.
The Kollaps images are built from its official Dockerfiles, without local
Docker Desktop or WSL adaptations.

```bash
git clone --branch master --depth 1 --recurse-submodules https://github.com/miguelammatos/Kollaps.git ../Kollaps
bash docker/private-network/kollaps/build.sh ../Kollaps
```

The source inspected for this integration was revision
`bc744a6c7fe746d415128af09282eb83c9683454`. Record the actual revision printed by
`build.sh` and image digests when collecting experimental results.

## Prepare and deploy

On a dedicated Docker host, initialize Swarm if it is not already active:

```bash
docker swarm init
```

Do not run the standalone Compose network during measurements: it competes
for CPU with the experiment. Stop it while preserving its data:

```bash
docker compose -f docker/private-network/compose.yaml down
bash docker/private-network/kollaps/prepare.sh
docker stack deploy --resolve-image never -c docker/private-network/kollaps/stack.yaml chia-kollaps
docker stack services chia-kollaps
```

`prepare.sh` provisions a shared CA, creates separate `chia-kollaps-*` volumes,
and runs the official Kollaps deployment generator. It then adds Chia volumes
and plot counts while preserving Kollaps' UUID, labels, paused entrypoints,
and DNSRR endpoints. Kollaps mounts `topology.xml` from its absolute path on
the dedicated host, following the upstream deployment mechanism. Keep this
repository at that path while the experiment is deployed. Generate `stack.yaml`
on the dedicated machine; do not copy a stack generated on another host.

The standalone Compose volumes are not reused or deleted. To choose another
plot count, use `PLOT_COUNT=64 bash docker/private-network/kollaps/prepare.sh`.
The script refuses to prepare an already deployed experiment or a multi-node
Swarm. Run one Kollaps experiment at a time on this Docker host.

## Run and verify

Open **http://<dedicated-host-ip>:8088**, or use an SSH tunnel:

```bash
ssh -L 8088:localhost:8088 user@dedicated-host
```

With the tunnel, open **http://localhost:8088**. Wait until all four Chia containers show ready,
then click **Start**. Kollaps holds the application entrypoints until this
point. Creating plots on a fresh volume takes additional time after Start.
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

Each full node should have three FULL_NODE peers. Each farmer should also
have a local FARMER connection, and the timelord node a local TIMELORD
connection. Compare peak height and hash across all four nodes after allowing
blocks to propagate. Check plot loading on the farmer containers:

```bash
docker exec <farmer-container-id> chia rpc farmer get_harvesters
```

Container connectivity alone does not demonstrate that emulation is active.
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
