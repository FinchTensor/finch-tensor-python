import builtins
from typing import Any, Callable, Optional, Iterable, Literal
import warnings

import numpy as np
from numpy.core.numeric import normalize_axis_index, normalize_axis_tuple

from . import dtypes as jl_dtypes
from .julia import jl
from .levels import (
    _Display,
    Dense,
    Element,
    Storage,
    DenseStorage,
    SparseCOO,
    SparseList,
    sparse_formats_names,
)
from .typing import OrderType, JuliaObj, spmatrix, TupleOf3Arrays, DType


class SparseArray:
    """
    PyData/Sparse marker class
    """


class Tensor(_Display, SparseArray):
    """
    A wrapper class for Finch.Tensor and Finch.SwizzleArray.

    Constructors
    ------------
    Tensor(scipy.sparse.spmatrix)
        Construct a Tensor out of a `scipy.sparse` object. Supported formats are: `COO`, `CSC`, and `CSR`.
    Tensor(numpy.ndarray)
        Construct a Tensor out of a NumPy array object. This is a no-copy operation.
    Tensor(Storage)
        Initialize a Tensor with a `storage` description. `storage` can already hold data.
    Tensor(julia_object)
        Tensor created from a compatible raw Julia object. Must be a `SwizzleArray` or `LazyTensor`.
        This is a no-copy operation.

    Parameters
    ----------
    obj : np.ndarray or scipy.sparse or Storage or Finch.SwizzleArray
        Input to construct a Tensor. It's a no-copy operation of for NumPy and SciPy input. For Storage
        it's levels' description with order. The order numbers the dimensions from the fastest to slowest.
        The leaf nodes have mode `0` and the root node has mode `n-1`. If the tensor was square of size `N`,
        then `N .^ order == strides`. Available options are "C" (row-major), "F" (column-major), or a custom
        order. Default: row-major.
    fill_value : np.number, optional
        Only used when `arr : np.ndarray` is passed.

    Returns
    -------
    Tensor
        Python wrapper for Finch Tensor.

    Examples
    --------
    >>> import numpy as np
    >>> import finch
    >>> arr2d = np.arange(6).reshape((2, 3))
    >>> t1 = finch.Tensor(arr2d)
    >>> t1.todense()
    array([[0, 1, 2],
           [3, 4, 5]])
    >>> np.shares_memory(t1.todense(), arr2d)
    True
    >>> storage = finch.Storage(finch.Dense(finch.SparseList(finch.Element(1))), order="C")
    >>> t2 = t1.to_device(storage)
    >>> t2.todense()
    array([[0, 1, 2],
           [3, 4, 5]])
    """

    row_major: str = "C"
    column_major: str = "F"

    def __init__(
        self,
        obj: np.ndarray | spmatrix | Storage | JuliaObj,
        /,
        *,
        fill_value: np.number = 0.0,
    ):
        if isinstance(obj, (int, float, complex, bool)):
            obj = np.array(obj)

        if _is_scipy_sparse_obj(obj):  # scipy constructor
            jl_data = self._from_scipy_sparse(obj)
            self._obj = jl_data
        elif isinstance(obj, np.ndarray):  # numpy constructor
            jl_data = self._from_numpy(obj, fill_value=fill_value)
            self._obj = jl_data
        elif isinstance(obj, Storage):  # from-storage constructor
            order = self.preprocess_order(
                obj.order, self.get_lvl_ndim(obj.levels_descr._obj)
            )
            self._obj = jl.swizzle(jl.Tensor(obj.levels_descr._obj), *order)
        elif jl.isa(obj, jl.Finch.Tensor):  # raw-Julia-object constructors
            self._obj = jl.swizzle(obj, *tuple(range(1, jl.ndims(obj) + 1)))
        elif jl.isa(obj, jl.Finch.SwizzleArray) or jl.isa(obj, jl.Finch.LazyTensor):
            self._obj = obj
        else:
            raise ValueError(
                "Either `arr`, `storage` or a raw julia object should be provided."
            )

    def __pos__(self):
        return self._elemwise_op("+")

    def __neg__(self):
        return self._elemwise_op("-")

    def __add__(self, other):
        return self._elemwise_op("+", other)

    def __mul__(self, other):
        return self._elemwise_op("*", other)

    def __sub__(self, other):
        return self._elemwise_op("-", other)

    def __truediv__(self, other):
        return self._elemwise_op("/", other)

    def __floordiv__(self, other):
        return self._elemwise_op("//", other)

    def __mod__(self, other):
        return self._elemwise_op("rem", other)

    def __pow__(self, other):
        return self._elemwise_op("^", other)

    def __matmul__(self, other):
        # TODO: Implement and use mul instead of tensordot
        # https://github.com/willow-ahrens/finch-tensor/pull/22#issuecomment-2007884763
        if self.ndim != 2 or other.ndim != 2:
            raise ValueError(
                f"Both tensors must be 2-dimensional, but are: {self.ndim=} and {other.ndim=}."
            )
        return tensordot(self, other, axes=((-1,), (-2,)))

    def __abs__(self):
        return self._elemwise_op("abs")

    def __invert__(self):
        return self._elemwise_op("~")

    def __and__(self, other):
        return self._elemwise_op("&", other)

    def __or__(self, other):
        return self._elemwise_op("|", other)

    def __xor__(self, other):
        return self._elemwise_op("xor", other)

    def __lshift__(self, other):
        return self._elemwise_op("<<", other)

    def __rshift__(self, other):
        return self._elemwise_op(">>", other)

    def __lt__(self, other):
        return self._elemwise_op("<", other)

    def __le__(self, other):
        return self._elemwise_op("<=", other)

    def __gt__(self, other):
        return self._elemwise_op(">", other)

    def __ge__(self, other):
        return self._elemwise_op(">=", other)

    def __eq__(self, other):
        return self._elemwise_op("==", other)

    def __ne__(self, other):
        return self._elemwise_op("!=", other)

    def _elemwise_op(self, op: str, other: Optional["Tensor"] = None) -> "Tensor":
        if other is None:
            result = jl.broadcast(jl.seval(op), self._obj)
        else:
            axis_x1, axis_x2 = range(self.ndim, 0, -1), range(other.ndim, 0, -1)
            # inverse swizzle, so `broadcast` appends new dims to the front
            result = jl.broadcast(
                jl.seval(op),
                jl.permutedims(self._obj, tuple(axis_x1)),
                jl.permutedims(other._obj, tuple(axis_x2)),
            )
            # swizzle back to the original order
            result = jl.permutedims(result, tuple(range(jl.ndims(result), 0, -1)))

        return Tensor(result)

    def __getitem__(self, key):
        if not isinstance(key, tuple):
            key = (key,)

        if None in key:
            # lazy indexing mode
            key = _process_lazy_indexing(key)
        else:
            # standard indexing mode
            key = _expand_ellipsis(key, self.shape)
            key = _add_missing_dims(key, self.shape)
            key = _add_plus_one(key, self.shape)

        result = self._obj[key]
        if jl.isa(result, jl.Finch.SwizzleArray) or jl.isa(result, jl.Finch.LazyTensor):
            return Tensor(result)
        elif jl.isa(result, jl.Finch.Tensor):
            return Tensor(jl.swizzle(result, *range(1, jl.ndims(result) + 1)))
        else:
            return result

    @property
    def dtype(self) -> DType:
        return jl.eltype(self._obj.body)

    @property
    def ndim(self) -> int:
        return jl.ndims(self._obj)

    @property
    def shape(self) -> tuple[int, ...]:
        return jl.size(self._obj)

    @property
    def size(self) -> int:
        return np.prod(self.shape)

    @property
    def fill_value(self) -> np.number:
        return jl.default(self._obj)

    @property
    def _is_dense(self) -> bool:
        lvl = self._obj.body.lvl
        for _ in self.shape:
            if not jl.isa(lvl, jl.Finch.Dense):
                return False
            lvl = lvl.lvl
        return True

    @property
    def _order(self) -> tuple[int, ...]:
        return jl.typeof(self._obj).parameters[1]

    def is_computed(self) -> bool:
        return not jl.isa(self._obj, jl.Finch.LazyTensor)

    @classmethod
    def preprocess_order(cls, order: OrderType, ndim: int) -> tuple[int, ...]:
        if order == cls.column_major:
            permutation = tuple(range(1, ndim + 1))
        elif order == cls.row_major or order is None:
            permutation = tuple(range(1, ndim + 1)[::-1])
        elif isinstance(order, tuple):
            if builtins.min(order) == 0:
                order = tuple(i + 1 for i in order)
            if len(order) == ndim and builtins.all(
                [i in order for i in range(1, ndim + 1)]
            ):
                permutation = order
            else:
                raise ValueError(f"Custom order is not a permutation: {order}.")
        else:
            raise ValueError(
                f"order must be 'C', 'F' or a tuple, but is: {type(order)}."
            )

        return permutation

    @classmethod
    def get_lvl_ndim(cls, lvl: JuliaObj) -> int:
        ndim = 0
        while True:
            ndim += 1
            lvl = lvl.lvl
            if jl.isa(lvl, jl.Finch.Element):
                break
        return ndim

    def get_order(self, zero_indexing: bool = True) -> tuple[int, ...]:
        order = self._order
        if zero_indexing:
            order = tuple(i - 1 for i in order)
        return order

    def get_inv_order(self, zero_indexing: bool = True) -> tuple[int, ...]:
        inv_order = jl.invperm(self._order)
        if zero_indexing:
            inv_order = tuple(i - 1 for i in inv_order)
        return inv_order

    def todense(self) -> np.ndarray:
        obj = self._obj

        if self._is_dense:
            # don't materialize a dense finch tensor
            shape = jl.size(obj.body)
            dense_tensor = obj.body.lvl
        else:
            # create materialized dense array
            shape = jl.size(obj)
            dense_lvls = jl.Element(jl.default(obj))
            for _ in range(self.ndim):
                dense_lvls = jl.Dense(dense_lvls)
            dense_tensor = jl.Tensor(dense_lvls, obj).lvl  # materialize

        for _ in range(self.ndim):
            dense_tensor = dense_tensor.lvl

        result = np.asarray(jl.reshape(dense_tensor.val, shape))
        return result.transpose(self.get_inv_order()) if self._is_dense else result

    def permute_dims(self, axes: tuple[int, ...]) -> "Tensor":
        axes = tuple(i + 1 for i in axes)
        new_obj = jl.swizzle(self._obj, *axes)
        new_tensor = Tensor(new_obj)
        return new_tensor

    def to_device(self, device: Storage) -> "Tensor":
        return Tensor(self._from_other_tensor(self, storage=device))

    @classmethod
    def _from_other_tensor(cls, tensor: "Tensor", storage: Storage | None) -> JuliaObj:
        order = cls.preprocess_order(storage.order, tensor.ndim)
        return jl.swizzle(
            jl.Tensor(storage.levels_descr._obj, tensor._obj.body), *order
        )

    @classmethod
    def _from_numpy(cls, arr: np.ndarray, fill_value: np.number) -> JuliaObj:
        order_char = "F" if np.isfortran(arr) else "C"
        order = cls.preprocess_order(order_char, arr.ndim)
        inv_order = tuple(i - 1 for i in jl.invperm(order))

        dtype = arr.dtype.type
        if (
            dtype == np.bool_
        ):  # Fails with: Finch currently only supports isbits defaults
            dtype = jl_dtypes.bool
        lvl = Element(dtype(fill_value), arr.reshape(-1, order=order_char))
        for i in inv_order:
            lvl = Dense(lvl, arr.shape[i])
        return jl.swizzle(jl.Tensor(lvl._obj), *order)

    @classmethod
    def from_scipy_sparse(cls, x) -> "Tensor":
        if not _is_scipy_sparse_obj(x):
            raise ValueError("{x} is not a SciPy sparse object.")
        return Tensor(x)

    @classmethod
    def _from_scipy_sparse(cls, x) -> JuliaObj:
        if x.format not in ("coo", "csr", "csc"):
            x = x.asformat("coo")
        if not x.has_canonical_format:
            warnings.warn(
                "SciPy sparse input must be in a canonical format. "
                "Calling `sum_duplicates`."
            )
            x = x.copy()
            x.sum_duplicates()
            assert x.has_canonical_format

        if x.format == "coo":
            return cls.construct_coo_jl_object(
                coords=(x.col, x.row),
                data=x.data,
                shape=x.shape[::-1],
                order=Tensor.row_major,
            )
        elif x.format == "csc":
            return cls.construct_csc_jl_object(
                arg=(x.data, x.indices, x.indptr),
                shape=x.shape,
            )
        elif x.format == "csr":
            return cls.construct_csr_jl_object(
                arg=(x.data, x.indices, x.indptr),
                shape=x.shape,
            )
        else:
            raise ValueError(f"Unsupported SciPy format: {type(x)}")

    @classmethod
    def construct_coo_jl_object(
        cls, coords, data, shape, order, fill_value=0.0
    ) -> JuliaObj:
        assert len(coords) == 2
        ndim = len(shape)
        order = cls.preprocess_order(order, ndim)

        lvl = jl.Element(data.dtype.type(fill_value), data)
        ptr = jl.Vector[jl.Int]([1, len(data) + 1])
        tbl = tuple(jl.PlusOneVector(arr) for arr in coords)

        jl_data = jl.swizzle(
            jl.Tensor(jl.SparseCOO[ndim](lvl, shape, ptr, tbl)), *order
        )
        return jl_data

    @classmethod
    def construct_coo(
        cls, coords, data, shape, order=row_major, fill_value=0.0
    ) -> "Tensor":
        return Tensor(
            cls.construct_coo_jl_object(coords, data, shape, order, fill_value)
        )

    @staticmethod
    def _construct_compressed2d_jl_object(
        arg: TupleOf3Arrays,
        shape: tuple[int, ...],
        order: tuple[int, ...],
        fill_value: np.number = 0.0,
    ) -> JuliaObj:
        assert isinstance(arg, tuple) and len(arg) == 3
        assert len(shape) == 2

        data, indices, indptr = arg
        dtype = data.dtype.type
        indices = jl.PlusOneVector(indices)
        indptr = jl.PlusOneVector(indptr)

        lvl = jl.Element(dtype(fill_value), data)
        jl_data = jl.swizzle(
            jl.Tensor(
                jl.Dense(jl.SparseList(lvl, shape[0], indptr, indices), shape[1])
            ),
            *order,
        )
        return jl_data

    @classmethod
    def construct_csc_jl_object(
        cls, arg: TupleOf3Arrays, shape: tuple[int, ...]
    ) -> JuliaObj:
        return cls._construct_compressed2d_jl_object(arg=arg, shape=shape, order=(1, 2))

    @classmethod
    def construct_csc(cls, arg: TupleOf3Arrays, shape: tuple[int, ...]) -> "Tensor":
        return Tensor(cls.construct_csc_jl_object(arg, shape))

    @classmethod
    def construct_csr_jl_object(
        cls, arg: TupleOf3Arrays, shape: tuple[int, ...]
    ) -> JuliaObj:
        return cls._construct_compressed2d_jl_object(
            arg=arg, shape=shape[::-1], order=(2, 1)
        )

    @classmethod
    def construct_csr(cls, arg: TupleOf3Arrays, shape: tuple[int, ...]) -> "Tensor":
        return Tensor(cls.construct_csr_jl_object(arg, shape))

    @staticmethod
    def construct_csf_jl_object(
        arg: TupleOf3Arrays, shape: tuple[int, ...], fill_value: np.number = 0.0
    ) -> JuliaObj:
        assert isinstance(arg, tuple) and len(arg) == 3

        data, indices_list, indptr_list = arg
        dtype = data.dtype.type

        assert len(indices_list) == len(shape) - 1
        assert len(indptr_list) == len(shape) - 1

        indices_list = [jl.PlusOneVector(i) for i in indices_list]
        indptr_list = [jl.PlusOneVector(i) for i in indptr_list]

        lvl = jl.Element(dtype(fill_value), data)
        for size, indices, indptr in zip(shape[:-1], indices_list, indptr_list):
            lvl = jl.SparseList(lvl, size, indptr, indices)

        jl_data = jl.swizzle(
            jl.Tensor(jl.Dense(lvl, shape[-1])), *range(1, len(shape) + 1)
        )
        return jl_data

    @classmethod
    def construct_csf(cls, arg: TupleOf3Arrays, shape: tuple[int, ...]) -> "Tensor":
        return Tensor(cls.construct_csf_jl_object(arg, shape))

    def to_scipy_sparse(self):
        import scipy.sparse as sp

        if self.ndim != 2:
            raise ValueError(
                "Can only convert a 2-dimensional array to a Scipy sparse matrix."
            )
        if self.fill_value != 0:
            raise ValueError(
                "Can only convert arrays with 0 fill value to a Scipy sparse matrix."
            )
        order = self.get_order()
        body = self._obj.body

        if str(jl.typeof(body.lvl).name.name) == "SparseCOOLevel":
            data = np.asarray(body.lvl.lvl.val)
            coords = body.lvl.tbl
            row, col = coords[::-1] if order == (1, 0) else coords
            row, col = np.asarray(row) - 1, np.asarray(col) - 1
            return sp.coo_matrix((data, (row, col)), shape=self.shape)

        if (
            str(jl.typeof(body.lvl).name.name) == "DenseLevel"
            and str(jl.typeof(body.lvl.lvl).name.name) == "SparseListLevel"
        ):
            data = np.asarray(body.lvl.lvl.lvl.val)
            indices = np.asarray(body.lvl.lvl.idx) - 1
            indptr = np.asarray(body.lvl.lvl.ptr) - 1
            sp_class = sp.csr_matrix if order == (1, 0) else sp.csc_matrix
            return sp_class((data, indices, indptr), shape=self.shape)
        if (
            jl.typeof(body.lvl).name.name in sparse_formats_names
            or jl.typeof(body.lvl.lvl).name.name in sparse_formats_names
        ):
            storage = Storage(SparseCOO(self.ndim, Element(self.fill_value)), order)
            return self.to_device(storage).to_scipy_sparse()
        else:
            raise ValueError("Tensor can't be converted to scipy.sparse object.")

    def __array_namespace__(self, *, api_version: str | None = None) -> Any:
        if api_version is None:
            api_version = "2023.12"

        if api_version not in {"2021.12", "2022.12", "2023.12"}:
            raise ValueError(f'"{api_version}" Array API version not supported.')
        import finch

        return finch


