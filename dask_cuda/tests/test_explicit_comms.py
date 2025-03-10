import asyncio
import multiprocessing as mp
import sys

import numpy as np
import pandas as pd
import pytest

import dask
from dask import dataframe as dd
from dask.dataframe.shuffle import partitioning_index
from dask.dataframe.utils import assert_eq
from distributed import Client, get_worker
from distributed.deploy.local import LocalCluster

import dask_cuda
from dask_cuda.explicit_comms import comms
from dask_cuda.explicit_comms.dataframe.shuffle import shuffle as explicit_comms_shuffle
from dask_cuda.initialize import initialize
from dask_cuda.utils import get_ucx_config

pytestmark = pytest.mark.skipif(
    sys.version_info.minor < 80,
    reason="Temporarily skipping some tests because of a bug "
    "in Dask see <https://github.com/rapidsai/dask-cuda/issues/746>",
)


mp = mp.get_context("spawn")  # type: ignore
ucp = pytest.importorskip("ucp")

# Notice, all of the following tests is executed in a new process such
# that UCX options of the different tests doesn't conflict.


async def my_rank(state, arg):
    return state["rank"] + arg


def _test_local_cluster(protocol):
    dask.config.update(
        dask.config.global_config,
        {"distributed.comm.ucx": get_ucx_config(enable_tcp_over_ucx=True),},
        priority="new",
    )

    with LocalCluster(
        protocol=protocol,
        dashboard_address=None,
        n_workers=4,
        threads_per_worker=1,
        processes=True,
    ) as cluster:
        with Client(cluster) as client:
            c = comms.CommsContext(client)
            assert sum(c.run(my_rank, 0)) == sum(range(4))


@pytest.mark.parametrize("protocol", ["tcp", "ucx"])
def test_local_cluster(protocol):
    p = mp.Process(target=_test_local_cluster, args=(protocol,))
    p.start()
    p.join()
    assert not p.exitcode


def _test_dataframe_merge_empty_partitions(nrows, npartitions):
    with LocalCluster(
        protocol="tcp",
        dashboard_address=None,
        n_workers=npartitions,
        threads_per_worker=1,
        processes=True,
    ) as cluster:
        with Client(cluster):
            df1 = pd.DataFrame({"key": np.arange(nrows), "payload1": np.arange(nrows)})
            key = np.arange(nrows)
            np.random.shuffle(key)
            df2 = pd.DataFrame({"key": key, "payload2": np.arange(nrows)})
            expected = df1.merge(df2).set_index("key")
            ddf1 = dd.from_pandas(df1, npartitions=npartitions)
            ddf2 = dd.from_pandas(df2, npartitions=npartitions)
            with dask.config.set(explicit_comms=True):
                ddf3 = ddf1.merge(ddf2, on=["key"]).set_index("key")
                got = ddf3.compute()
                pd.testing.assert_frame_equal(got, expected)


def test_dataframe_merge_empty_partitions():
    # Notice, we use more partitions than rows
    p = mp.Process(target=_test_dataframe_merge_empty_partitions, args=(2, 4))
    p.start()
    p.join()
    assert not p.exitcode


def check_partitions(df, npartitions):
    """Check that all values in `df` hashes to the same"""
    hashes = partitioning_index(df, npartitions)
    if len(hashes) > 0:
        return len(hashes.unique()) == 1
    else:
        return True


def _test_dataframe_shuffle(backend, protocol, n_workers):
    if backend == "cudf":
        cudf = pytest.importorskip("cudf")
        initialize(enable_tcp_over_ucx=True)
    else:
        dask.config.update(
            dask.config.global_config,
            {"distributed.comm.ucx": get_ucx_config(enable_tcp_over_ucx=True),},
            priority="new",
        )

    with LocalCluster(
        protocol=protocol,
        dashboard_address=None,
        n_workers=n_workers,
        threads_per_worker=1,
        processes=True,
    ) as cluster:
        with Client(cluster) as client:
            all_workers = list(client.get_worker_logs().keys())
            comms.default_comms()
            np.random.seed(42)
            df = pd.DataFrame({"key": np.random.random(100)})
            if backend == "cudf":
                df = cudf.DataFrame.from_pandas(df)

            for input_nparts in range(1, 5):
                for output_nparts in range(1, 5):
                    ddf = dd.from_pandas(df.copy(), npartitions=input_nparts).persist(
                        workers=all_workers
                    )
                    ddf = explicit_comms_shuffle(
                        ddf, ["key"], npartitions=output_nparts
                    ).persist()

                    assert ddf.npartitions == output_nparts

                    # Check that each partition of `ddf` hashes to the same value
                    result = ddf.map_partitions(
                        check_partitions, output_nparts
                    ).compute()
                    assert all(result.to_list())

                    # Check the values of `ddf` (ignoring the row order)
                    expected = df.sort_values("key")
                    got = ddf.compute().sort_values("key")
                    assert_eq(got, expected)


@pytest.mark.parametrize("nworkers", [1, 2, 3])
@pytest.mark.parametrize("backend", ["pandas", "cudf"])
@pytest.mark.parametrize("protocol", ["tcp", "ucx"])
def test_dataframe_shuffle(backend, protocol, nworkers):
    if backend == "cudf":
        pytest.importorskip("cudf")

    p = mp.Process(target=_test_dataframe_shuffle, args=(backend, protocol, nworkers))
    p.start()
    p.join()
    assert not p.exitcode


