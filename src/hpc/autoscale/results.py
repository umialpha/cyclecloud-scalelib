""" A collection of Result types that are used throughout the demand calculation."""
import typing
from abc import ABC, abstractmethod
from collections.abc import Hashable
from typing import Callable, Dict, List, Optional, TypeVar
from uuid import uuid4

import hpc.autoscale.hpclogging as logging
from hpc.autoscale import hpctypes as ht
from hpc.autoscale import node as nodepkg

if typing.TYPE_CHECKING:
    from hpc.autoscale.node.node import Node

Reasons = Optional[List[str]]  # pylint: disable=invalid-name


HANDLERS: List[Callable[["Result"], None]] = []

R = TypeVar("R", bound=Callable[["Result"], None])


def register_result_handler(handler: R) -> R:
    HANDLERS.append(handler)
    return handler


def unregister_result_handler(handler: R) -> Optional[R]:
    try:
        HANDLERS.remove(handler)
        return handler
    except ValueError:
        return None


def unregister_all_result_handlers() -> None:
    HANDLERS.clear()


def fire_result_handlers(result: "Result") -> None:
    for handler in HANDLERS:
        handler(result)


class Result(ABC):
    def __init__(self, status: str, reasons: Reasons) -> None:
        self.status = status
        self.reasons = reasons or []
        self.result_id = str(uuid4())

    def __bool__(self) -> bool:
        return self.status == "success"

    @abstractmethod
    def __str__(self) -> str:
        ...

    def __repr__(self) -> str:
        return str(self)


class AllocationResult(Result):
    def __init__(
        self,
        status: str,
        nodes: Optional[List["Node"]] = None,
        slots_allocated: Optional[int] = None,
        reasons: Reasons = None,
    ) -> None:
        Result.__init__(self, status, reasons)
        if status == "success":
            assert nodes
            pass
        self.nodes = nodes or []
        self.total_core_count = sum([x.vcpu_count for x in nodes]) if nodes else 0
        self.total_memory = sum([x.vcpu_count for x in nodes]) if nodes else 0

        if self:
            assert slots_allocated is not None
            assert slots_allocated > 0
        self.total_slots = slots_allocated or -1

        fire_result_handlers(self)

    def __str__(self) -> str:
        if self:
            return "AllocationResult(status={}, nodes={})".format(
                self.status, self.nodes
            )
        else:
            return "AllocationResult(status={}, reason={})".format(
                self.status, self.reasons
            )


class MatchResult(Result):
    def __init__(
        self,
        status: str,
        node: "Node",
        slots: int,
        reasons: Optional[List[str]] = None,
    ) -> None:
        Result.__init__(self, status, reasons)
        self.node = node
        self.total_slots = slots
        if slots:
            assert slots > 0
        if self.reasons:
            assert not isinstance(self.reasons[0], list)
        fire_result_handlers(self)

    def __str__(self) -> str:
        reasons = " AND ".join(self.reasons)
        if self:
            return "MatchResult(status={}, node={}, tasks={})".format(
                self.status, repr(self.node), self.total_slots
            )
        else:
            return "MatchResult(status={}, node={}, reason={})".format(
                self.status, repr(self.node), reasons
            )


class CandidatesResult(Result):
    def __init__(
        self,
        status: str,
        candidates: Optional[List["nodepkg.bucket.NodeBucket"]] = None,
        child_results: List[Result] = None,
    ) -> None:
        Result.__init__(self, status, [str(r) for r in (child_results or [])])
        self.candidates = candidates or []  # List[NodeBucket]
        self.child_results = child_results
        fire_result_handlers(self)

    def __str__(self) -> str:
        reasons = " AND ".join(set(self.reasons))[:150]
        if self:
            return "CandidatesResult(status={}, candidates={})".format(
                self.status, [str(x) for x in self.candidates]
            )
        else:
            return "CandidatesResult(status={}, reason={})".format(self.status, reasons)

    def __repr__(self) -> str:
        reasons = " AND ".join(set(self.reasons))
        if self:
            return "CandidatesResult(status={}, candidates={})".format(
                self.status, self.candidates
            )
        else:
            return "CandidatesResult(status={}, reason={})".format(self.status, reasons)


class SatisfiedResult(Result):
    def __init__(
        self,
        status: str,
        constraint: "nodepkg.constraints.NodeConstraint",
        node: "Node",
        reasons: Reasons = None,
        score: Optional[int] = 1,
    ) -> None:
        Result.__init__(self, status, reasons)
        self.score = score
        self.constraint = constraint
        self.node = node
        fire_result_handlers(self)

    def __int__(self) -> int:
        if self.score is None:
            return int(bool(self))
        return self.score

    def __str__(self) -> str:
        reasons = " AND ".join(set(self.reasons))

        if self:
            return "SatisfiedResult(status={}, node={}, score={}, constraint={})".format(
                self.status, self.node, self.score, self.constraint
            )
        else:
            return "SatisfiedResult(status={}, node={},reason={})".format(
                self.status, self.node, reasons
            )


class BootupResult(Result):
    def __init__(
        self,
        status: str,
        operation_id: ht.OperationId,
        request_id: Optional[ht.RequestId],
        nodes: Optional[List["Node"]] = None,
        reasons: Reasons = None,
    ) -> None:
        Result.__init__(self, status, reasons)
        self.operation_id = operation_id
        self.request_id = request_id
        self.nodes = nodes
        fire_result_handlers(self)

    def __str__(self) -> str:
        reasons = " AND ".join(set(self.reasons))
        if self:
            return "BootupResult(status={}, nodes={})".format(self.status, self.nodes)
        else:
            return "BootupResult(status={}, reason={})".format(self.status, reasons)


class ResultsHandler(ABC):
    @abstractmethod
    def __call__(self, result: Result) -> None:
        pass


class DefaultContextHandler(ResultsHandler):
    """
        This class does the following:
            1) Logs each result, with a prefix
                logging.debug("[my-custom-context]: %s", result)
            2) Collects each result on a per-context basis
            3) Adds metadata to the nodes so that you can correlate contexts with nodes.
                if "my-custom-id" in node.metadata["contexts"]:
                    ...

            handler = ContextHandler("[relevant-id]")
            results.register_result_handler(handler)
            ...
            handler.set_context("[new-id]")
            node_mgr.allocate...
            ...
            handler.set_context("[booting]")
            node.bootup(subset_of_nodes)

            for result in handler.by_context["[relevant-id]"]:
                ...

            for result in handler.by_context["[new-id]"]:
                ...

            for result in handler.by_context["[booting]"]:
                ...

            for node in node.get_nodes():
                if "[relevant-id]" in node.metadata["contexts"]:
                    ...
    """

    def __init__(self, ctx: Hashable) -> None:
        self.ctx = ctx
        self.by_context: Dict[Hashable, List[Result]] = {ctx: []}

    def set_context(self, ctx: Hashable, ctx_str: Optional[str] = None) -> None:
        self.ctx = ctx
        if self.ctx not in self.by_context:
            self.by_context[ctx] = []

    def __call__(self, result: Result) -> None:
        logging.debug("%s: %s", self.ctx, result)

        self.by_context[self.ctx].append(result)

        if hasattr(result, "nodes") and getattr(result, "nodes"):
            for result_node in getattr(result, "nodes"):
                if "contexts" not in result_node.metadata:
                    result_node.metadata["contexts"] = set()

                result_node.metadata["contexts"].add(self.ctx)

    def __str__(self) -> str:
        return "DefaultContextHandler(cur='{}', all='{}'".format(
            self.ctx, list(self.by_context.keys())
        )

    def __repr__(self) -> str:
        return str(self)