def random(shape, density=0.01, random_state=None):
    args = [*shape, density]
    if random_state is not None:
        if isinstance(random_state, np.random.Generator):
            seed = random_state.integers(np.iinfo(np.int32).max)
        else:
            seed = random_state
        rng = jl.Random.default_rng()
        jl.Random.seed_b(rng, seed)
        args = [rng] + args
    return Tensor(jl.fsprand(*args))


def asarray(obj, /, *, dtype=None, format=None):
    if format not in {"coo", "csr", "csc", "csf", "dense", None}:
        raise ValueError(f"{format} format not supported.")
    tensor = obj if isinstance(obj, Tensor) else Tensor(obj)

    if format is not None:
        order = tensor.get_order()
        if format == "coo":
            storage = Storage(SparseCOO(tensor.ndim, Element(tensor.fill_value)), order)
        elif format == "csr":
            if order != (1, 0):
                raise ValueError("Invalid order for csr")
            storage = Storage(Dense(SparseList(Element(tensor.fill_value))), (2, 1))
        elif format == "csc":
            if order != (0, 1):
                raise ValueError("Invalid order for csc")
            storage = Storage(Dense(SparseList(Element(tensor.fill_value))), (1, 2))
        elif format == "csf":
            storage = Element(tensor.fill_value)
            for _ in range(tensor.ndim - 1):
                storage = SparseList(storage)
            storage = Storage(Dense(storage), order)
        elif format == "dense":
            storage = DenseStorage(tensor.ndim, tensor.dtype, order)
        tensor = tensor.to_device(storage)

    if dtype is not None:
        return astype(tensor, dtype)
    else:
        return tensor


