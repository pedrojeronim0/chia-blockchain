# Private Chia network with Docker

Private test network with **4 Chia containers and 4 full nodes**, intended to
run with **Kollaps on a single dedicated Linux machine using Docker Swarm**.

| Container | Services |
| --- | --- |
| `timelord` | full node, timelord, and VDF launcher |
| `farmer-1` | full node, farmer, harvester, and wallet |
| `farmer-2` | full node, farmer, harvester, and wallet |
| `farmer-3` | full node, farmer, harvester, and wallet |

Each farmer has an independent key and 32 k19 plots generated on first startup
(96 plots across the network). Only `/data/plots/k19` is configured for farming.
On restart, each farmer creates any plots missing from its target count.
`plot.py` uses Chia's plotting API because the CLI enforces a minimum of k25.
The full nodes connect to each other; each farmer and the timelord use their local full node.
Each node runs in its own container with a separate IP address. The Swarm
deployment uses an overlay network; the standalone Compose deployment uses a
bridge network. Docker assigns the addresses automatically; peer
configuration uses service names so it remains valid if addresses change.
The timelord runs three VDF processes for the CC/RC/ICC chains within a single
timelord service. The network uses real proofs without the simulator.

## Dedicated machine: Docker Swarm and Kollaps

Follow the [Kollaps deployment guide](kollaps/README.md) on the dedicated machine.
It covers building the images, initializing Swarm, generating the stack,
deploying the services, and starting the experiment from the dashboard.

Swarm runs the containers. Kollaps starts the Chia applications and applies
the network conditions defined in [topology.xml](kollaps/topology.xml).
The default topology provides 100 Mbps access links and 10 ms one-way latency
between nodes. Kollaps also runs a dashboard and bootstrapper alongside the
four Chia containers, plus a privileged emulation container.

The deployment files are:

| File | Purpose |
| --- | --- |
| `kollaps/topology.xml` | Nodes and emulated network conditions |
| `kollaps/build.sh` | Build Chia and the official Kollaps images |
| `kollaps/prepare.sh` | Prepare volumes, certificates, and generate the stack |
| `kollaps/stack.yaml` | Generated Swarm deployment; excluded from Git |

Generate `stack.yaml` on the dedicated machine because it contains an absolute
path to the topology. End-to-end operation must be verified on that machine.

## Optional local deployment: Docker Compose

The following commands run Chia directly, without Kollaps network emulation.
This deployment uses separate volumes from the Swarm experiment. Stop it
before collecting experimental measurements on the same machine.

### Start

You need Docker Engine/Desktop with Compose v2. On WSL, enable integration
for your distribution in Docker Desktop settings. From the repository root:

```bash
docker compose -f docker/private-network/compose.yaml up -d --build
docker compose -f docker/private-network/compose.yaml logs -f
```

The build compiles the VDF client and may take some time. Farmers create their
plots before starting services. Allocate several CPU cores for the timelord
and the three concurrent plotters. Block production is probabilistic;
there is no guaranteed block interval, especially at startup.

### Verify

```bash
docker compose -f docker/private-network/compose.yaml ps
docker network inspect chia-private_private --format '{{range .Containers}}{{println .Name .IPv4Address}}{{end}}'
docker compose -f docker/private-network/compose.yaml exec timelord chia show -s
docker compose -f docker/private-network/compose.yaml exec timelord chia peer full_node -c
docker compose -f docker/private-network/compose.yaml exec farmer-1 chia plots check -n 5
docker compose -f docker/private-network/compose.yaml exec farmer-1 chia rpc farmer get_harvesters
docker compose -f docker/private-network/compose.yaml exec timelord chia rpc full_node get_blockchain_state
docker compose -f docker/private-network/compose.yaml exec farmer-1 chia show -s
```

Check for three FULL_NODE connections and one TIMELORD connection on the
`timelord` node. On each farmer node, check the local FARMER connection and
FULL_NODE peers. Verify 32 k19 plots per farmer, increasing block height, and
convergence across all four nodes. Repeat the checks for `farmer-2` and
`farmer-3`. Before the first block, the peak is null.
Service logs are also available at `/data/chia/log/debug.log` inside the containers.

### Stop and restart

```bash
docker compose -f docker/private-network/compose.yaml down
docker compose -f docker/private-network/compose.yaml up -d
```

Volumes preserve the blockchain, keys, certificates, and plots.
To **delete all of this data** and start over:

```bash
docker compose -f docker/private-network/compose.yaml down -v
```

## Configuration

`configure.py` defines the `private-docker` network with a dedicated genesis
challenge, signature replay protection, reduced difficulty, and a test plot filter.
The Chia network is internal, with no published Chia ports, DNS seeders, or
introducer. The Kollaps deployment additionally publishes its dashboard on port 8088.
Service certificates use a shared private CA.
Coins and k19 plots are exclusively for this test network.

The default of 32 plots per farmer provides an equal initial space allocation
for comparing the three farmers. For Kollaps, set the count when preparing
the experiment:

```bash
PLOT_COUNT=64 bash docker/private-network/kollaps/prepare.sh
```

For standalone Compose:

```bash
PLOT_COUNT=64 docker compose -f docker/private-network/compose.yaml up -d
```

Lowering the target does not delete plots already created. Plot count controls
the amount of space allocated; it does not guarantee a particular block rate.

The build uses code from this checkout; pip resolves dependencies without
pinning them to the lockfile. Save the image ID/digest to repeat an experiment
with the same environment. Changes to consensus constants require recreating
the network and its volumes.

Each farmer also runs a wallet connected to its local full node, using its
existing key to receive and spend farming rewards. The transaction workload
is started explicitly; see [transaction generation](TRANSACTIONS.md).
For varying senders and recipients, `run_transactions.py` coordinates the three
farmer wallets with a global TPS target and a reproducible destination schedule.
