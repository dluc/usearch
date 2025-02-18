from __future__ import annotations

# The purpose of this file is to provide Pythonic wrapper on top
# the native precompiled CPython module. It improves compatibility
# Python tooling, linters, and static analyzers. It also embeds JIT
# into the primary `Index` class, connecting USearch with Numba.
import os
import math
from dataclasses import dataclass
from typing import (
    Optional,
    Union,
    NamedTuple,
    List,
    Iterable,
    Tuple,
    Dict,
    Callable,
)

import numpy as np
from tqdm import tqdm

from usearch.compiled import Index as _CompiledIndex
from usearch.compiled import Indexes as _CompiledIndexes
from usearch.compiled import IndexStats as _CompiledIndexStats

from usearch.compiled import index_dense_metadata as _index_dense_metadata
from usearch.compiled import exact_search as _exact_search
from usearch.compiled import MetricKind, ScalarKind, MetricSignature
from usearch.compiled import (
    DEFAULT_CONNECTIVITY,
    DEFAULT_EXPANSION_ADD,
    DEFAULT_EXPANSION_SEARCH,
    USES_OPENMP,
    USES_SIMSIMD,
    USES_NATIVE_F16,
)

MetricKindBitwise = (
    MetricKind.Hamming,
    MetricKind.Tanimoto,
    MetricKind.Sorensen,
)


class CompiledMetric(NamedTuple):
    pointer: int
    kind: MetricKind
    signature: MetricSignature


Key = np.uint64

KeyOrKeysLike = Union[Key, Iterable[Key], int, Iterable[int], np.ndarray, memoryview]

VectorOrVectorsLike = Union[np.ndarray, Iterable[np.ndarray], memoryview]

DTypeLike = Union[str, ScalarKind]

MetricLike = Union[str, MetricKind, CompiledMetric]


def _normalize_dtype(dtype, metric: MetricKind = MetricKind.Cos) -> ScalarKind:
    if dtype is None or dtype == "":
        return ScalarKind.B1 if metric in MetricKindBitwise else ScalarKind.F32

    if isinstance(dtype, ScalarKind):
        return dtype

    if isinstance(dtype, str):
        dtype = dtype.lower()

    _normalize = {
        "f64": ScalarKind.F64,
        "f32": ScalarKind.F32,
        "f16": ScalarKind.F16,
        "i8": ScalarKind.I8,
        "b1": ScalarKind.B1,
        "b1x8": ScalarKind.B1,
        "float64": ScalarKind.F64,
        "float32": ScalarKind.F32,
        "float16": ScalarKind.F16,
        "int8": ScalarKind.I8,
        np.float64: ScalarKind.F64,
        np.float32: ScalarKind.F32,
        np.float16: ScalarKind.F16,
        np.int8: ScalarKind.I8,
        np.uint8: ScalarKind.B1,
    }
    return _normalize[dtype]


def _to_numpy_dtype(dtype: ScalarKind):
    _normalize = {
        ScalarKind.F64: np.float64,
        ScalarKind.F32: np.float32,
        ScalarKind.F16: np.float16,
        ScalarKind.I8: np.int8,
        ScalarKind.B1: np.uint8,
    }
    if dtype in _normalize.values():
        return dtype
    return _normalize[dtype]


def _normalize_metric(metric) -> MetricKind:
    if metric is None:
        return MetricKind.Cos

    if isinstance(metric, str):
        _normalize = {
            "cos": MetricKind.Cos,
            "cosine": MetricKind.Cos,
            "ip": MetricKind.IP,
            "dot": MetricKind.IP,
            "inner_product": MetricKind.IP,
            "l2sq": MetricKind.L2sq,
            "l2_sq": MetricKind.L2sq,
            "haversine": MetricKind.Haversine,
            "pearson": MetricKind.Pearson,
            "hamming": MetricKind.Hamming,
            "tanimoto": MetricKind.Tanimoto,
            "sorensen": MetricKind.Sorensen,
        }
        return _normalize[metric.lower()]

    return metric


def _search_in_compiled(
    compiled_callable: Callable,
    vectors: np.ndarray,
    *,
    log: Union[str, bool],
    batch_size: int,
    **kwargs,
) -> Union[Matches, BatchMatches]:
    #
    assert isinstance(vectors, np.ndarray), "Expects a NumPy array"
    assert vectors.ndim == 1 or vectors.ndim == 2, "Expects a matrix or vector"
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, len(vectors))
    count_vectors = vectors.shape[0]

    def distil_batch(
        batch_matches: BatchMatches,
    ) -> Union[BatchMatches, Matches]:
        return batch_matches[0] if count_vectors == 1 else batch_matches

    if log and batch_size == 0:
        batch_size = int(math.ceil(count_vectors / 100))

    if batch_size:
        tasks = [
            vectors[start_row : start_row + batch_size, :]
            for start_row in range(0, count_vectors, batch_size)
        ]
        tasks_matches = []
        name = log if isinstance(log, str) else "Search"
        pbar = tqdm(
            tasks,
            desc=name,
            total=count_vectors,
            unit="vector",
            disable=log is False,
        )
        for vectors in tasks:
            tuple_ = compiled_callable(vectors, **kwargs)
            tasks_matches.append(BatchMatches(*tuple_))
            pbar.update(vectors.shape[0])

        pbar.close()
        return distil_batch(
            BatchMatches(
                keys=np.vstack([m.keys for m in tasks_matches]),
                distances=np.vstack([m.distances for m in tasks_matches]),
                counts=np.concatenate([m.counts for m in tasks_matches], axis=None),
                visited_members=sum([m.visited_members for m in tasks_matches]),
                computed_distances=sum([m.computed_distances for m in tasks_matches]),
            )
        )

    else:
        tuple_ = compiled_callable(vectors, **kwargs)
        return distil_batch(BatchMatches(*tuple_))


