"""Typed model of a parsed capture."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


class EventKind(enum.StrEnum):
    MARKER = "marker"
    DRAW = "draw"
    DISPATCH = "dispatch"
    DISPATCH_RAYS = "dispatch_rays"
    EXECUTE_INDIRECT = "execute_indirect"
    COPY = "copy"
    CLEAR = "clear"
    BARRIER = "barrier"
    STATE = "state"
    QUERY = "query"
    SYNC = "sync"
    RAYTRACING = "raytracing"
    OTHER = "other"


DRAW_KINDS = (
    EventKind.DRAW,
    EventKind.DISPATCH,
    EventKind.DISPATCH_RAYS,
    EventKind.EXECUTE_INDIRECT,
)


class ShaderStage(enum.StrEnum):
    VS = "VS"
    PS = "PS"
    CS = "CS"
    GS = "GS"
    HS = "HS"
    DS = "DS"
    AS = "AS"
    MS = "MS"
    LIB = "LIB"


class ResourceKind(enum.StrEnum):
    BUFFER = "buffer"
    TEXTURE1D = "texture1d"
    TEXTURE2D = "texture2d"
    TEXTURE3D = "texture3d"
    UNKNOWN = "unknown"


class ViewKind(enum.StrEnum):
    SRV = "SRV"
    UAV = "UAV"
    CBV = "CBV"
    RTV = "RTV"
    DSV = "DSV"
    SAMPLER = "SAMPLER"
    VBV = "VBV"
    IBV = "IBV"


class RootParameterKind(enum.StrEnum):
    DESCRIPTOR_TABLE = "descriptor_table"
    CBV = "root_cbv"
    SRV = "root_srv"
    UAV = "root_uav"
    CONSTANTS = "root_constants"


# --------------------------------------------------------------------------
@dataclass(slots=True)
class Resource:
    api_id: int
    kind: ResourceKind = ResourceKind.UNKNOWN
    width: int = 0
    height: int = 0
    depth_or_array_size: int = 1
    mip_levels: int = 1
    format: str = "DXGI_FORMAT_UNKNOWN"
    sample_count: int = 1
    flags: str = ""
    initial_state: str = ""
    heap_id: Optional[int] = None
    heap_offset: int = 0
    name: Optional[str] = None
    is_render_target: bool = False
    is_depth_stencil: bool = False
    is_uav: bool = False
    source_file: str = ""
    source_line: int = 0
    data_blob_index: Optional[int] = None

    @property
    def is_buffer(self) -> bool:
        return self.kind is ResourceKind.BUFFER

    @property
    def is_texture(self) -> bool:
        return self.kind in (
            ResourceKind.TEXTURE1D,
            ResourceKind.TEXTURE2D,
            ResourceKind.TEXTURE3D,
        )

    @property
    def size_bytes(self) -> int:
        if self.is_buffer:
            return self.width
        return estimate_texture_bytes(self)

    @property
    def pixel_count(self) -> int:
        if self.is_buffer:
            return 0
        return self.width * max(self.height, 1) * max(self.depth_or_array_size, 1)

    def describe(self) -> str:
        if self.is_buffer:
            return f"Buffer#{self.api_id} {self.width} bytes"
        suffix = (
            f"x{self.depth_or_array_size}" if self.depth_or_array_size > 1 else ""
        )
        return (
            f"{self.kind.value}#{self.api_id} {self.width}x{self.height}{suffix} "
            f"{self.format} mips={self.mip_levels}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.api_id,
            "kind": self.kind.value,
            "width": self.width,
            "height": self.height,
            "depth_or_array_size": self.depth_or_array_size,
            "mip_levels": self.mip_levels,
            "format": self.format,
            "sample_count": self.sample_count,
            "size_bytes": self.size_bytes,
            "is_render_target": self.is_render_target,
            "is_depth_stencil": self.is_depth_stencil,
            "is_uav": self.is_uav,
            "heap_id": self.heap_id,
            "heap_offset": self.heap_offset,
            "initial_state": self.initial_state,
            "flags": self.flags,
            "source": f"{self.source_file}:{self.source_line}" if self.source_file else "",
            "description": self.describe(),
        }


FORMAT_BITS: dict[str, int] = {
    "R32G32B32A32": 128,
    "R32G32B32": 96,
    "R16G16B16A16": 64,
    "R32G32": 64,
    "R10G10B10A2": 32,
    "R11G11B10": 32,
    "R8G8B8A8": 32,
    "B8G8R8A8": 32,
    "B8G8R8X8": 32,
    "R16G16": 32,
    "R32": 32,
    "D32": 32,
    "R24G8": 32,
    "D24": 32,
    "R32G8X24": 64,
    "D32_FLOAT_S8X24": 64,
    "R8G8": 16,
    "R16": 16,
    "D16": 16,
    "B5G6R5": 16,
    "B5G5R5A1": 16,
    "R8": 8,
    "A8": 8,
    "BC1": 4,
    "BC4": 4,
    "BC2": 8,
    "BC3": 8,
    "BC5": 8,
    "BC6H": 8,
    "BC7": 8,
}


def format_bits_per_pixel(fmt: str) -> int:
    name = fmt.replace("DXGI_FORMAT_", "").upper()
    for key, bits in sorted(FORMAT_BITS.items(), key=lambda kv: -len(kv[0])):
        if name.startswith(key):
            return bits
    return 32


def estimate_texture_bytes(resource: "Resource") -> int:
    if resource.is_buffer:
        return resource.width
    bits = format_bits_per_pixel(resource.format)
    total = 0
    width = max(resource.width, 1)
    height = max(resource.height, 1)
    slices = max(resource.depth_or_array_size, 1)
    for _ in range(max(resource.mip_levels, 1)):
        total += (width * height * bits) // 8
        width = max(width // 2, 1)
        height = max(height // 2, 1)
    return total * slices * max(resource.sample_count, 1)


# --------------------------------------------------------------------------
@dataclass(slots=True)
class View:
    kind: ViewKind
    heap_id: Optional[int] = None
    heap_index: Optional[int] = None
    resource_id: Optional[int] = None
    format: str = ""
    dimension: str = ""
    detail: str = ""
    gpu_va: Optional[int] = None
    va_resource_id: Optional[int] = None
    va_offset: int = 0
    size_bytes: int = 0
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_kind": self.kind.value,
            "heap_id": self.heap_id,
            "heap_index": self.heap_index,
            "resource_id": self.resource_id,
            "format": self.format,
            "dimension": self.dimension,
            "source": f"{self.source_file}:{self.source_line}" if self.source_file else "",
        }


@dataclass(slots=True)
class BindingSlot:
    root_index: int
    kind: RootParameterKind
    view_kind: Optional[ViewKind] = None
    heap_id: Optional[int] = None
    heap_index: Optional[int] = None
    resource_id: Optional[int] = None
    gpu_va: Optional[int] = None
    va_offset: int = 0
    num_constants: int = 0
    constants: tuple[int, ...] = ()
    resolved_views: list[View] = field(default_factory=list)
    source_line: int = 0
    table_confidence: str = ""

    def to_dict(self, *, max_views: int | None = None) -> dict[str, Any]:
        views = self.resolved_views if max_views is None else self.resolved_views[:max_views]
        payload: dict[str, Any] = {
            "root_index": self.root_index,
            "binding_kind": self.kind.value,
            "heap_id": self.heap_id,
            "heap_index": self.heap_index,
            "resource_id": self.resource_id,
            "views": [v.to_dict() for v in views],
            "view_count": len(self.resolved_views),
        }
        if self.table_confidence:
            payload["table_confidence"] = self.table_confidence
        if self.num_constants:
            payload["num_constants"] = self.num_constants
        return payload


@dataclass(slots=True)
class RootParameter:
    index: int
    kind: RootParameterKind
    shader_register: int = 0
    register_space: int = 0
    num_descriptors: int = 0
    ranges: list[dict[str, Any]] = field(default_factory=list)
    visibility: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_index": self.index,
            "kind": self.kind.value,
            "shader_register": self.shader_register,
            "register_space": self.register_space,
            "num_descriptors": self.num_descriptors,
            "visibility": self.visibility,
            "ranges": self.ranges,
        }


# --------------------------------------------------------------------------
@dataclass(slots=True)
class Shader:
    stage: ShaderStage
    pso_id: int
    byte_size: int
    hash_md5: str = ""
    blob_index: Optional[int] = None
    blob_stage_offset: int = 0
    _shader_hash: str = ""
    _debug_name: str = ""
    _chunks: tuple[str, ...] = ()
    _capture: Any = field(default=None, repr=False)
    _bytecode: Optional[bytes] = field(default=None, repr=False)
    _disasm: Optional[str] = field(default=None, repr=False)
    _meta_loaded: bool = field(default=False, repr=False)

    # -- lazy metadata --------------------------------------------------
    def _ensure_meta(self) -> None:
        if self._meta_loaded:
            return
        self._meta_loaded = True
        if self._capture is not None:
            self._capture._ensure_shader_meta(self)

    @property
    def shader_hash(self) -> str:
        self._ensure_meta()
        return self._shader_hash

    @property
    def debug_name(self) -> str:
        self._ensure_meta()
        return self._debug_name

    @property
    def chunks(self) -> tuple[str, ...]:
        self._ensure_meta()
        return self._chunks

    @property
    def key(self) -> str:
        return f"{self.stage.value}:{self.shader_hash or self.hash_md5}"

    # -- lazy heavy data ------------------------------------------------
    @property
    def bytecode(self) -> bytes:
        if self._bytecode is None and self._capture is not None:
            self._bytecode = self._capture._load_shader_bytecode(self)
        return self._bytecode or b""

    @property
    def disassembly(self) -> str:
        if self._disasm is None and self._capture is not None:
            self._disasm = self._capture._disassemble(self)
        return self._disasm or ""

    @property
    def embedded_source(self) -> str:
        if self._capture is None:
            return ""
        return self._capture._embedded_source(self)

    @property
    def has_embedded_source(self) -> bool:
        return bool(self.embedded_source)

    @property
    def source(self) -> str:
        embedded = self.embedded_source
        return embedded if embedded else self.disassembly

    @property
    def input_signature(self) -> list[Any]:
        if self._capture is None:
            return []
        return self._capture._signature(self, "ISG1")

    @property
    def output_signature(self) -> list[Any]:
        if self._capture is None:
            return []
        return self._capture._signature(self, "OSG1")

    @property
    def resource_bindings(self) -> list[dict[str, Any]]:
        if self._capture is None:
            return []
        return self._capture._shader_bindings(self)

    @property
    def metadata(self) -> dict[str, Any]:
        if self._capture is None:
            return {}
        return self._capture._shader_metadata(self)

    @property
    def constant_buffers(self) -> list[dict[str, Any]]:
        if self._capture is None:
            return []
        return self._capture._shader_constant_buffers(self)

    @property
    def num_threads(self) -> list[int] | None:
        return self.metadata.get("num_threads")

    @property
    def entry_point(self) -> str:
        """HLSL entry function name, e.g. RayTracingBuildLightGridCS.

        This is the most useful handle when the capture has no embedded source:
        the name is unique enough to locate the original .usf in the engine tree.
        """
        cached = self.metadata.get("entry_point")
        if cached:
            return str(cached)
        for line in (self.disassembly or "").splitlines():
            if "EntryFunctionName:" in line:
                name = line.split("EntryFunctionName:", 1)[1].strip().rstrip(";").strip()
                if name:
                    self.metadata["entry_point"] = name
                    return name
            if line.startswith("define ") and "@" in line:
                candidate = line.split("@", 1)[1].split("(", 1)[0].strip()
                if candidate and not candidate.startswith("dx."):
                    self.metadata["entry_point"] = candidate
                    return candidate
        return ""

    def to_dict(self, *, detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage.value,
            "pso_id": self.pso_id,
            "byte_size": self.byte_size,
            "shader_hash": self.shader_hash,
            "debug_name": self.debug_name,
            "key": self.key,
        }
        if detail:
            payload["chunks"] = list(self.chunks)
            payload["metadata"] = self.metadata
            payload["has_embedded_source"] = self.has_embedded_source
            payload["entry_point"] = self.entry_point
        return payload


@dataclass(slots=True)
class PipelineState:
    api_id: int
    kind: str = "graphics"
    root_signature_id: Optional[int] = None
    shaders: list[Shader] = field(default_factory=list)
    rtv_formats: list[str] = field(default_factory=list)
    dsv_format: str = ""
    primitive_topology_type: str = ""
    input_layout: list[dict[str, Any]] = field(default_factory=list)
    blend_enabled: bool = False
    depth_enabled: bool = False
    depth_write: bool = False
    depth_func: str = ""
    cull_mode: str = ""
    fill_mode: str = ""
    sample_count: int = 1
    sample_mask: int = 0
    blend_states: list[dict[str, Any]] = field(default_factory=list)
    blob_index: Optional[int] = None
    source_file: str = ""
    source_line: int = 0

    def shader(self, stage: ShaderStage | str) -> Optional[Shader]:
        want = ShaderStage(stage) if isinstance(stage, str) else stage
        return next((s for s in self.shaders if s.stage is want), None)

    @property
    def is_compute(self) -> bool:
        return self.kind == "compute" or any(s.stage is ShaderStage.CS for s in self.shaders)

    def to_dict(self, *, detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pso_id": self.api_id,
            "kind": "compute" if self.is_compute else self.kind,
            "root_signature_id": self.root_signature_id,
            "stages": [s.stage.value for s in self.shaders],
            "rtv_formats": self.rtv_formats,
            "dsv_format": self.dsv_format,
            "primitive_topology_type": self.primitive_topology_type,
            "sample_count": self.sample_count,
        }
        if detail:
            payload.update(
                {
                    "input_layout": self.input_layout,
                    "blend_enabled": self.blend_enabled,
                    "blend_states": self.blend_states,
                    "depth_enabled": self.depth_enabled,
                    "depth_write": self.depth_write,
                    "depth_func": self.depth_func,
                    "cull_mode": self.cull_mode,
                    "fill_mode": self.fill_mode,
                    "sample_mask": self.sample_mask,
                    "shaders": [s.to_dict() for s in self.shaders],
                    "source": f"{self.source_file}:{self.source_line}",
                }
            )
        return payload


# --------------------------------------------------------------------------
@dataclass(slots=True)
class Event:
    queue_id: int
    name: str
    global_id: Optional[int] = None
    parent_queue_id: int = -1
    kind: EventKind = EventKind.OTHER
    depth: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    children: list["Event"] = field(default_factory=list, repr=False)
    parent: Optional["Event"] = field(default=None, repr=False)
    _capture: Any = field(default=None, repr=False)

    @property
    def path(self) -> str:
        parts: list[str] = []
        node: Optional[Event] = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))

    @property
    def marker_path(self) -> list[str]:
        out: list[str] = []
        node = self.parent
        while node is not None:
            if node.kind is EventKind.MARKER:
                out.append(node.name)
            node = node.parent
        return list(reversed(out))

    def walk(self) -> Iterator["Event"]:
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def is_draw(self) -> bool:
        return self.kind in DRAW_KINDS

    @property
    def draw_call(self) -> Optional["DrawCall"]:
        if self._capture is None or self.global_id is None:
            return None
        return self._capture.draw_call_by_global_id(self.global_id)

    def to_dict(self, *, detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "queue_id": self.queue_id,
            "global_id": self.global_id,
            "name": self.name,
            "kind": self.kind.value,
            "depth": self.depth,
            "parent_queue_id": self.parent_queue_id,
            "is_draw": self.is_draw,
        }
        if detail:
            payload["path"] = self.path
            payload["marker_path"] = self.marker_path
            payload["child_count"] = len(self.children)
            if self.counters:
                payload["counters"] = self.counters
        return payload


@dataclass(slots=True)
class VertexBufferBinding:
    slot: int
    resource_id: Optional[int]
    offset: int = 0
    size_bytes: int = 0
    stride: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "resource_id": self.resource_id,
            "offset": self.offset,
            "size_bytes": self.size_bytes,
            "stride": self.stride,
            "vertex_count": self.size_bytes // self.stride if self.stride else 0,
        }


@dataclass(slots=True)
class IndexBufferBinding:
    resource_id: Optional[int]
    offset: int = 0
    size_bytes: int = 0
    format: str = ""

    @property
    def index_stride(self) -> int:
        return 2 if "R16" in self.format else 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "offset": self.offset,
            "size_bytes": self.size_bytes,
            "format": self.format,
            "index_count": self.size_bytes // self.index_stride if self.size_bytes else 0,
        }


@dataclass(slots=True)
class DrawCall:
    index: int
    kind: EventKind
    api: str
    command_list_id: int
    global_id: Optional[int] = None
    marker_path: tuple[str, ...] = ()
    source_file: str = ""
    source_line: int = 0

    vertex_or_index_count: int = 0
    instance_count: int = 0
    start_index: int = 0
    base_vertex: int = 0
    start_instance: int = 0
    thread_group_x: int = 0
    thread_group_y: int = 0
    thread_group_z: int = 0

    pso_id: Optional[int] = None
    root_signature_id: Optional[int] = None
    primitive_topology: str = ""

    bindings: list[BindingSlot] = field(default_factory=list)
    descriptor_heap_ids: list[int] = field(default_factory=list)
    vertex_buffers: list[VertexBufferBinding] = field(default_factory=list)
    index_buffer: Optional[IndexBufferBinding] = None
    render_target_resource_ids: list[int] = field(default_factory=list)
    depth_stencil_resource_id: Optional[int] = None
    viewports: list[dict[str, Any]] = field(default_factory=list)
    scissor_rects: list[dict[str, Any]] = field(default_factory=list)
    indirect_argument_buffer: Optional[str] = None

    _capture: Any = field(default=None, repr=False)

    # -- links ----------------------------------------------------------
    @property
    def pipeline_state(self) -> Optional[PipelineState]:
        if self._capture is None or self.pso_id is None:
            return None
        return self._capture.pipeline_states.get(self.pso_id)

    @property
    def shaders(self) -> list[Shader]:
        pso = self.pipeline_state
        return list(pso.shaders) if pso else []

    def shader(self, stage: ShaderStage | str) -> Optional[Shader]:
        pso = self.pipeline_state
        return pso.shader(stage) if pso else None

    @property
    def event(self) -> Optional[Event]:
        if self._capture is None or self.global_id is None:
            return None
        return self._capture.event_by_global_id(self.global_id)

    @property
    def pass_name(self) -> str:
        return self.marker_path[-1] if self.marker_path else ""

    @property
    def marker(self) -> str:
        return " / ".join(self.marker_path)

    # -- views ----------------------------------------------------------
    def views(self, kind: ViewKind | str | None = None) -> list[View]:
        want = ViewKind(kind) if isinstance(kind, str) else kind
        out: list[View] = []
        for binding in self.bindings:
            for view in binding.resolved_views:
                if want is None or view.kind is want:
                    out.append(view)
        return out

    @property
    def srvs(self) -> list[View]:
        return self.views(ViewKind.SRV)

    @property
    def uavs(self) -> list[View]:
        return self.views(ViewKind.UAV)

    @property
    def samplers(self) -> list[View]:
        return self.views(ViewKind.SAMPLER)

    @property
    def cbvs(self) -> list[View]:
        out = self.views(ViewKind.CBV)
        for binding in self.bindings:
            if binding.kind is RootParameterKind.CBV and not binding.resolved_views:
                out.append(
                    View(
                        kind=ViewKind.CBV,
                        resource_id=binding.resource_id,
                        va_resource_id=binding.resource_id,
                        va_offset=binding.va_offset,
                        source_line=binding.source_line,
                    )
                )
        return out

    def resources(self, kind: ViewKind | str | None = None) -> list[Resource]:
        if self._capture is None:
            return []
        ids: list[int] = []
        for view in self.views(kind):
            rid = view.resource_id if view.resource_id is not None else view.va_resource_id
            if rid is not None:
                ids.append(rid)
        if kind is None:
            ids += self.render_target_resource_ids
            if self.depth_stencil_resource_id is not None:
                ids.append(self.depth_stencil_resource_id)
            for vertex in self.vertex_buffers:
                if vertex.resource_id is not None:
                    ids.append(vertex.resource_id)
            if self.index_buffer and self.index_buffer.resource_id is not None:
                ids.append(self.index_buffer.resource_id)
            for binding in self.bindings:
                if binding.resource_id is not None:
                    ids.append(binding.resource_id)
        seen: dict[int, Resource] = {}
        for rid in ids:
            resource = self._capture.resources.get(rid)
            if resource is not None:
                seen[rid] = resource
        return list(seen.values())

    @property
    def buffers(self) -> list[Resource]:
        return [r for r in self.resources() if r.is_buffer]

    @property
    def textures(self) -> list[Resource]:
        return [r for r in self.resources() if r.is_texture]

    @property
    def render_targets(self) -> list[Resource]:
        if self._capture is None:
            return []
        return [
            self._capture.resources[rid]
            for rid in self.render_target_resource_ids
            if rid in self._capture.resources
        ]

    @property
    def depth_stencil(self) -> Optional[Resource]:
        if self._capture is None or self.depth_stencil_resource_id is None:
            return None
        return self._capture.resources.get(self.depth_stencil_resource_id)

    # -- derived metrics ------------------------------------------------
    @property
    def triangle_count(self) -> int:
        if self.kind is not EventKind.DRAW:
            return 0
        topology = self.primitive_topology.upper()
        count = self.vertex_or_index_count * max(self.instance_count, 1)
        if "TRIANGLELIST" in topology or not topology:
            return count // 3
        if "TRIANGLESTRIP" in topology:
            return max(count - 2, 0)
        return 0

    @property
    def thread_count(self) -> int:
        if self.kind not in (EventKind.DISPATCH, EventKind.DISPATCH_RAYS):
            return 0
        groups = self.thread_group_x * self.thread_group_y * self.thread_group_z
        shader = self.shader(ShaderStage.CS)
        threads = shader.num_threads if shader else None
        per_group = (threads[0] * threads[1] * threads[2]) if threads else 1
        return groups * per_group

    def summary(self) -> str:
        if self.kind in (EventKind.DISPATCH, EventKind.DISPATCH_RAYS):
            args = f"groups=({self.thread_group_x},{self.thread_group_y},{self.thread_group_z})"
        else:
            args = f"count={self.vertex_or_index_count} inst={self.instance_count}"
        stages = "+".join(s.stage.value for s in self.shaders) or "?"
        return (
            f"#{self.index} {self.api} {args} pso={self.pso_id} [{stages}] "
            f"rt={len(self.render_target_resource_ids)} srv={len(self.srvs)} "
            f"uav={len(self.uavs)} cbv={len(self.cbvs)} | {self.pass_name}"
        )

    def to_dict(self, *, detail: bool = False, max_views: int | None = 8) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "draw_index": self.index,
            "kind": self.kind.value,
            "api": self.api,
            "global_id": self.global_id,
            "command_list_id": self.command_list_id,
            "pass_name": self.pass_name,
            "pso_id": self.pso_id,
            "root_signature_id": self.root_signature_id,
        }
        if self.kind in (EventKind.DISPATCH, EventKind.DISPATCH_RAYS):
            payload["thread_groups"] = [
                self.thread_group_x,
                self.thread_group_y,
                self.thread_group_z,
            ]
            payload["thread_count"] = self.thread_count
        else:
            payload["vertex_or_index_count"] = self.vertex_or_index_count
            payload["instance_count"] = self.instance_count
            payload["triangle_count"] = self.triangle_count
        payload["counts"] = {
            "render_targets": len(self.render_target_resource_ids),
            "srv": len(self.srvs),
            "uav": len(self.uavs),
            "cbv": len(self.cbvs),
            "sampler": len(self.samplers),
            "vertex_buffers": len(self.vertex_buffers),
        }
        if detail:
            payload.update(
                {
                    "marker_path": list(self.marker_path),
                    "primitive_topology": self.primitive_topology,
                    "start_index": self.start_index,
                    "base_vertex": self.base_vertex,
                    "start_instance": self.start_instance,
                    "shaders": [s.to_dict() for s in self.shaders],
                    "render_targets": [r.to_dict() for r in self.render_targets],
                    "depth_stencil": (
                        self.depth_stencil.to_dict() if self.depth_stencil else None
                    ),
                    "vertex_buffers": [v.to_dict() for v in self.vertex_buffers],
                    "index_buffer": (
                        self.index_buffer.to_dict() if self.index_buffer else None
                    ),
                    "bindings": [b.to_dict(max_views=max_views) for b in self.bindings],
                    "descriptor_heap_ids": self.descriptor_heap_ids,
                    "viewports": self.viewports,
                    "scissor_rects": self.scissor_rects,
                    "indirect_argument_buffer": self.indirect_argument_buffer,
                    "source": f"{self.source_file}:{self.source_line}",
                }
            )
        return payload
