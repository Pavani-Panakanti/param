# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import os
from itertools import cycle
from typing import List, Optional

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
from comms_utils import backendFunctions, collectiveArgsHolder, paramProfile

try:
    from internals import all_to_all_internal, all_to_allv_internal, extend_distributed

    has_ext_dist = True
except ImportError:
    try:
        # Open-source extend_distributed.py can be found in https://github.com/facebookresearch/dlrm
        import extend_distributed

        has_ext_dist = True
    except ImportError:
        has_ext_dist = False

logger = logging.getLogger(__name__)


def _downcast(input, bitwidth):
    if bitwidth == 16:
        return input.to(torch.float16)
    elif bitwidth == 8:
        return input.to(torch.int8)
    else:
        raise NotImplementedError("Unsupported bitwidth. Set --bitwidth to 8/16/32")


# a future object or a tensor
# okay to use float32 because a prior check that ensures
# the original dtype is float32.
def _dequantize(obj):
    if obj is None:
        # invoked in a irrelevant rank
        return None
    elif type(obj) == torch.Tensor:
        # only call to() if it is not a float32 tensor
        if obj.dtype != torch.float32:
            return obj.to(torch.float32)
        else:
            return obj
    else:
        resultTensor = obj.value()[0]
        if resultTensor.dtype != torch.float32:
            return resultTensor.to(torch.float32)
        else:
            return resultTensor