def _add_to_compiled(
    compiled,
    *,
    keys,
    vectors,
    copy: bool,
    threads: int,
    log: Union[str, bool],
    batch_size: int,
) -> Union[int, np.ndarray]:
    assert isinstance(vectors, np.ndarray), "Expects a NumPy array"
    assert vectors.ndim == 1 or vectors.ndim == 2, "Expects a matrix or vector"
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, len(vectors))

    # Validate or generate the keys
    count_vectors = vectors.shape[0]
    generate_labels = keys is None
    if generate_labels:
        start_id = len(compiled)
        keys = np.arange(start_id, start_id + count_vectors, dtype=Key)
    else:
        if not isinstance(keys, Iterable):
            assert count_vectors == 1, "Each vector must have a key"
            keys = [keys]
        keys = np.array(keys).astype(Key)

    assert len(keys) == count_vectors

    # If logging is requested, and batch size is undefined, set it to grow 1% at a time:
    if log and batch_size == 0:
        batch_size = int(math.ceil(count_vectors / 100))

    # Split into batches and log progress, if needed
    if batch_size:
        keys = [
            keys[start_row : start_row + batch_size]
            for start_row in range(0, count_vectors, batch_size)
        ]
        vectors = [
            vectors[start_row : start_row + batch_size, :]
            for start_row in range(0, count_vectors, batch_size)
        ]
        tasks = zip(keys, vectors)
        name = log if isinstance(log, str) else "Add"
        pbar = tqdm(
            tasks,
            desc=name,
            total=count_vectors,
            unit="vector",
            disable=log is False,
        )
        for keys, vectors in tasks:
            compiled.add_many(keys, vectors, copy=copy, threads=threads)
            pbar.update(len(keys))

        pbar.close()

    else:
        compiled.add_many(keys, vectors, copy=copy, threads=threads)

    return keys


@dataclass
class Match:
    """This class contains information about retrieved vector."""

    key: int
    distance: float

    def to_tuple(self) -> tuple:
        return self.key, self.distance


@dataclass
class Matches:
    """This class contains information about multiple retrieved vectors for single query,
    i.e it is a set of `Match` instances."""

    keys: np.ndarray
    distances: np.ndarray

    visited_members: int = 0
    computed_distances: int = 0

    def __len__(self) -> int:
        return len(self.keys)

    def __getitem__(self, index: int) -> Match:
        if isinstance(index, int) and index < len(self):
            return Match(
                key=self.keys[index],
                distance=self.distances[index],
            )
        else:
            raise IndexError(f"`index` must be an integer under {len(self)}")

    def to_list(self) -> List[tuple]:
        """
        Convert matches to the list of tuples which contain matches' indices and distances to them.
        """

        return [(int(l), float(d)) for l, d in zip(self.keys, self.distances)]

    def __repr__(self) -> str:
        return f"usearch.Matches({len(self)})"


@dataclass
class BatchMatches:
    """This class contains information about multiple retrieved vectors for multiple queries,
    i.e it is a set of `Matches` instances."""

    keys: np.ndarray
    distances: np.ndarray
    counts: np.ndarray

    visited_members: int = 0
    computed_distances: int = 0

    def __len__(self) -> int:
        return len(self.counts)

    def __getitem__(self, index: int) -> Matches:
        if isinstance(index, int) and index < len(self):
            return Matches(
                keys=self.keys[index, : self.counts[index]],
                distances=self.distances[index, : self.counts[index]],
                visited_members=self.visited_members // len(self),
                computed_distances=self.computed_distances // len(self),
            )
        else:
            raise IndexError(f"`index` must be an integer under {len(self)}")

    def to_list(self) -> List[List[tuple]]:
        """Convert the result for each query to the list of tuples with information about its matches."""
        list_of_matches = [self.__getitem__(row) for row in range(self.__len__())]
        return [match.to_tuple() for matches in list_of_matches for match in matches]

    def mean_recall(self, expected: np.ndarray, count: Optional[int] = None) -> float:
        """Measures recall [0, 1] as of `Matches` that contain the corresponding
        `expected` entry anywhere among results."""
        return self.count_matches(expected, count=count) / len(expected)

    def count_matches(self, expected: np.ndarray, count: Optional[int] = None) -> int:
        """Measures recall [0, len(expected)] as of `Matches` that contain the corresponding
        `expected` entry anywhere among results.
        """
        assert len(expected) == len(self)
        recall = 0
        if count is None:
            count = self.keys.shape[1]

        if count == 1:
            recall = np.sum(self.keys[:, 0] == expected)
        else:
            for i in range(len(self)):
                recall += expected[i] in self.keys[i, :count]
        return recall

    def __repr__(self) -> str:
        return f"usearch.BatchMatches({np.sum(self.counts)} across {len(self)} queries)"


