from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from chia.plotting.create_plots import create_plots, resolve_plot_keys
from chia.plotting.util import Params


async def main() -> None:
    root = Path(os.environ["CHIA_ROOT"])
    keys = await resolve_plot_keys(None, None, None, None, root, logging.getLogger(__name__))
    # The CLI enforces k >= 25; the plotting API supports small test plots.
    params = Params(
        size=19,
        num=int(sys.argv[1]),
        buffer=256,
        num_threads=2,
        buckets=32,
        stripe_size=2000,
        tmp_dir=Path("/data/tmp"),
        tmp2_dir=None,
        final_dir=Path("/data/plots/k19"),
        plotid=None,
        memo=None,
        nobitfield=False,
    )
    await create_plots(params, keys)


if __name__ == "__main__":
    asyncio.run(main())
