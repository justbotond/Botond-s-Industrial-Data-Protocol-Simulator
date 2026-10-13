"""OPC UA server exposing simulated tag sets as variables."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from asyncua import Server, ua
from asyncua.crypto.permission_rules import User, UserRole

from .tags import TagSet, TagSpec

log = logging.getLogger("opcua")

VARIANT_TYPE = {
    "float32": ua.VariantType.Float, "float64": ua.VariantType.Double,
    "int16": ua.VariantType.Int16, "uint16": ua.VariantType.UInt16,
    "int32": ua.VariantType.Int32, "uint32": ua.VariantType.UInt32,
    "bool": ua.VariantType.Boolean,
}


class OpcUaSimServer:
    def __init__(self, name: str, host: str, port: int, update_interval_ms: int,
                 tag_groups: list[dict], namespace: str = "urn:botond:simulator",
                 writable: bool = False, folder_size: int = 1000):
        self.name, self.host, self.port = name, host, port
        self.folder_size = max(1, folder_size)
        self.interval = update_interval_ms / 1000
        self.namespace = namespace
        self.writable = writable
        self.tagsets = [TagSet(TagSpec(**g)) for g in tag_groups]
        self.nodeids: list[list[ua.NodeId]] = []
        self.ns_idx = 0
        self.ready = asyncio.Event()

    async def _build(self) -> Server:
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://{self.host}:{self.port}/{self.name}")
        server.set_server_name(f"Botond's Industrial Data Protocol Simulator - {self.name}")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns_idx = await server.register_namespace(self.namespace)
        root = await server.nodes.objects.add_folder(ua.NodeId(self.name, self.ns_idx), self.name)
        t0 = time.monotonic()
        for ts in self.tagsets:
            spec = ts.spec
            folder = await root.add_folder(ua.NodeId(f"{self.name}.{spec.name}", self.ns_idx), spec.name)
            vtype = VARIANT_TYPE[spec.data_type]
            access = ua.AccessLevel.CurrentRead.mask
            if self.writable:
                access |= ua.AccessLevel.CurrentWrite.mask
            # bulk AddNodes is orders of magnitude faster than add_variable() per node;
            # node creation slows down quadratically per parent, so split into sub-folders
            items, ids = [], []
            sub = folder
            for i, (tag, value) in enumerate(zip(ts.names(), ts.values)):
                if spec.count > self.folder_size and i % self.folder_size == 0:
                    last = min(i + self.folder_size, spec.count) - 1
                    sub_name = f"{spec.name}_{i}-{last}"
                    sub = await folder.add_folder(ua.NodeId(f"{self.name}.{sub_name}", self.ns_idx), sub_name)
                attrs = ua.VariableAttributes()
                attrs.DisplayName = ua.LocalizedText(tag)
                attrs.Value = ua.Variant(value, vtype)
                attrs.DataType = ua.NodeId(vtype.value)
                attrs.ValueRank = -1
                attrs.AccessLevel = attrs.UserAccessLevel = access
                item = ua.AddNodesItem()
                item.RequestedNewNodeId = ua.NodeId(f"{spec.name}.{tag}", self.ns_idx)
                item.BrowseName = ua.QualifiedName(tag, self.ns_idx)
                item.NodeClass = ua.NodeClass.Variable
                item.ParentNodeId = sub.nodeid
                item.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasComponent)
                item.TypeDefinition = ua.NodeId(ua.ObjectIds.BaseDataVariableType)
                item.NodeAttributes = attrs
                items.append(item)
                ids.append(item.RequestedNewNodeId)
            self._bulk_add(server, items)
            self.nodeids.append(ids)
        log.info("%s: created %d nodes in %.1f s", self.name, sum(len(i) for i in self.nodeids),
                 time.monotonic() - t0)
        return server

    @staticmethod
    def _bulk_add(server: Server, items: list[ua.AddNodesItem]) -> None:
        """Add many nodes in O(n).

        asyncua's AddNodes scans every existing reference of the parent for each new child,
        which is O(n^2) per folder (40,000 tags would take minutes). We add each node without
        a parent and then attach the parent/child references directly.
        """
        mgt = server.iserver.node_mgt_service
        aspace = server.iserver.aspace
        admin = User(role=UserRole.Admin)
        for item in items:
            parent_id = item.ParentNodeId
            item.ParentNodeId = ua.NodeId()
            res = mgt._add_node(item, admin, check=False)
            res.StatusCode.check()
            item.ParentNodeId = parent_id
            parent, node = aspace[parent_id], aspace[item.RequestedNewNodeId]
            desc = ua.ReferenceDescription()
            desc.ReferenceTypeId = item.ReferenceTypeId
            desc.NodeId = item.RequestedNewNodeId
            desc.NodeClass = item.NodeClass
            desc.BrowseName = item.BrowseName
            desc.DisplayName = item.NodeAttributes.DisplayName
            desc.TypeDefinition = item.TypeDefinition
            desc.IsForward = True
            parent.references.append(desc)
            mgt._add_ref_to_parent(node, item, parent)

    async def _update_loop(self, server: Server) -> None:
        aspace = server.iserver.aspace
        attval_groups = [[aspace[nid].attributes[ua.AttributeIds.Value] for nid in ids] for ids in self.nodeids]
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            now = datetime.now(timezone.utc)
            for ts, attvals in zip(self.tagsets, attval_groups):
                vtype = VARIANT_TYPE[ts.spec.data_type]
                for attval, v in zip(attvals, ts.tick()):
                    dv = ua.DataValue(ua.Variant(v, vtype), SourceTimestamp=now, ServerTimestamp=now)
                    # same as server.write_attribute_value() minus lookups and type checks
                    attval.value = dv
                    if attval.datachange_callbacks:  # only nodes a client has subscribed to
                        for k, cb in list(attval.datachange_callbacks.items()):
                            await cb(k, dv)
            next_t += self.interval
            delay = next_t - loop.time()
            if delay < 0:
                log.warning("%s: update took longer than interval (%.0f ms behind)", self.name, -delay * 1000)
                next_t = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    def tag_map(self) -> list[dict]:
        rows = []
        for ts in self.tagsets:
            for tag in ts.names():
                rows.append({"server": self.name, "tag": tag,
                             "endpoint": f"opc.tcp://{self.host}:{self.port}/{self.name}",
                             "namespace": self.namespace,
                             "node_id": f"ns={self.ns_idx};s={ts.spec.name}.{tag}",
                             "data_type": ts.spec.data_type})
        return rows

    async def run(self) -> None:
        server = await self._build()
        async with server:
            self.ready.set()
            log.info("%s: OPC UA listening on opc.tcp://%s:%d/%s (%d tags, %d ms)", self.name, self.host,
                     self.port, self.name, sum(t.spec.count for t in self.tagsets), int(self.interval * 1000))
            await self._update_loop(server)