class PyTorchDistBackend(backendFunctions):
    def get_collective_group(self, collectiveArgs):
        if self.use_ext_dist:
            return collectiveArgs.group.my_pg
        else:
            return collectiveArgs.group

    def sayHello(self):
        myhost = os.uname()[1]
        global_rank = self.get_global_rank()
        local_rank = self.get_local_rank()
        world_size = self.get_world_size()
        master_ip = self.comms_world_info.master_ip
        device = self.get_device()

        hello_msg = f"[Rank {global_rank:3}] host {myhost}, device: {device}, local_rank: {local_rank} world_size: {world_size}, master_ip: {master_ip}"

        try:
            from mpi4py import MPI
        except ImportError:
            print(hello_msg)
        else:
            # if mpi4py exists, use mpi to collect info and print prettier message :)
            comm = MPI.COMM_WORLD

            all_hello_msgs = comm.gather(hello_msg, root=0)
            if global_rank == 0:
                print(all_hello_msgs)

    # Collectives
    def all_reduce(self, collectiveArgs, retFlag=False, pair=False):
        # pair=True mode does not support quantization
        if (
            collectiveArgs.allreduce_qcomm != 32
            and collectiveArgs.allreduce_qcomm > 4
            and collectiveArgs.ipTensor.dtype == torch.float32
            and not pair
        ):
            # note: note that quantized is a new tensor
            # that is not collectiveArgs.ipTensor.
            # this means when all_reduce/reduce finished
            # quantized will hold the result instead of collectiveArgs.ipTensor
            # this is intended because we don't want to allocate new buffers
            # every time we call all_reduce (because if we don't, it will be float16 instead of float32).
            # That also means we can't use the output of  quantized all_reduce's for anything other than
            # benchmarking purpose.
            with paramProfile(
                timer=collectiveArgs.quant_time,
                description="# PARAM: Allreduce quantization #",
            ):
                quantized = _downcast(
                    collectiveArgs.ipTensor, collectiveArgs.allreduce_qcomm
                )
        else:
            quantized = (
                collectiveArgs.ipTensor if not pair else collectiveArgs.ipTensor_pair
            )
        if self.use_ext_dist:
            retObj = collectiveArgs.group.all_reduce(
                tensor=quantized,
                op=collectiveArgs.op,
                async_op=collectiveArgs.asyncOp,
            )  # synchronicity is maintained in runColl
        else:
            retObj = dist.all_reduce(
                quantized,
                op=collectiveArgs.op,
                group=collectiveArgs.group,
                async_op=collectiveArgs.asyncOp,
            )  # synchronicity is maintained in runColl
        if (id(quantized) != id(collectiveArgs.ipTensor)) and not pair:
            if collectiveArgs.asyncOp:
                retObj = retObj.get_future().then(_dequantize)
            else:
                with paramProfile(
                    timer=collectiveArgs.dequant_time,
                    description="# PARAM: Allreduce de-quantization #",
                ):
                    retObj = _dequantize(quantized)

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def reduce(self, collectiveArgs, retFlag=False, pair=False):
        # pair=True mode does not support quantization
        if collectiveArgs.reduce_qcomm != 32 and not pair:
            assert collectiveArgs.ipTensor.dtype == torch.float32
            with paramProfile(
                timer=collectiveArgs.quant_time,
                description="# PARAM: Reduce quantization #",
            ):
                quantized = _downcast(
                    collectiveArgs.ipTensor, collectiveArgs.allreduce_qcomm
                )
        else:
            quantized = (
                collectiveArgs.ipTensor if not pair else collectiveArgs.ipTensor_pair
            )

        retObj = dist.reduce(
            quantized,
            dst=collectiveArgs.srcOrDst,
            op=collectiveArgs.op,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl
        if collectiveArgs.reduce_qcomm != 32 and not pair:
            if collectiveArgs.asyncOp:
                retObj = retObj.get_future().then(_dequantize)
            else:
                with paramProfile(
                    timer=collectiveArgs.dequant_time,
                    description="# PARAM: Reduce de-quantization #",
                ):
                    retObj = _dequantize(quantized)

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def all_to_all(
        self, collectiveArgs: collectiveArgsHolder, retFlag=False, pair=False
    ):
        # pair=True mode does not support quantization
        if collectiveArgs.all2all_qcomm and not pair:
            collectiveArgs.use_ext_dist = self.use_ext_dist
            work = all_to_all_internal(collectiveArgs)
        elif collectiveArgs.num_emb_tables_batched > 0 and self.use_ext_dist:
            work: List[extend_distributed.Request] = []
            dim_sum_per_rank = [
                collectiveArgs.num_emb_tables_batched * collectiveArgs.emb_dim
            ] * collectiveArgs.world_size

            for i in range(collectiveArgs.num_emb_ops):
                pooled_embs = collectiveArgs.emb[i](*collectiveArgs.embRequests[i])
                work += [
                    collectiveArgs.group.alltoall_pooled(
                        pooled_embs.reshape(
                            collectiveArgs.batch_size,
                            -1,
                            collectiveArgs.emb_dim,
                        ),
                        dim_sum_per_rank,
                    )
                ]

            for r in work:
                r.wait()
        else:
            if collectiveArgs.num_emb_tables_batched > 0:
                logger.warn(
                    "Not using batched embedding tables because extend distributed package not in use"
                )

            work = dist.all_to_all(
                collectiveArgs.opTensor if not pair else collectiveArgs.opTensor_pair,
                collectiveArgs.ipTensor if not pair else collectiveArgs.ipTensor_pair,
                group=self.get_collective_group(collectiveArgs),
                async_op=collectiveArgs.asyncOp,
            )

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(work)

        if retFlag:
            return work

    def all_to_allv(self, collectiveArgs, retFlag=False, pair=False):
        # pair=True mode does not support quantization
        if (
            collectiveArgs.all2all_qcomm
            and collectiveArgs.ipTensor.dtype == torch.float32
            and (
                collectiveArgs.opTensor.nelement() >= collectiveArgs.quant_threshold
                or collectiveArgs.ipTensor.nelement() >= collectiveArgs.quant_threshold
            )
            and not pair
        ):
            work = all_to_allv_internal(collectiveArgs)
        elif self.use_ext_dist:
            work = collectiveArgs.group.alltoall_single(
                collectiveArgs.opTensor if not pair else collectiveArgs.opTensor_pair,
                collectiveArgs.ipTensor if not pair else collectiveArgs.ipTensor_pair,
                collectiveArgs.opTensor_split
                if not pair
                else collectiveArgs.opTensor_split_pair,
                collectiveArgs.ipTensor_split
                if not pair
                else collectiveArgs.ipTensor_split_pair,
                async_op=collectiveArgs.asyncOp,
            )
        else:
            work = dist.all_to_all_single(
                collectiveArgs.opTensor if not pair else collectiveArgs.opTensor_pair,
                collectiveArgs.ipTensor if not pair else collectiveArgs.ipTensor_pair,
                collectiveArgs.opTensor_split
                if not pair
                else collectiveArgs.opTensor_split_pair,
                collectiveArgs.ipTensor_split
                if not pair
                else collectiveArgs.ipTensor_split_pair,
                group=collectiveArgs.group,
                async_op=collectiveArgs.asyncOp,
            )

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(work)

        if retFlag:
            return work

    def all_gather(self, collectiveArgs, retFlag=False, pair=False):
        if self.use_ext_dist:
            retObj = collectiveArgs.group.all_gather(
                tensor_list=collectiveArgs.opTensor
                if not pair
                else collectiveArgs.opTensor_pair,
                tensor=collectiveArgs.ipTensor
                if not pair
                else collectiveArgs.ipTensor_pair,
                async_op=collectiveArgs.asyncOp,
            )
        else:
            retObj = dist.all_gather(
                tensor_list=collectiveArgs.opTensor
                if not pair
                else collectiveArgs.opTensor_pair,
                tensor=collectiveArgs.ipTensor
                if not pair
                else collectiveArgs.ipTensor_pair,
                group=collectiveArgs.group,
                async_op=collectiveArgs.asyncOp,
            )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def gather(self, collectiveArgs, retFlag=False, pair=False):
        if pair:
            ipTensors = collectiveArgs.ipTensor_pair
            opTensors = collectiveArgs.opTensor_pair
        else:
            ipTensors = collectiveArgs.ipTensor
            opTensors = collectiveArgs.opTensor

        retObj = dist.gather(
            gather_list=opTensors
            if (collectiveArgs.global_rank == collectiveArgs.srcOrDst)
            else None,
            tensor=ipTensors,
            dst=collectiveArgs.srcOrDst,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def scatter(self, collectiveArgs, retFlag=False, pair=False):
        if pair:
            ipTensors = collectiveArgs.ipTensor_pair
            opTensors = collectiveArgs.opTensor_pair
        else:
            ipTensors = collectiveArgs.ipTensor
            opTensors = collectiveArgs.opTensor

        retObj = dist.scatter(
            tensor=opTensors,
            scatter_list=ipTensors
            if (collectiveArgs.global_rank == collectiveArgs.srcOrDst)
            else None,
            src=collectiveArgs.srcOrDst,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def reduce_scatter(self, collectiveArgs, retFlag=False, pair=False):
        if self.use_ext_dist:
            retObj = collectiveArgs.group.reduce_scatter(
                output=collectiveArgs.opTensor,
                input_list=collectiveArgs.ipTensor,
                op=collectiveArgs.op,
                async_op=collectiveArgs.asyncOp,
            )  # synchronicity is maintained in runColl
        else:
            retObj = dist.reduce_scatter(
                output=collectiveArgs.opTensor,
                input_list=collectiveArgs.ipTensor,
                op=collectiveArgs.op,
                group=collectiveArgs.group,
                async_op=collectiveArgs.asyncOp,
            )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def reduce_scatter_base(self, collectiveArgs, retFlag=False, pair=False):
        retObj = dist.reduce_scatter_tensor(
            output=collectiveArgs.opTensor,
            input=collectiveArgs.ipTensor,
            op=collectiveArgs.op,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def all_gather_base(self, collectiveArgs, retFlag=False, pair=False):
        retObj = dist.all_gather_into_tensor(
            output_tensor=collectiveArgs.opTensor,
            input_tensor=collectiveArgs.ipTensor,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    # Many-to-one pattern
    def incast(self, collectiveArgs):
        if collectiveArgs.global_rank == collectiveArgs.srcOrDst:
            # root receives tensor from each of user-specified source ranks
            for idx, src_rank in enumerate(collectiveArgs.src_ranks):
                retObj = dist.irecv(
                    tensor=collectiveArgs.opTensor[idx],
                    src=src_rank,
                    group=self.get_collective_group(collectiveArgs),
                    tag=0,
                )
                collectiveArgs.waitObj.append(retObj)
            # complete outstanding irecvs if blocking
            if not collectiveArgs.asyncOp:
                self.complete_accel_ops(collectiveArgs, devSync=False)
        elif collectiveArgs.global_rank in collectiveArgs.src_ranks:
            # send local tensor to root
            if collectiveArgs.asyncOp:
                self.isend(collectiveArgs, collectiveArgs.srcOrDst)
            else:
                self.send(collectiveArgs, collectiveArgs.srcOrDst)

    def broadcast(self, collectiveArgs, retFlag=False, pair=False):
        retObj = dist.broadcast(
            tensor=collectiveArgs.opTensor
            if not pair
            else collectiveArgs.opTensor_pair,
            src=collectiveArgs.srcOrDst,
            group=self.get_collective_group(collectiveArgs),
            async_op=collectiveArgs.asyncOp,
        )  # synchronicity is maintained in runColl

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    # One-to-many pattern
    def multicast(self, collectiveArgs):
        if collectiveArgs.global_rank == collectiveArgs.srcOrDst:
            # root sends tensor to each of user-specified destination ranks
            for dst_rank in collectiveArgs.dst_ranks:
                self.isend(collectiveArgs, dst_rank)
            # complete outstanding isends if blocking
            if not collectiveArgs.asyncOp:
                self.complete_accel_ops(collectiveArgs, devSync=False)
        elif collectiveArgs.global_rank in collectiveArgs.dst_ranks:
            # recvs tensor from root
            if collectiveArgs.asyncOp:
                self.irecv(collectiveArgs, collectiveArgs.srcOrDst)
            else:
                self.recv(collectiveArgs, collectiveArgs.srcOrDst)

    def send(self, collectiveArgs, dst_rank, retFlag=False, tag=0):
        dist.send(
            tensor=collectiveArgs.ipTensor,
            dst=dst_rank,
            group=self.get_collective_group(collectiveArgs),
            tag=tag,
        )

    def recv(self, collectiveArgs, src_rank, retFlag=False, tag=0):
        dist.recv(
            tensor=collectiveArgs.opTensor,
            src=src_rank,
            group=self.get_collective_group(collectiveArgs),
            tag=tag,
        )

    def isend(self, collectiveArgs, dst_rank, retFlag=False, tag=0):
        retObj = dist.isend(
            tensor=collectiveArgs.ipTensor,
            dst=dst_rank,
            group=self.get_collective_group(collectiveArgs),
            tag=tag,
        )

        collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def irecv(self, collectiveArgs, src_rank, retFlag=False, tag=0):
        retObj = dist.irecv(
            tensor=collectiveArgs.opTensor,
            src=src_rank,
            group=self.get_collective_group(collectiveArgs),
            tag=tag,
        )

        collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def device_sync(self, collectiveArgs):
        dev_str = (
            self.commsParams["device"]
            if isinstance(self.commsParams, dict)
            else self.commsParams.device
        )
        if dev_str == "cuda":
            torch.cuda.synchronize(collectiveArgs.device)

    def complete_accel_ops(self, collectiveArgs, initOp=False, devSync=True):
        if initOp is True:
            temp = torch.ones([1], device=collectiveArgs.device)
            dist.all_reduce(temp)
        for waitReq in collectiveArgs.waitObj:
            if waitReq is not None:
                waitReq.wait()
        collectiveArgs.waitObj.clear()
        collectiveArgs.waitObjIds.clear()

        if devSync:
            self.device_sync(collectiveArgs)

    # retFlag not used
    def complete_single_op(self, collectiveArgs, retFlag=False):
        """only wait on the first op in the queue"""
        if len(collectiveArgs.waitObj) > 0:
            waitReq = collectiveArgs.waitObj.pop(0)
            if waitReq is not None:
                waitReq.wait()

            # to ensure GPU collective is completed
            self.device_sync(collectiveArgs)

    def wait(self, collectiveArgs, retFlag=False):

        # for backwards compatibility, use old wait functionality.
        if len(collectiveArgs.waitObjIds) == 0:
            self.complete_single_op(collectiveArgs)
            return

        """wait on op with the matching reqID"""
        if collectiveArgs.collectiveId in collectiveArgs.waitObjIds:
            waitObj = collectiveArgs.waitObjIds[collectiveArgs.collectiveId]
            if waitObj is not None:
                waitObj.wait()

    def barrier(self, collectiveArgs, name="dummy", retFlag=False):
        if self.use_ext_dist:
            retObj = collectiveArgs.group.barrier(async_op=collectiveArgs.asyncOp)
        else:
            retObj = dist.barrier(collectiveArgs.group, async_op=collectiveArgs.asyncOp)

        if collectiveArgs.asyncOp:
            collectiveArgs.waitObj.append(retObj)

        if retFlag:
            return retObj

    def sync_barrier(self, collectiveArgs, desc="dummy"):
        self.barrier(collectiveArgs, name=desc)
        self.complete_accel_ops(collectiveArgs)

    def get_reduce_op(self, opName):
        if opName == "sum":
            return dist.ReduceOp.SUM
        elif opName == "max":
            return dist.ReduceOp.MAX
        else:
            return dist.ReduceOp.SUM

    # Compute functions
    def compute_mm(self, collectiveArgs):
        self.gemm(collectiveArgs)

    def gemm(self, collectiveArgs):
        # Matrix multiplication as compute kernel
        collectiveArgs.MMout = torch.mm(collectiveArgs.MMin1, collectiveArgs.MMin2)

    def emb_lookup(self, collectiveArgs):
        # If we are using the batched embedding lookup with alltoall, don't do the embedding
        # lookup here, but pool it with the alltoalls in the collective
        if not (
            collectiveArgs.collective == "all_to_all"
            and collectiveArgs.num_emb_tables_batched != -1
            and self.use_ext_dist
        ):
            # Embedding table lookup as compute kernel
            for i in range(len(collectiveArgs.embRequests)):
                (indices, offsets, weights) = collectiveArgs.embRequests[i]
                collectiveArgs.LookupOut = collectiveArgs.emb[i].forward(
                    indices,
                    offsets,
                    weights,
                )

    # Memory related
    def get_mem_size(self, collectiveArgs, pair=False):
        _sizeBytes = 0
        # opTensor could be a list of tensor for all_gather/gather/incast, get the aggregated size
        if isinstance(collectiveArgs.opTensor, list):
            _sizeBytes = sum(
                [t.nelement() * t.element_size() for t in collectiveArgs.opTensor]
            )
        # reduce scatter
        elif isinstance(collectiveArgs.ipTensor, list):
            _sizeBytes = sum(
                [t.nelement() * t.element_size() for t in collectiveArgs.ipTensor]
            )
        # reduce_scatter_base and reduce_scatter_v should use input tensor for total memory size
        elif collectiveArgs.collective in ["reduce_scatter_v", "reduce_scatter_base"]:
            _sizeBytes = (
                collectiveArgs.ipTensor.nelement()
                * collectiveArgs.ipTensor.element_size()
            )
        else:
            _sizeBytes = (
                collectiveArgs.opTensor.nelement()
                * collectiveArgs.opTensor.element_size()
            )
        if pair:
            if isinstance(collectiveArgs.opTensor_pair, list):
                _sizeBytes = sum(
                    [
                        t.nelement() * t.element_size()
                        for t in collectiveArgs.opTensor_pair
                    ]
                )
            else:
                _sizeBytes = (
                    collectiveArgs.opTensor_pair.nelement()
                    * collectiveArgs.opTensor_pair.element_size()
                )

        return _sizeBytes

    def alloc_random(
        self, sizeArr, curRankDevice="cuda", dtype=torch.float32, scaleFactor=1.0
    ):
        if dtype in (torch.uint8, torch.int16, torch.int32, torch.long):
            ipTensor = torch.randint(
                low=0, high=10, size=sizeArr, device=curRankDevice, dtype=dtype
            )
        elif dtype == torch.bool:
            ipTensor = (
                torch.rand(sizeArr, device=curRankDevice, dtype=torch.float32) < 0.5
            )
        else:
            ipTensor = torch.rand(sizeArr, device=curRankDevice, dtype=dtype)
            if (scaleFactor) != 0:
                ipTensor = ipTensor / scaleFactor
        return ipTensor

    def alloc_embedding_tables(self, n, m, curRankDevice, dtype):
        EE = nn.EmbeddingBag(n, m, mode="sum", sparse=True)

        W = np.random.uniform(
            low=-(np.sqrt(1 / n)), high=np.sqrt(1 / n), size=(n, m)
        ).astype(np.float32)
        # approach 1

        EE.weight.data = torch.tensor(
            W, dtype=dtype, requires_grad=True, device=curRankDevice
        )
        return EE

    def alloc_empty(self, sizeArr, dtype, curRankDevice):
        return torch.empty(sizeArr, device=curRankDevice, dtype=dtype)

    def clear_memory(self, collectiveArgs):
        del collectiveArgs.ipTensor
        del collectiveArgs.opTensor
        if collectiveArgs.ipTensor_pair is not None:
            del collectiveArgs.ipTensor_pair
            del collectiveArgs.opTensor_pair

        torch.cuda.empty_cache()

    # Getting world-size and other information.
    def get_local_rank(self):
        return self.comms_world_info.local_rank

    def get_global_rank(self):
        return self.comms_world_info.global_rank

    def get_world_size(self):
        return self.comms_world_info.world_size

    def get_group_rank(self, group):
        return dist.get_rank(group)

    def get_device(self):
        """get current device: 'cpu' or 'cuda'"""
        # TODO: this is a temporary workaround; need to unify the type of commsParams in comms and dlrm
        dev_str = (
            self.commsParams["device"]
            if isinstance(self.commsParams, dict)
            else self.commsParams.device
        )
        my_dev = torch.device(dev_str)
        if dev_str == "cuda":
            # explicitly select the device ordinal based on the local rank
            ordinal = self.get_local_rank()
            if self.get_local_rank() == -1:
                logger.warning(
                    "Cannot determine device ordinal since LOCAL_RANK is -1. Try GPU 0 and continue. "
                )
                ordinal = 0
            my_dev = torch.device(f"cuda:{ordinal}")
        elif dev_str != "cpu":
            # sanity check, such error should be catched when parsing arguments
            raise ValueError(f"{dev_str} is not a valid device option")

        return my_dev

    def get_hw_device(self):
        self.get_device()

    def get_default_group(self):
        if self.use_ext_dist:
            return extend_distributed.from_process_group(dist.GroupMember.WORLD)
        else:
            # return the world group to always perform collectives on default PG
            return dist.GroupMember.WORLD

    def get_groups(self):
        return self.groups

    def get_num_pgs(self):
        return self.num_pgs

    def get_next_group(self):
        return next(self.round_robin_group)

    def set_device(self):
        """set current device: 'cpu' or 'cuda'"""
        dev_str = (
            self.commsParams["device"]
            if isinstance(self.commsParams, dict)
            else self.commsParams.device
        )
        if dev_str.startswith("cuda"):
            if self.get_local_rank() > torch.cuda.device_count():
                raise ValueError(
                    "Insufficient #GPUs: "
                    f"available {torch.cuda.device_count()} "
                    f"requested {self.get_local_rank()}"
                )
            torch.cuda.set_device(self.get_local_rank())

        logger.info(f"rank {self.get_global_rank()} set torch device to {dev_str}")

    def get_new_stream(self):
        """get/allocate a new stream"""
        if self.commsParams.device == "cuda":
            # TODO: optional to use high-priority stream
            return torch.cuda.Stream(device=self.get_device(), priority=0)
        else:
            return None

    def switch_stream(self, stream, device: Optional[torch.device]):
        """switch to a new stream and return the current stream"""
        if device is None:
            device = self.get_device()
        if stream is not None and device.type == "cuda":
            cur_stream = torch.cuda.current_stream(device=device)
            torch.cuda.set_stream(stream)
            return cur_stream
        else:
            return None

    # Init functions
    def __init__(self, comms_world_info, commsParams):
        super().__init__()
        self.use_ext_dist = commsParams.use_ext_dist
        self.comms_world_info = comms_world_info
        self.commsParams = commsParams
        # extra ops supported (Note these are not supported in pytorch_tpu_backend.py)
        self.collectiveFunc[
            "wait"
        ] = (
            self.wait
        )  # a noop until all collective operations can post a wait operation or specify async vs not async
        self.collectiveFunc["send"] = self.send
        self.collectiveFunc["recv"] = self.recv
        self.collectiveFunc["isend"] = self.isend
        self.collectiveFunc["irecv"] = self.irecv
        self.collectiveFunc[
            "pt2pt"
        ] = self.noop  # dummy entry to support pt2pt benchmark

        backend = (
            self.commsParams["backend"]
            if isinstance(self.commsParams, dict)
            else self.commsParams.backend
        )
        # Import ucc plugin
        if backend == "ucc":
            # try OSS/setup.py
            try:
                import torch_ucc  # noqa
            except ImportError:
                try:
                    from ucc_plugin import initialize_ucc_plugin
                except ImportError:
                    raise RuntimeError("Unable to import initialize_ucc_plugin")
                else:
                    initialize_ucc_plugin(backend)
        # Import Fairring
        if backend == "fairring":
            try:
                import fairring  # noqa
            except ImportError:
                raise RuntimeError("Unable to import Fairring")

    def get_new_pg(self, group_ranks, backend):
        if self.use_ext_dist:
            return extend_distributed.new_extend_process_group(
                ranks=group_ranks, backend=backend
            )
        else:
            return dist.new_group(ranks=group_ranks, backend=backend)

    def initialize_backend(self, master_ip, master_port, backend="gloo"):
        # Set CUDA device before initializing backend
        # Required for backends that don't do lazy initialization, e.g. UCC
        self.set_device()

        global_rank = self.get_global_rank()
        world_size = self.get_world_size()

        # Torch initializaiton
        # NOTE: MASTER_ADDR and MASTER_PORT should be set already in `comms_utils.py`
        if world_size > 0:
            os.environ["WORLD_SIZE"] = str(world_size)
        if global_rank >= 0:
            os.environ["RANK"] = str(global_rank)

        if has_ext_dist and self.use_ext_dist:
            extend_distributed.init_distributed(
                rank=global_rank, size=world_size, backend=backend
            )
        else:
            self.use_ext_dist = False

        if not dist.is_initialized():
            # init default process group if not yet initialized or extend_distributed failed or is disabled
            dist.init_process_group(backend, rank=global_rank, world_size=world_size)

        self.groups = {}

        # create additional groups
        for pg_id, group_ranks in self.commsParams.groupRanks.items():
            if (
                len(group_ranks) > world_size
            ):  # this means that --auto-shrink is enabled, only use default pg
                self.groups.clear()
                break
            if (
                len(group_ranks) == world_size
            ):  # this is the default group, it has already been created
                pg = self.get_default_group()
            else:
                pg = self.get_new_pg(group_ranks=group_ranks, backend=backend)
            self.groups[pg_id] = pg

        if len(self.groups) == 0:  # if no groups were provided, use default group
            self.groups[0] = self.get_default_group()

        self.num_pgs = len(self.groups)

        self.round_robin_group = cycle(list(self.groups.values()))

    def benchmark_comms(self):
        self.initialize_backend(
            self.comms_world_info.master_ip,
            self.comms_world_info.master_port,
            self.commsParams.backend,
        )
        index = 0  # used in TPU, where it is not initialized!
        self.commsParams.benchTime(index, self.commsParams, self)
        return

    def __del__(self):
        if dist.is_initialized():
            dist.destroy_process_group()
        pass
