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
deployment uses an overlay network. Docker assigns the addresses automatically; peer
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
| `kollaps/prepare.sh` | Prepare network and volumes, and generate the stack |
| `kollaps/stack.yaml` | Generated Swarm deployment; excluded from Git |

Generate `stack.yaml` on the dedicated machine because it contains an absolute
path to the topology. End-to-end operation must be verified on that machine.

## Application startup

All four Chia nodes use the `chia-private:local` image and the single
`entrypoint.sh` in this directory. Kollaps
launches it with the role and node name from `kollaps/topology.xml` after
**Start** is selected in the dashboard. It requires `KOLLAPS_UUID`.
On first startup, each node runs `chia init` to generate its own private CA
and service certificates in its persistent data volume.

For each application startup, the entrypoint runs `configure.py`, creates
missing plots on farmers, and starts the Chia daemon and services. The four
nodes each run their own copy against their own persistent volume.

The build compiles the VDF client and may take some time. Farmers create their
plots before starting services. Allocate several CPU cores for the timelord
and the three concurrent plotters. Block production is probabilistic;
there is no guaranteed block interval, especially at startup.

See the Kollaps guide for verification and shutdown commands.
Volumes preserve the blockchain, keys, certificates, and plots between deployments.

## Configuration

`configure.py` defines the `private-docker` network with a dedicated genesis
challenge, signature replay protection, reduced difficulty, and a test plot filter.
The Chia network is internal, with no published Chia ports, DNS seeders, or
introducer. The Kollaps deployment additionally publishes its dashboard on port 8088.
Each node's local services use its own private CA. Full-node peer connections
use Chia's bundled public CA and do not require sharing private CAs.
Coins and k19 plots are exclusively for this test network.

The network uses `DIFFICULTY_CONSTANT_FACTOR = 2**34`.
With the default 96 k19 plots, difficulty 1024, sub-slot iterations 1048576,
and a 1-bit filter, this gives approximately 0.46 eligible proofs per signage
point (29 per sub-slot). This estimates eligibility, not achieved block rate;
changing the plot count changes the estimate.
The earlier configuration used `2**25`, making all found qualities eligible
after the filter. Reset the test network data before deploying this changed
consensus constant. Rebuilding the image does not reset existing volumes.

The default of 32 plots per farmer provides an equal initial space allocation
for comparing the three farmers. For Kollaps, set the count when preparing
the experiment:

```bash
PLOT_COUNT=64 bash docker/private-network/kollaps/prepare.sh
```

Lowering the target does not delete plots already created. Plot count controls
the amount of space allocated; it does not guarantee a particular block rate.

The build uses code from this checkout; pip resolves dependencies without
pinning them to the lockfile. Save the image ID/digest to repeat an experiment
with the same environment. These test consensus settings and k19 plots do not
establish equivalence to mainnet or another proof scheme. Record them when
comparing experiments, together with the observed block cadence.
Changes to consensus constants require recreating
the network and its volumes.

Each farmer also runs a wallet connected to its local full node, using its
existing key to receive and spend farming rewards. The transaction workload
is started explicitly; see [transaction generation](TRANSACTIONS.md).
For varying senders and recipients, `run_transactions.py` coordinates the three
farmer wallets with a global TPS target and a reproducible destination schedule.