def _test_dask_use_explicit_comms():
    def check_shuffle(in_cluster):
        """Check if shuffle use explicit-comms by search for keys named
        'explicit-comms-shuffle'
        """
        name = "explicit-comms-shuffle"
        ddf = dd.from_pandas(pd.DataFrame({"key": np.arange(10)}), npartitions=2)
        with dask.config.set(explicit_comms=False):
            res = ddf.shuffle(on="key", npartitions=4, shuffle="tasks")
            assert all(name not in str(key) for key in res.dask)
        with dask.config.set(explicit_comms=True):
            res = ddf.shuffle(on="key", npartitions=4, shuffle="tasks")
            if in_cluster:
                assert any(name in str(key) for key in res.dask)
            else:  # If not in cluster, we cannot use explicit comms
                assert all(name not in str(key) for key in res.dask)

    with LocalCluster(
        protocol="tcp",
        dashboard_address=None,
        n_workers=2,
        threads_per_worker=1,
        processes=True,
    ) as cluster:
        with Client(cluster):
            check_shuffle(True)
    check_shuffle(False)


def test_dask_use_explicit_comms():
    p = mp.Process(target=_test_dask_use_explicit_comms)
    p.start()
    p.join()
    assert not p.exitcode


def _test_dataframe_shuffle_merge(backend, protocol, n_workers):
    if backend == "cudf":
        cudf = pytest.importorskip("cudf")

        initialize(enable_tcp_over_ucx=True)
    else:

        dask.config.update(
            dask.config.global_config,
            {"distributed.comm.ucx": get_ucx_config(enable_tcp_over_ucx=True),},
            priority="new",
        )

    with LocalCluster(
        protocol=protocol,
        dashboard_address=None,
        n_workers=n_workers,
        threads_per_worker=1,
        processes=True,
    ) as cluster:
        with Client(cluster):
            nrows = n_workers * 10

            # Let's make some dataframes that we can join on the "key" column
            df1 = pd.DataFrame({"key": np.arange(nrows), "payload1": np.arange(nrows)})
            key = np.arange(nrows)
            np.random.shuffle(key)
            df2 = pd.DataFrame(
                {"key": key[nrows // 3 :], "payload2": np.arange(nrows)[nrows // 3 :]}
            )
            expected = df1.merge(df2, on="key").set_index("key")

            if backend == "cudf":
                df1 = cudf.DataFrame.from_pandas(df1)
                df2 = cudf.DataFrame.from_pandas(df2)

            ddf1 = dd.from_pandas(df1, npartitions=n_workers + 1)
            ddf2 = dd.from_pandas(
                df2, npartitions=n_workers - 1 if n_workers > 1 else 1
            )
            with dask.config.set(explicit_comms=True):
                got = ddf1.merge(ddf2, on="key").set_index("key").compute()
            assert_eq(got, expected)


@pytest.mark.parametrize("nworkers", [1, 2, 4])
@pytest.mark.parametrize("backend", ["pandas", "cudf"])
@pytest.mark.parametrize("protocol", ["tcp", "ucx"])
def test_dataframe_shuffle_merge(backend, protocol, nworkers):
    if backend == "cudf":
        pytest.importorskip("cudf")
    p = mp.Process(
        target=_test_dataframe_shuffle_merge, args=(backend, protocol, nworkers)
    )
    p.start()
    p.join()
    assert not p.exitcode


def _test_jit_unspill(protocol):
    import cudf

    with dask_cuda.LocalCUDACluster(
        protocol=protocol,
        dashboard_address=None,
        n_workers=1,
        threads_per_worker=1,
        jit_unspill=True,
        device_memory_limit="1B",
        enable_tcp_over_ucx=True if protocol == "ucx" else False,
    ) as cluster:
        with Client(cluster):
            np.random.seed(42)
            df = cudf.DataFrame.from_pandas(
                pd.DataFrame({"key": np.random.random(100)})
            )
            ddf = dd.from_pandas(df.copy(), npartitions=4)
            ddf = explicit_comms_shuffle(ddf, ["key"])

            # Check the values of `ddf` (ignoring the row order)
            expected = df.sort_values("key")
            got = ddf.compute().sort_values("key")
            assert_eq(got, expected)


@pytest.mark.parametrize("protocol", ["tcp", "ucx"])
def test_jit_unspill(protocol):
    pytest.importorskip("cudf")

    p = mp.Process(target=_test_jit_unspill, args=(protocol,))
    p.start()
    p.join()
    assert not p.exitcode


def _test_lock_workers(scheduler_address, ranks):
    async def f(_):
        worker = get_worker()
        if hasattr(worker, "running"):
            assert not worker.running
        worker.running = True
        await asyncio.sleep(0.5)
        assert worker.running
        worker.running = False

    with Client(scheduler_address) as client:
        c = comms.CommsContext(client)
        c.run(f, workers=[c.worker_addresses[r] for r in ranks], lock_workers=True)


def test_lock_workers():
    """
    Testing `run(...,lock_workers=True)` by spawning 30 runs with overlapping
    and non-overlapping worker sets.
    """
    try:
        from distributed import MultiLock  # noqa F401
    except ImportError as e:
        pytest.skip(str(e))

    with LocalCluster(
        protocol="tcp",
        dashboard_address=None,
        n_workers=4,
        threads_per_worker=5,
        processes=True,
    ) as cluster:
        ps = []
        for _ in range(5):
            for ranks in [[0, 1], [1, 3], [2, 3]]:
                ps.append(
                    mp.Process(
                        target=_test_lock_workers,
                        args=(cluster.scheduler_address, ranks),
                    )
                )
                ps[-1].start()

        for p in ps:
            p.join()

        assert all(p.exitcode == 0 for p in ps)