def full(
    shape: int | tuple[int, ...],
    fill_value: jl_dtypes.number,
    *,
    dtype: DType | None = None,
    format: str = "coo",
) -> Tensor:
    if isinstance(shape, int):
        shape = (shape,)
    if dtype is not None:
        fill_value = dtype(fill_value)

    if format == "coo":
        return Tensor(
            jl.Tensor(jl.SparseCOO[len(shape)](jl.Element(fill_value)), *shape)
        )
    if format == "dense":
        return Tensor(np.full(shape, fill_value, dtype=jl_dtypes.jl_to_np_dtype[dtype]))
    raise ValueError(f"{format} format not supported.")


def full_like(
    x: Tensor,
    /,
    fill_value: jl_dtypes.number,
    *,
    dtype: DType | None = None,
    format: str = "coo",
) -> Tensor:
    return full(x.shape, fill_value, dtype=dtype, format=format)


def ones(
    shape: int | tuple[int, ...], *, dtype: DType | None = None, format: str = "coo"
) -> Tensor:
    return full(shape, jl_dtypes.int64(1), dtype=dtype, format=format)


def ones_like(
    x: Tensor, /, *, dtype: DType | None = None, format: str = "coo"
) -> Tensor:
    return ones(x.shape, dtype=dtype, format=format)