@dataclass
class Clustering:
    def __init__(
        self,
        index: Index,
        matches: BatchMatches,
        queries: Optional[np.ndarray] = None,
    ) -> None:
        if queries is None:
            queries = index._compiled.get_keys_in_slice()
        self.index = index
        self.queries = queries
        self.matches = matches

    def __repr__(self) -> str:
        return f"usearch.Clustering(for {len(self.queries)} queries)"

    @property
    def centroids_popularity(self) -> Tuple[np.ndarray, np.ndarray]:
        return np.unique(self.matches.keys, return_counts=True)

    def members_of(self, centroid: Key) -> np.ndarray:
        return self.queries[self.matches.keys.flatten() == centroid]

    def subcluster(self, centroid: Key, **clustering_kwards) -> Clustering:
        sub_keys = self.members_of(centroid)
        return self.index.cluster(keys=sub_keys, **clustering_kwards)

    def plot_centroids_popularity(self):
        from matplotlib import pyplot as plt

        _, sizes = self.centroids_popularity
        plt.yscale("log")
        plt.plot(sorted(sizes), np.arange(len(sizes)))
        plt.show()

    @property
    def network(self):
        import networkx as nx

        keys, sizes = self.centroids_popularity

        g = nx.Graph()
        for key, size in zip(keys, sizes):
            g.add_node(key, size=size)

        for i, i_key in enumerate(keys):
            for j_key in keys[:i]:
                d = self.index.pairwise_distance(i_key, j_key)
                g.add_edge(i_key, j_key, distance=d)

        return g


class IndexedKeys:
    """Smart-reference for the range of keys present in a specific `Index`"""

    def __init__(self, index: Index) -> None:
        self.index = index

    def __len__(self) -> int:
        return len(self.index)

    def __getitem__(
        self,
        offset_offsets_or_slice: Union[int, np.ndarray, slice],
    ) -> Union[Key, np.ndarray]:
        if isinstance(offset_offsets_or_slice, slice):
            start, stop, step = offset_offsets_or_slice.indices(len(self))
            if step:
                raise
            return self.index._compiled.get_keys_in_slice(start, stop - start)

        elif isinstance(offset_offsets_or_slice, Iterable):
            offsets = np.array(offset_offsets_or_slice)
            return self.index._compiled.get_keys_at_offsets(offsets)

        else:
            offset = int(offset_offsets_or_slice)
            return self.index._compiled.get_key_at_offset(offset)

    def __array__(self, dtype=None) -> np.ndarray:
        if dtype is None:
            dtype = Key
        return self.index._compiled.get_keys_in_slice().astype(dtype)