def zeros(
    shape: int | tuple[int, ...], *, dtype: DType | None = None, format: str = "coo"
) -> Tensor:
    return full(shape, jl_dtypes.int64(0), dtype=dtype, format=format)


def zeros_like(
    x: Tensor, /, *, dtype: DType | None = None, format: str = "coo"
) -> Tensor:
    return zeros(x.shape, dtype=dtype, format=format)


def permute_dims(x: Tensor, axes: tuple[int, ...]):
    return x.permute_dims(axes)


def astype(x: Tensor, dtype: DType, /, *, copy: bool = True):
    if not copy:
        if x.dtype == dtype:
            return x
        else:
            raise ValueError("Unable to avoid a copy while casting in no-copy mode.")
    else:
        finch_tns = x._obj.body
        result = jl.copyto_b(
            jl.similar(finch_tns, jl.default(finch_tns), dtype), finch_tns
        )
        return Tensor(jl.swizzle(result, *x.get_order(zero_indexing=False)))


def where(condition: Tensor, x1: Tensor, x2: Tensor, /) -> Tensor:
    axis_cond, axis_x1, axis_x2 = (
        range(condition.ndim, 0, -1),
        range(x1.ndim, 0, -1),
        range(x2.ndim, 0, -1),
    )
    # inverse swizzle, so `broadcast` appends new dims to the front
    result = jl.broadcast(
        jl.ifelse,
        jl.permutedims(condition._obj, tuple(axis_cond)),
        jl.permutedims(x1._obj, tuple(axis_x1)),
        jl.permutedims(x2._obj, tuple(axis_x2)),
    )
    # swizzle back to the original order
    result = jl.permutedims(result, tuple(range(jl.ndims(result), 0, -1)))
    return Tensor(result)


def nonzero(x: Tensor, /) -> tuple[np.ndarray, ...]:
    indices = jl.ffindnz(x._obj)[:-1]  # return only indices, skip values
    indices = tuple(np.asarray(i) - 1 for i in indices)
    sort_order = np.lexsort(indices[::-1])  # sort to row-major, C-style order
    return tuple(i[sort_order] for i in indices)


def _reduce(x: Tensor, fn: Callable, axis, dtype=None):
    if axis is not None:
        axis = normalize_axis_tuple(axis, x.ndim)
        axis = tuple(i + 1 for i in axis)
        result = fn(x._obj, dims=axis)
    else:
        result = fn(x._obj)

    if jl.isa(result, jl.Finch.Tensor) or jl.isa(result, jl.Finch.LazyTensor):
        result = Tensor(result)
    else:
        result = np.array(result)
    return result


def sum(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    dtype: DType | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.sum, axis, dtype)


def prod(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    dtype: DType | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.prod, axis, dtype)


def max(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.maximum, axis)


def min(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.minimum, axis)


def any(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.any, axis)


def all(
    x: Tensor,
    /,
    *,
    axis: int | tuple[int, ...] | None = None,
    keepdims: bool = False,
) -> Tensor:
    return _reduce(x, jl.all, axis)


def eye(
    n_rows: int,
    n_cols: int | None = None,
    /,
    *,
    k: int = 0,
    dtype: DType | None = None,
    format: Literal["coo", "dense"] = "coo",
) -> Tensor:
    n_cols = n_rows if n_cols is None else n_cols
    dtype = jl_dtypes.float64 if dtype is None else dtype
    if format == "coo":
        tns_def = "SparseCOO{2}" + f"(Element({dtype}(0.0)))"
    elif format == "dense":
        tns_def = f"Dense(Dense(Element({dtype}(0.0))))"
    else:
        raise ValueError(f"{format} not supported, only 'coo' and 'dense' is allowed.")

    obj = jl.seval(f"tns = Tensor({tns_def}, {n_rows}, {n_cols})")
    jl.seval(f"""
        @finch begin
            tns .= 0
            for i=_, j=_
                if i+{k} == j
                    tns[i, j] += 1
                end
            end
        end
    """)
    return Tensor(obj)