class Index:
    """Fast vector-search engine for dense equi-dimensional embeddings.

    Vector keys must be integers.
    Vectors must have the same number of dimensions within the index.
    Supports Inner Product, Cosine Distance, L^n measures like the Euclidean metric,
    as well as automatic downcasting to low-precision floating-point and integral
    representations.
    """

    def __init__(
        self,
        *,
        ndim: int = 0,
        metric: MetricLike = MetricKind.Cos,
        dtype: Optional[DTypeLike] = None,
        connectivity: Optional[int] = None,
        expansion_add: Optional[int] = None,
        expansion_search: Optional[int] = None,
        multi: bool = False,
        path: Optional[os.PathLike] = None,
        view: bool = False,
    ) -> None:
        """Construct the index and compiles the functions, if requested (expensive).

        :param ndim: Number of vector dimensions
        :type ndim: int
            Required for some metrics, pre-set for others.
            Haversine, for example, only applies to 2-dimensional latitude/longitude
            coordinates. Angular (Cos) and Euclidean (L2sq), obviously, apply to
            vectors with arbitrary number of dimensions.

        :param metric: Distance function
        :type metric: MetricLike, defaults to MetricKind.Cos
            Kind of the distance function, or the Numba `cfunc` JIT-compiled object.
            Possible `MetricKind` values: IP, Cos, L2sq, Haversine, Pearson,
            Hamming, Tanimoto, Sorensen.

        :param dtype: Scalar type for internal vector storage
        :type dtype: Optional[DTypeLike], defaults to None
            For continuous metrics can be: f16, f32, f64, or i8.
            For bitwise metrics it's implementation-defined, and can't change.
            Example: you can use the `f16` index with `f32` vectors in Euclidean space,
            which will be automatically downcasted.

        :param connectivity: Connections per node in HNSW
        :type connectivity: Optional[int], defaults to None
            Hyper-parameter for the number of Graph connections
            per layer of HNSW. The original paper calls it "M".
            Optional, but can't be changed after construction.

        :param expansion_add: Traversal depth on insertions
        :type expansion_add: Optional[int], defaults to None
            Hyper-parameter for the search depth when inserting new
            vectors. The original paper calls it "efConstruction".
            Can be changed afterwards, as the `.expansion_add`.

        :param expansion_search: Traversal depth on queries
        :type expansion_search: Optional[int], defaults to None
            Hyper-parameter for the search depth when querying
            nearest neighbors. The original paper calls it "ef".
            Can be changed afterwards, as the `.expansion_search`.

        :param multi: Allow multiple vectors with the same key
        :type multi: bool, defaults to True
        :param path: Where to store the index
        :type path: Optional[os.PathLike], defaults to None
        :param view: Are we simply viewing an immutable index
        :type view: bool, defaults to False
        """

        if connectivity is None:
            connectivity = DEFAULT_CONNECTIVITY
        if expansion_add is None:
            expansion_add = DEFAULT_EXPANSION_ADD
        if expansion_search is None:
            expansion_search = DEFAULT_EXPANSION_SEARCH

        assert isinstance(connectivity, int), "Expects integer `connectivity`"
        assert isinstance(expansion_add, int), "Expects integer `expansion_add`"
        assert isinstance(expansion_search, int), "Expects integer `expansion_search`"

        metric = _normalize_metric(metric)
        if isinstance(metric, MetricKind):
            self._metric_kind = metric
            self._metric_jit = None
            self._metric_pointer = 0
            self._metric_signature = MetricSignature.ArrayArraySize
        elif isinstance(metric, CompiledMetric):
            self._metric_jit = metric
            self._metric_kind = metric.kind
            self._metric_pointer = metric.pointer
            self._metric_signature = metric.signature
        else:
            raise ValueError(
                "The `metric` must be a `CompiledMetric` or a `MetricKind`"
            )

        # Validate, that the right scalar type is defined
        dtype = _normalize_dtype(dtype, self._metric_kind)
        self._compiled = _CompiledIndex(
            ndim=ndim,
            dtype=dtype,
            connectivity=connectivity,
            expansion_add=expansion_add,
            expansion_search=expansion_search,
            multi=multi,
            metric_kind=self._metric_kind,
            metric_pointer=self._metric_pointer,
            metric_signature=self._metric_signature,
        )

        self.path = path
        if path and os.path.exists(path):
            path = os.fspath(path)
            if view:
                self._compiled.view(path)
            else:
                self._compiled.load(path)

    @staticmethod
    def metadata(path: os.PathLike) -> Optional[dict]:
        path = os.fspath(path)
        if not os.path.exists(path):
            return None
        try:
            return _index_dense_metadata(path)
        except Exception:
            return None

    @staticmethod
    def restore(path: os.PathLike, view: bool = False) -> Optional[Index]:
        path = os.fspath(path)
        meta = Index.metadata(path)
        if not meta:
            return None
        return Index(
            ndim=meta["dimensions"],
            dtype=meta["kind_scalar"],
            metric=meta["kind_metric"],
            path=path,
            view=view,
        )

    def __len__(self) -> int:
        return self._compiled.__len__()

    def add(
        self,
        keys: KeyOrKeysLike,
        vectors: VectorOrVectorsLike,
        *,
        copy: bool = True,
        threads: int = 0,
        log: Union[str, bool] = False,
        batch_size: int = 0,
    ) -> Union[int, np.ndarray]:
        """Inserts one or move vectors into the index.

        For maximal performance the `keys` and `vectors`
        should conform to the Python's "buffer protocol" spec.

        To index a single entry:
            keys: int, vectors: np.ndarray.
        To index many entries:
            keys: np.ndarray, vectors: np.ndarray.

        When working with extremely large indexes, you may want to
        pass `copy=False`, if you can guarantee the lifetime of the
        primary vectors store during the process of construction.

        :param keys: Unique identifier(s) for passed vectors
        :type keys: Optional[KeyOrKeysLike], can be `None`
        :param vectors: Vector or a row-major matrix
        :type vectors: VectorOrVectorsLike
        :param copy: Should the index store a copy of vectors
        :type copy: bool, defaults to True
        :param threads: Optimal number of cores to use
        :type threads: int, defaults to 0
        :param log: Whether to print the progress bar
        :type log: Union[str, bool], defaults to False
        :param batch_size: Number of vectors to process at once
        :type batch_size: int, defaults to 0
        :return: Inserted key or keys
        :type: Union[int, np.ndarray]
        """
        return _add_to_compiled(
            self._compiled,
            keys=keys,
            vectors=vectors,
            copy=copy,
            threads=threads,
            log=log,
            batch_size=batch_size,
        )

    def search(
        self,
        vectors: VectorOrVectorsLike,
        count: int = 10,
        radius: float = math.inf,
        *,
        threads: int = 0,
        exact: bool = False,
        log: Union[str, bool] = False,
        batch_size: int = 0,
    ) -> Union[Matches, BatchMatches]:
        """
        Performs approximate nearest neighbors search for one or more queries.

        :param vectors: Query vector or vectors.
        :type vectors: VectorOrVectorsLike
        :param count: Upper count on the number of matches to find
        :type count: int, defaults to 10
        :param threads: Optimal number of cores to use
        :type threads: int, defaults to 0
        :param exact: Perform exhaustive linear-time exact search
        :type exact: bool, defaults to False
        :param log: Whether to print the progress bar, default to False
        :type log: Union[str, bool], optional
        :param batch_size: Number of vectors to process at once
        :type batch_size: int, defaults to 0
        :return: Matches for one or more queries
        :rtype: Union[Matches, BatchMatches]
        """

        return _search_in_compiled(
            self._compiled.search_many,
            vectors,
            # Batch scheduling:
            log=log,
            batch_size=batch_size,
            # Search constraints:
            count=count,
            exact=exact,
            threads=threads,
        )

    def contains(self, keys: KeyOrKeysLike) -> Union[bool, np.ndarray]:
        if isinstance(keys, Iterable):
            return self._compiled.contains_many(np.array(keys, dtype=Key))
        else:
            return self._compiled.contains_one(int(keys))

    def __contains__(self, keys: KeyOrKeysLike) -> Union[bool, np.ndarray]:
        return self.contains(keys)

    def count(self, keys: KeyOrKeysLike) -> Union[int, np.ndarray]:
        if isinstance(keys, Iterable):
            return self._compiled.count_many(np.array(keys, dtype=Key))
        else:
            return self._compiled.count_one(int(keys))

    def get(
        self,
        keys: KeyOrKeysLike,
        dtype: Optional[DTypeLike] = None,
    ) -> Union[Optional[np.ndarray], Tuple[Optional[np.ndarray]]]:
        """Looks up one or more keys from the `Index`, retrieving corresponding vectors.

        Returns `None`, if one key is requested, and its not present.
        Returns a (row) vector, if the key maps into a single vector.
        Returns a (row-major) matrix, if the key maps into a multiple vectors.
        If multiple keys are requested, composes many such responses into a `tuple`.

        :param keys: One or more keys to lookup
        :type keys: KeyOrKeysLike
        :return: One or more keys lookup results
        :rtype: Union[Optional[np.ndarray], Tuple[Optional[np.ndarray]]]
        """
        if not dtype:
            dtype = self.dtype
        else:
            dtype = _normalize_dtype(dtype)

        view_dtype = _to_numpy_dtype(dtype)

        def cast(result):
            if result is not None:
                return result.view(view_dtype)
            return result

        is_one = not isinstance(keys, Iterable)
        if is_one:
            keys = [keys]
        if not isinstance(keys, np.ndarray):
            keys = np.array(keys, dtype=Key)
        else:
            keys = keys.astype(Key)

        results = self._compiled.get_many(keys, dtype)
        results = [cast(result) for result in results]
        return results[0] if is_one else results

    def __getitem__(
        self, keys: KeyOrKeysLike
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """Looks up one or more keys from the `Index`, retrieving corresponding vectors.

        Returns `None`, if one key is requested, and its not present.
        Returns a (row) vector, if the key maps into a single vector.
        Returns a (row-major) matrix, if the key maps into a multiple vectors.
        If multiple keys are requested, composes many such responses into a `tuple`.

        :param keys: One or more keys to lookup
        :type keys: KeyOrKeysLike
        :return: One or more keys lookup results
        :rtype: Union[Optional[np.ndarray], Tuple[Optional[np.ndarray]]]
        """
        return self.get(keys)

    def remove(
        self,
        keys: KeyOrKeysLike,
        *,
        compact: bool = False,
        threads: int = 0,
    ) -> Union[int, np.ndarray]:
        """Removes one or move vectors from the index.

        When working with extremely large indexes, you may want to
        mark some entries deleted, instead of rebuilding a filtered index.
        In other cases, rebuilding - is the recommended approach.

        :param keys: Unique identifier for passed vectors, optional
        :type keys: KeyOrKeysLike
        :param compact: Removes links to removed nodes (expensive), defaults to False
        :type compact: bool, optional
        :param threads: Optimal number of cores to use, defaults to 0
        :type threads: int, optional
        :return: Array of integers for the number of removed vectors per key
        :type: Union[int, np.ndarray]
        """
        if not isinstance(keys, Iterable):
            return self._compiled.remove_one(keys, compact=compact, threads=threads)
        else:
            keys = np.array(keys, dtype=Key)
            return self._compiled.remove_many(keys, compact=compact, threads=threads)

    def __delitem__(self, keys: KeyOrKeysLike) -> Union[int, np.ndarray]:
        raise self.remove(keys)

    def rename(
        self,
        from_: KeyOrKeysLike,
        to: KeyOrKeysLike,
    ) -> Union[int, np.ndarray]:
        """Rename existing member vector or vectors.

        May be used in iterative clustering procedures, where one would iteratively
        relabel every vector with the name of the cluster an entry belongs to, until
        the system converges.

        :param from_: One or more keys to be renamed
        :type from_: KeyOrKeysLike
        :param to: New name or names (of identical length as `from_`)
        :type to: KeyOrKeysLike
        :return: Number of vectors that were found and renamed
        :rtype: int
        """
        if isinstance(from_, Iterable):
            from_ = np.array(from_, dtype=Key)
            if isinstance(to, Iterable):
                to = np.array(to, dtype=Key)
                return self._compiled.rename_many_to_many(from_, to)

            else:
                return self._compiled.rename_many_to_one(from_, int(to))

        else:
            return self._compiled.rename_one_to_one(int(from_), int(to))

    @property
    def jit(self) -> bool:
        """
        :return: True, if the provided `metric` was JIT-ed
        :rtype: bool
        """
        return self._metric_jit is not None

    @property
    def hardware_acceleration(self) -> str:
        """Describes the kind of hardware-acceleration support used in
        that exact instance of the `Index`, for that metric kind, and
        the given number of dimensions.

        :return: "auto", if nothing is available, ISA subset name otherwise
        :rtype: str
        """
        return self._compiled.hardware_acceleration

    @property
    def size(self) -> int:
        return self._compiled.size

    @property
    def ndim(self) -> int:
        return self._compiled.ndim

    @property
    def metric(self) -> Union[MetricKind, CompiledMetric]:
        return self._metric_jit if self._metric_jit else self._metric_kind

    @metric.setter
    def metric(self, metric: MetricLike):
        metric = _normalize_metric(metric)
        if isinstance(metric, MetricKind):
            metric_kind = metric
            metric_pointer = 0
            metric_signature = MetricSignature.ArrayArraySize
        elif isinstance(metric, CompiledMetric):
            metric_kind = metric.kind
            metric_pointer = metric.pointer
            metric_signature = metric.signature
        else:
            raise ValueError(
                "The `metric` must be a `CompiledMetric` or a `MetricKind`"
            )

        return self._compiled.change_metric(
            metric_kind=metric_kind,
            metric_pointer=metric_pointer,
            metric_signature=metric_signature,
        )

    @property
    def dtype(self) -> ScalarKind:
        return self._compiled.dtype

    @property
    def connectivity(self) -> int:
        return self._compiled.connectivity

    @property
    def capacity(self) -> int:
        return self._compiled.capacity

    @property
    def memory_usage(self) -> int:
        return self._compiled.memory_usage

    @property
    def expansion_add(self) -> int:
        return self._compiled.expansion_add

    @property
    def expansion_search(self) -> int:
        return self._compiled.expansion_search

    @expansion_add.setter
    def expansion_add(self, v: int):
        self._compiled.expansion_add = v

    @expansion_search.setter
    def expansion_search(self, v: int):
        self._compiled.expansion_search = v

    def save(self, path: Optional[os.PathLike] = None):
        path = path if path else self.path
        if path is None:
            raise Exception("Define `path` argument")
        self._compiled.save(os.fspath(path))

    def load(self, path: Optional[os.PathLike] = None):
        path = path if path else self.path
        if path is None:
            raise Exception("Define `path` argument")
        self._compiled.load(os.fspath(path))

    def view(self, path: Optional[os.PathLike] = None):
        path = path if path else self.path
        if path is None:
            raise Exception("Define `path` argument")
        self._compiled.view(os.fspath(path))

    def clear(self):
        """Erases all the vectors from the index, preserving the space for future insertions."""
        self._compiled.clear()

    def reset(self):
        """Erases all members from index, closing files, and returning RAM to OS."""
        self._compiled.reset()

    def __del__(self):
        self.reset()

    def copy(self) -> Index:
        result = Index(
            ndim=self.ndim,
            metric=self.metric,
            dtype=self.dtype,
            connectivity=self.connectivity,
            expansion_add=self.expansion_add,
            expansion_search=self.expansion_search,
            path=self.path,
        )
        result._compiled = self._compiled.copy()
        return result

    def join(
        self,
        other: Index,
        max_proposals: int = 0,
        exact: bool = False,
    ) -> Dict[Key, Key]:
        """Performs "Semantic Join" or pairwise matching between `self` & `other` index.
        Is different from `search`, as no collisions are allowed in resulting pairs.
        Uses the concept of "Stable Marriages" from Combinatorics, famous for the 2012
        Nobel Prize in Economics.

        :param other: Another index.
        :type other: Index
        :param max_proposals: Limit on candidates evaluated per vector, defaults to 0
        :type max_proposals: int, optional
        :param exact: Controls if underlying `search` should be exact, defaults to False
        :type exact: bool, optional
        :return: Mapping from keys of `self` to keys of `other`
        :rtype: Dict[Key, Key]
        """
        return self._compiled.join(
            other=other._compiled,
            max_proposals=max_proposals,
            exact=exact,
        )

    def cluster(
        self,
        *,
        vectors: Optional[np.ndarray] = None,
        keys: Optional[np.ndarray] = None,
        min_count: Optional[int] = None,
        max_count: Optional[int] = None,
        threads: int = 0,
        log: Union[str, bool] = False,
        batch_size: int = 0,
    ) -> Clustering:
        """
        Clusters already indexed or provided `vectors`, mapping them to various centroids.

        :param vectors: .
        :type vectors: Optional[VectorOrVectorsLike]
        :param count: Upper bound on the number of clusters to produce
        :type count: Optional[int], defaults to None

        :param threads: Optimal number of cores to use,
        :type threads: int, defaults to 0
        :param log: Whether to print the progress bar
        :type log: Union[str, bool], defaults to False
        :param batch_size: Number of vectors to process at once, defaults to 0
        :type batch_size: int, defaults to 0
        :return: Matches for one or more queries
        :rtype: Union[Matches, BatchMatches]
        """
        if min_count is None:
            min_count = 0
        if max_count is None:
            max_count = 0

        if vectors is not None:
            assert keys is None, "You can either cluster vectors or member keys"
            results = self._compiled.cluster_vectors(
                vectors,
                min_count=min_count,
                max_count=max_count,
                threads=threads,
            )
        else:
            if keys is None:
                keys = self._compiled.get_keys_in_slice()
            if not isinstance(keys, np.ndarray):
                keys = np.array(keys)
            keys = keys.astype(Key)
            results = self._compiled.cluster_keys(
                keys,
                min_count=min_count,
                max_count=max_count,
                threads=threads,
            )

        batch_matches = BatchMatches(*results)
        return Clustering(self, batch_matches, keys)

    def pairwise_distance(
        self, left: KeyOrKeysLike, right: KeyOrKeysLike
    ) -> Union[np.ndarray, float]:
        assert isinstance(left, Iterable) == isinstance(right, Iterable)

        if not isinstance(left, Iterable):
            return self._compiled.pairwise_distance(int(left), int(right))
        else:
            left = np.array(left).astype(Key)
            right = np.array(right).astype(Key)
            return self._compiled.pairwise_distances(left, right)

    @property
    def keys(self) -> IndexedKeys:
        return IndexedKeys(self)

    @property
    def vectors(self) -> np.ndarray:
        return self.get(self.keys, vstack=True)

    @property
    def max_level(self) -> int:
        return self._compiled.max_level

    @property
    def nlevels(self) -> int:
        return self._compiled.max_level + 1

    @property
    def levels_stats(self) -> _CompiledIndexStats:
        """Get the accumulated statistics for the entire multi-level graph.

        :return: Statistics for the entire multi-level graph.
        :rtype: _CompiledIndexStats

        Statistics:
            - ``nodes`` (int): The number of nodes in that level.
            - ``edges`` (int): The number of edges in that level.
            - ``max_edges`` (int): The maximum possible number of edges in that level.
            - ``allocated_bytes`` (int): The amount of allocated memory for that level.
        """
        return self._compiled.levels_stats

    def level_stats(self, level: int) -> _CompiledIndexStats:
        """Get statistics for one level of the index - one graph.

        :return: Statistics for one level of the index - one graph.
        :rtype: _CompiledIndexStats

        Statistics:
            - ``nodes`` (int): The number of nodes in that level.
            - ``edges`` (int): The number of edges in that level.
            - ``max_edges`` (int): The maximum possible number of edges in that level.
            - ``allocated_bytes`` (int): The amount of allocated memory for that level.
        """
        return self._compiled.level_stats(level)

    @property
    def specs(self) -> Dict[str, Union[str, int, bool]]:
        return {
            "Class": "usearch.Index",
            "Connectivity": self.connectivity,
            "Size": self.size,
            "Dimensions": self.ndim,
            "Expansion@Add": self.expansion_add,
            "Expansion@Search": self.expansion_search,
            "OpenMP": USES_OPENMP,
            "SimSIMD": USES_SIMSIMD,
            "NativeF16": USES_NATIVE_F16,
            "JIT": self.jit,
            "DType": self.dtype,
            "Path": self.path,
        }

    def __repr__(self) -> str:
        f = "usearch.Index({} x {}, {}, connectivity: {}, expansion: {} & {}, {} vectors in {} levels)"
        return f.format(
            self.dtype,
            self.ndim,
            self.metric,
            self.connectivity,
            self.expansion_add,
            self.expansion_search,
            len(self),
            self.nlevels,
        )

    def _repr_pretty_(self, printer, cycle) -> str:
        level_stats = [
            f"--- {i}. {self.level_stats(i).nodes:,} nodes" for i in range(self.nlevels)
        ]
        lines = "\n".join(
            [
                "usearch.Index",
                "- config",
                f"-- data type: {self.dtype}",
                f"-- dimensions: {self.ndim}",
                f"-- metric: {self.metric}",
                f"-- connectivity: {self.connectivity}",
                f"-- expansion on addition:{self.expansion_add} candidates",
                f"-- expansion on search: {self.expansion_search} candidates",
                "- binary",
                f"-- uses OpenMP: {USES_OPENMP}",
                f"-- uses SimSIMD: {USES_SIMSIMD}",
                f"-- supports half-precision: {USES_NATIVE_F16}",
                f"-- uses hardware acceletion: {self.hardware_acceleration}",
                "- state",
                f"-- size: {self.size:,} vectors",
                f"-- memory usage: {self.memory_usage:,} bytes",
                f"-- max level: {self.max_level}",
                *level_stats,
            ]
        )
        printer.text(lines)


class Indexes:
    def __init__(
        self,
        indexes: Iterable[Index] = [],
        paths: Iterable[os.PathLike] = [],
        view: bool = False,
        threads: int = 0,
    ) -> None:
        self._compiled = _CompiledIndexes()
        for index in indexes:
            self._compiled.merge(index._compiled)
        self._compiled.merge_paths(paths, view=view, threads=threads)

    def merge(self, index: Index):
        self._compiled.merge(index._compiled)

    def merge_path(self, path: os.PathLike):
        self._compiled.merge_path(os.fspath(path))

    def __len__(self) -> int:
        return self._compiled.__len__()

    def search(
        self,
        vectors,
        count: int = 10,
        *,
        threads: int = 0,
        exact: bool = False,
    ):
        return _search_in_compiled(
            self._compiled.search_many,
            vectors,
            # Batch scheduling:
            log=False,
            batch_size=None,
            # Search constraints:
            count=count,
            exact=exact,
            threads=threads,
        )


def search(
    dataset: np.ndarray,
    query: np.ndarray,
    count: int = 10,
    metric: MetricLike = MetricKind.Cos,
    *,
    exact: bool = False,
    threads: int = 0,
    log: Union[str, bool] = False,
    batch_size: int = 0,
) -> Union[Matches, BatchMatches]:
    """Shortcut for search, that can avoid index construction. Particularly useful for
    tiny datasets, where brute-force exact search works fast enough.

    :param dataset: Row-major matrix.
    :type dataset: np.ndarray
    :param query: Query vector or vectors (also row-major), to find in `dataset`.
    :type query: np.ndarray

    :param count: Upper count on the number of matches to find, defaults to 10
    :type count: int, optional

    :param metric: Distance function
    :type metric: MetricLike, defaults to MetricKind.Cos
        Kind of the distance function, or the Numba `cfunc` JIT-compiled object.
        Possible `MetricKind` values: IP, Cos, L2sq, Haversine, Pearson,
        Hamming, Tanimoto, Sorensen.

    :param threads: Optimal number of cores to use, defaults to 0
    :type threads: int, optional
    :param exact: Perform exhaustive linear-time exact search, defaults to False
    :type exact: bool, optional
    :param log: Whether to print the progress bar, default to False
    :type log: Union[str, bool], optional
    :param batch_size: Number of vectors to process at once, defaults to 0
    :type batch_size: int, optional

    :return: Matches for one or more queries
    :rtype: Union[Matches, BatchMatches]
    """
    assert dataset.ndim == 2, "Dataset must be a matrix, with a vector in each row"

    if not exact:
        index = Index(
            dataset.shape[1],
            metric=metric,
            dtype=dataset.dtype,
        )
        index.add(
            None,
            dataset,
            threads=threads,
            log=log,
            batch_size=batch_size,
        )
        return index.search(
            query,
            count,
            threads=threads,
            log=log,
            batch_size=batch_size,
        )

    metric = _normalize_metric(metric)
    if isinstance(metric, MetricKind):
        metric_kind = metric
        metric_pointer = 0
        metric_signature = MetricSignature.ArrayArraySize
    elif isinstance(metric, CompiledMetric):
        metric_kind = metric.kind
        metric_pointer = metric.pointer
        metric_signature = metric.signature
    else:
        raise ValueError("The `metric` must be a `CompiledMetric` or a `MetricKind`")

    def search_batch(query, **kwargs):
        assert dataset.shape[1] == query.shape[1], "Number of dimensions differs"
        if dataset.dtype != query.dtype:
            query = query.astype(dataset.dtype)

        return _exact_search(
            dataset,
            query,
            metric_kind=metric_kind,
            metric_pointer=metric_pointer,
            metric_signature=metric_signature,
            **kwargs,
        )

    return _search_in_compiled(
        search_batch,
        query,
        # Batch scheduling:
        log=log,
        batch_size=batch_size,
        # Search constraints:
        count=count,
        threads=threads,
    )