def tensordot(x1: Tensor, x2: Tensor, /, *, axes=2) -> Tensor:
    if isinstance(axes, Iterable):
        self_axes = normalize_axis_tuple(axes[0], x1.ndim)
        other_axes = normalize_axis_tuple(axes[1], x2.ndim)
        axes = (tuple(i + 1 for i in self_axes), tuple(i + 1 for i in other_axes))

    result = jl.tensordot(x1._obj, x2._obj, axes)
    return Tensor(result)


def log(x: Tensor, /) -> Tensor:
    return x._elemwise_op("log")


def log10(x: Tensor, /) -> Tensor:
    return x._elemwise_op("log10")


def log1p(x: Tensor, /) -> Tensor:
    return x._elemwise_op("log1p")


def log2(x: Tensor, /) -> Tensor:
    return x._elemwise_op("log2")


def sqrt(x: Tensor, /) -> Tensor:
    return x._elemwise_op("sqrt")


def sign(x: Tensor, /) -> Tensor:
    return x._elemwise_op("sign")


def round(x: Tensor, /) -> Tensor:
    return x._elemwise_op("round")


def exp(x: Tensor, /) -> Tensor:
    return x._elemwise_op("exp")


def expm1(x: Tensor, /) -> Tensor:
    return x._elemwise_op("expm1")


def floor(x: Tensor, /) -> Tensor:
    return x._elemwise_op("floor")


def ceil(x: Tensor, /) -> Tensor:
    return x._elemwise_op("ceil")


def cos(x: Tensor, /) -> Tensor:
    return x._elemwise_op("cos")


def cosh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("cosh")


def acos(x: Tensor, /) -> Tensor:
    return x._elemwise_op("acos")


def acosh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("acosh")


def sin(x: Tensor, /) -> Tensor:
    return x._elemwise_op("sin")


def sinh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("sinh")


def asin(x: Tensor, /) -> Tensor:
    return x._elemwise_op("asin")


def asinh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("asinh")


def tan(x: Tensor, /) -> Tensor:
    return x._elemwise_op("tan")


def tanh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("tanh")


def atan(x: Tensor, /) -> Tensor:
    return x._elemwise_op("atan")


def atanh(x: Tensor, /) -> Tensor:
    return x._elemwise_op("atanh")


def atan2(x: Tensor, other: Tensor, /) -> Tensor:
    return x._elemwise_op("atand", other)


def _is_scipy_sparse_obj(x):
    return hasattr(x, "__module__") and x.__module__.startswith("scipy.sparse")


def _slice_plus_one(s: slice, size: int) -> range:
    step = s.step if s.step is not None else 1
    start_default = size if step < 0 else 1
    stop_default = 1 if step < 0 else size

    if s.start is not None:
        start = normalize_axis_index(s.start, size) + 1 if s.start < size else size
    else:
        start = start_default

    if s.stop is not None:
        stop_offset = 2 if step < 0 else 0
        stop = (
            normalize_axis_index(s.stop, size) + stop_offset if s.stop < size else size
        )
    else:
        stop = stop_default

    return jl.range(start=start, step=step, stop=stop)


def _add_plus_one(key: tuple, shape: tuple[int, ...]) -> tuple:
    new_key = ()
    for idx, size in zip(key, shape):
        if isinstance(idx, int):
            new_key += (normalize_axis_index(idx, size) + 1,)
        elif isinstance(idx, slice):
            new_key += (_slice_plus_one(idx, size),)
        elif isinstance(idx, (list, np.ndarray, tuple)):
            idx = normalize_axis_tuple(idx, size)
            new_key += (jl.Vector([i + 1 for i in idx]),)
        elif idx is None:
            raise IndexError("`None` in the index is supported only in lazy indexing")
        else:
            new_key += (idx,)
    return new_key


def _expand_ellipsis(key: tuple, shape: tuple[int, ...]) -> tuple:
    ndim = len(shape)
    ellipsis_pos = None
    key_without_ellipsis = ()
    # first we need to find the ellipsis and confirm it's the only one
    for pos, idx in enumerate(key):
        if idx == Ellipsis:
            if ellipsis_pos is None:
                ellipsis_pos = pos
            else:
                raise IndexError("an index can only have a single ellipsis ('...')")
        else:
            key_without_ellipsis += (idx,)
    key = key_without_ellipsis

    # then we expand ellipsis with a full range
    if ellipsis_pos is not None:
        ellipsis_indices = range(ellipsis_pos, ellipsis_pos + ndim - len(key))
        new_key = ()
        key_iter = iter(key)
        for i in range(ndim):
            if i in ellipsis_indices:
                new_key = new_key + (jl.range(start=1, stop=shape[i]),)
            else:
                new_key = new_key + (next(key_iter),)
        key = new_key
    return key


def _add_missing_dims(key: tuple, shape: tuple[int, ...]) -> tuple:
    for i in range(len(key), len(shape)):
        key = key + (jl.range(start=1, stop=shape[i]),)
    return key


def _process_lazy_indexing(key: tuple) -> tuple:
    new_key = ()
    for idx in key:
        if idx == slice(None):
            new_key += (jl.Colon(),)
        elif idx is None:
            new_key += (jl.nothing,)
        else:
            raise ValueError(f"Invalid lazy index member: {idx}")
    return new_key
