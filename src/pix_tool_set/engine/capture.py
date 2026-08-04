"""The capture engine: lazily parses one export and answers queries."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..errors import PixToolError, export_incomplete
from ..pixtool import PixTool, validate_export
from . import cppparse, eventlist
from .dxbc import (
    DxbcContainer,
    ShaderDisassembler,
    parse_constant_buffers,
    parse_resource_bindings,
    parse_shader_metadata,
    parse_signature,
    scrape_embedded_hlsl,
    split_packed_shaders,
)
from .model import (
    DRAW_KINDS,
    DrawCall,
    Event,
    EventKind,
    PipelineState,
    Resource,
    ResourceKind,
    RootParameterKind,
    Shader,
    ShaderStage,
    View,
    ViewKind,
)


class Capture:
    """A parsed PIX capture backed by a pixtool C++ export."""

    def __init__(
        self,
        capture_path: Path | None,
        export_dir: Path,
        event_csv: Path | None = None,
        pixtool: PixTool | None = None,
    ) -> None:
        self.capture_path = Path(capture_path) if capture_path else None
        self.export_dir = Path(export_dir)
        self.event_csv = Path(event_csv) if event_csv else None
        self._pixtool = pixtool
        self._disassembler = ShaderDisassembler(
            pixtool.install_dir if pixtool is not None else None
        )
        missing = validate_export(self.export_dir)
        if missing:
            raise export_incomplete(str(self.export_dir), missing)

    @property
    def pixtool(self) -> PixTool:
        if self._pixtool is None:
            self._pixtool = PixTool.locate()
        return self._pixtool

    # ==================================================================
    # parsed layers
    # ==================================================================
    @cached_property
    def resources(self) -> dict[int, Resource]:
        return cppparse.parse_resources(self.export_dir)

    @cached_property
    def views(self) -> dict[tuple[int, int], View]:
        # Only CommandLists_*.cpp is passed in here. Descriptors_*.cpp and
        # ModifyDescriptors_*.cpp are collected inside parse_descriptors, which
        # owns their relative order -- ModifyDescriptors must be applied after
        # the Descriptors filler, and the inline Create*View calls in the command
        # lists after both. Numeric ordering (via sorted_group rather than a bare
        # glob) keeps the last-write-wins result reproducible across platforms.
        extra = [
            path
            for path in cppparse.sorted_group(self.export_dir, "CommandLists")
            if path.exists()
        ]
        return cppparse.parse_descriptors(self.export_dir, extra)

    @cached_property
    def _pso_parse(self) -> cppparse.PsoParseResult:
        return cppparse.parse_pipeline_states(self.export_dir)

    @cached_property
    def pipeline_states(self) -> dict[int, PipelineState]:
        states = self._pso_parse.pipeline_states
        for pso in states.values():
            for shader in pso.shaders:
                shader._capture = self
        return states

    @cached_property
    def root_signatures(self) -> dict[int, cppparse.RootSignature]:
        return cppparse.parse_root_signatures(self.export_dir)

    @cached_property
    def shaders(self) -> list[Shader]:
        out: list[Shader] = []
        for pso in self.pipeline_states.values():
            out.extend(pso.shaders)
        return out

    @cached_property
    def draw_calls(self) -> list[DrawCall]:
        parser = cppparse.CommandListParser(
            self.export_dir, self.views, self.root_signatures
        )
        calls = parser.parse()
        for call in calls:
            call._capture = self
        self._reconcile_marker_paths(calls)
        return calls

    def _reconcile_marker_paths(self, calls: list[DrawCall]) -> None:
        """Replace C++-derived marker paths with the event list's authoritative ones.

        The C++ export interleaves command lists, so a single PIX BeginEvent/EndEvent
        stack tracked while streaming the files drifts: markers that were closed on
        one command list stay on the stack while another list is being replayed. That
        produced paths with repeated segments (`Frame N / ... / Frame N / ...`).

        The exported event list carries an explicit parent link per event, so its
        hierarchy is correct by construction. Where a draw's Global ID is present in
        that list we take its path verbatim; otherwise the parsed path is kept.
        """
        if self.event_csv is None or not self.event_csv.exists():
            return
        by_global = self._events_by_global_id
        if not by_global:
            return
        for call in calls:
            if call.global_id is None:
                continue
            event = by_global.get(call.global_id)
            if event is None:
                continue
            authoritative = tuple(event.marker_path)
            if authoritative and authoritative != call.marker_path:
                call.marker_path = authoritative

    @cached_property
    def events(self) -> list[Event]:
        if self.event_csv is None or not self.event_csv.exists():
            return []
        parsed = eventlist.parse_event_list(self.event_csv)
        for event in parsed:
            event._capture = self
        return parsed

    @cached_property
    def event_roots(self) -> list[Event]:
        return eventlist.roots(self.events)

    @cached_property
    def timing(self):
        """Measured GPU durations, when the timing event list has been exported.

        Returns None when absent; callers must degrade to the estimate rather
        than fabricate numbers. Produce it with the `export-timing` tool.
        """
        from . import timing as timing_mod

        if self.event_csv is None:
            return None
        path = timing_mod.timing_csv_path(self.event_csv.parent, self.capture_path.stem)
        if not path.exists():
            return None
        try:
            table = timing_mod.TimingTable(path)
        except OSError:
            return None
        return table if table.measured_count else None

    @cached_property
    def _resource_reads(self):
        """Every resources.bin Read() call, numbered in stream order."""
        return cppparse.collect_resource_reads(self.export_dir)

    @cached_property
    def _resource_stream(self):
        from .xpress import ResourceStream

        stream = ResourceStream(self.export_dir / "resources.bin")
        reads = self._resource_reads
        if reads:
            stream.build_index([read.compressed_size for read in reads])
        else:
            # Fall back to the PSO-only sizes, which is all earlier versions had.
            stream.build_index(self._pso_parse.read_sizes)
        return stream

    @cached_property
    def _resource_blob_index(self) -> dict[int, int]:
        """resource id -> index of the blob holding its initial contents.

        A resource may be filled by several Read() calls (one per subresource);
        the first is what callers want for buffers, and texture mips are handled
        by the texture tools.
        """
        out: dict[int, int] = {}
        for read in self._resource_reads:
            if read.owner_kind != "resource" or read.owner_id is None:
                continue
            out.setdefault(read.owner_id, read.index)
        return out

    def resource_blob_indices(self, resource_id: int) -> list[int]:
        """Every blob index that feeds one resource, in order."""
        return [
            read.index
            for read in self._resource_reads
            if read.owner_kind == "resource" and read.owner_id == resource_id
        ]

    @cached_property
    def _modification_plan(self):
        """Per-frame CPU page writes recorded for mapped resources."""
        from . import modifications

        symbol_to_blob = {
            read.size_symbol: read.index
            for read in self._resource_reads
            if read.owner_kind == "modification" and read.size_symbol
        }
        if not symbol_to_blob:
            return None
        return modifications.build_plan(self.export_dir, symbol_to_blob)

    def read_resource_bytes(
        self,
        resource_id: int,
        *,
        offset: int = 0,
        length: int | None = None,
        apply_modifications: bool = True,
    ) -> bytes:
        """Return the contents of a resource as the frame's shaders saw them.

        The initial upload is only half the story: UE5 rewrites its big upload
        buffers from the CPU during the frame, and PIX records those writes
        separately. Without replaying them a cbuffer reads back stale bytes, so
        they are applied by default.

        Raises PixToolError when the export holds no data at all for a resource,
        so a caller can report "not captured" rather than return zeros.
        """
        index = self._resource_blob_index.get(resource_id)
        plan = self._modification_plan if apply_modifications else None
        writes = plan.for_resource(resource_id) if plan is not None else []

        if index is None and not writes:
            raise PixToolError(
                code="resource_data_unavailable",
                message=f"No captured contents for resource {resource_id}.",
                stage="resources.bin",
                suggestion=(
                    "PIX only stores data it saw uploaded or written from the CPU. "
                    "Buffers produced entirely on the GPU have no recorded bytes."
                ),
            )

        if index is not None:
            blob = bytearray(self._load_blob(index))
        else:
            resource = self.resources.get(resource_id)
            blob = bytearray(resource.size_bytes if resource else 0)

        # Later writes win, matching the order the frame performed them. A patch
        # blob whose stream position is still unresolved is skipped rather than
        # failing the whole read, so callers keep the initial contents plus
        # whatever patches did decode.
        applied = 0
        skipped = 0
        for write in writes:
            try:
                patch = self._load_blob(write.blob_index)
            except PixToolError:
                skipped += 1
                continue
            chunk = patch[write.blob_offset : write.blob_offset + write.size]
            start = write.resource_offset
            if start + len(chunk) > len(blob):
                blob.extend(bytes(start + len(chunk) - len(blob)))
            blob[start : start + len(chunk)] = chunk
            applied += 1
        self._last_patch_stats = {"applied": applied, "skipped": skipped}

        out = bytes(blob)
        if offset:
            out = out[offset:]
        if length is not None:
            out = out[:length]
        return out

    @cached_property
    def _footprints(self):
        """resource id -> subresource footprints recorded for its upload."""
        from . import footprint

        return footprint.parse_footprints(self.export_dir)

    def resource_footprints(self, resource_id: int) -> list:
        """Subresource footprints for a texture upload, empty when not a texture."""
        return list(self._footprints.get(resource_id, []))

    def resource_page_status(self, resource_id: int, page: int) -> dict[str, Any]:
        """Whether a specific page's CPU rewrite could actually be applied.

        A page that the frame rewrote is only trustworthy once the patch blob
        feeding it decodes. Callers use this to decide between "these are the
        values the shader read" and "these are stale pre-frame bytes".
        """
        plan = self._modification_plan
        writes = (
            [w for w in plan.for_resource(resource_id) if w.page == page]
            if plan is not None
            else []
        )
        if not writes:
            return {"rewritten": False, "patched": True, "patch_count": 0}
        applied = 0
        for write in writes:
            try:
                self._load_blob(write.blob_index)
                applied += 1
            except PixToolError:
                pass
        return {
            "rewritten": True,
            "patched": applied == len(writes),
            "patch_count": len(writes),
            "patches_applied": applied,
        }

    def resource_written_pages(self, resource_id: int) -> set[int]:
        """Every page of a resource that the frame rewrote from the CPU."""
        plan = self._modification_plan
        if plan is None:
            return set()
        return {write.page for write in plan.for_resource(resource_id)}

    def resource_data_sources(self, resource_id: int) -> dict[str, Any]:
        """Explain where a resource's bytes come from, for honest reporting."""
        pages = self.resource_written_pages(resource_id)
        return {
            "initial_blob_index": self._resource_blob_index.get(resource_id),
            "cpu_page_writes": len(
                self._modification_plan.for_resource(resource_id)
                if self._modification_plan is not None
                else []
            ),
            "written_page_count": len(pages),
            # Truncated for display only; never use this list for a membership
            # test, call resource_written_pages() instead.
            "written_pages_sample": sorted(pages)[:32],
        }

    # ==================================================================
    # indexes
    # ==================================================================
    @cached_property
    def _events_by_global_id(self) -> dict[int, Event]:
        return {e.global_id: e for e in self.events if e.global_id is not None}

    @cached_property
    def _draws_by_global_id(self) -> dict[int, DrawCall]:
        return {d.global_id: d for d in self.draw_calls if d.global_id is not None}

    @cached_property
    def passes(self) -> list[dict[str, Any]]:
        """Render passes derived from the deepest marker of each draw."""
        buckets: dict[tuple[str, ...], list[DrawCall]] = defaultdict(list)
        for draw in self.draw_calls:
            buckets[draw.marker_path].append(draw)

        entries: list[dict[str, Any]] = []
        for index, (path, draws) in enumerate(
            sorted(buckets.items(), key=lambda kv: kv[1][0].index)
        ):
            render_targets: list[int] = []
            for draw in draws:
                for rid in draw.render_target_resource_ids:
                    if rid not in render_targets:
                        render_targets.append(rid)
            depth_ids = [
                d.depth_stencil_resource_id
                for d in draws
                if d.depth_stencil_resource_id is not None
            ]
            entries.append(
                {
                    "pass_index": index,
                    "name": path[-1] if path else "(root)",
                    "marker_path": list(path),
                    "depth": len(path),
                    "draw_count": sum(1 for d in draws if d.kind is EventKind.DRAW),
                    "dispatch_count": sum(
                        1 for d in draws if d.kind is EventKind.DISPATCH
                    ),
                    "indirect_count": sum(
                        1 for d in draws if d.kind is EventKind.EXECUTE_INDIRECT
                    ),
                    "event_count": len(draws),
                    "first_draw_index": draws[0].index,
                    "last_draw_index": draws[-1].index,
                    "first_global_id": draws[0].global_id,
                    "last_global_id": draws[-1].global_id,
                    "first_queue_id": self._queue_id_for_global(draws[0].global_id),
                    "last_queue_id": self._queue_id_for_global(draws[-1].global_id),
                    "marker_queue_id": self._marker_queue_id(path),
                    "triangle_count": sum(d.triangle_count for d in draws),
                    "thread_count": sum(d.thread_count for d in draws),
                    "render_target_ids": render_targets,
                    "depth_stencil_ids": sorted(set(depth_ids)),
                    "pso_ids": sorted({d.pso_id for d in draws if d.pso_id is not None}),
                }
            )
        return entries

    def _queue_id_for_global(self, global_id: int | None) -> Optional[int]:
        """Translate an action's Global ID into the Queue ID the PIX GUI shows."""
        if global_id is None:
            return None
        event = self.event_by_global_id(global_id)
        return getattr(event, "queue_id", None) if event is not None else None

    def _marker_queue_id(self, path: tuple[str, ...]) -> Optional[int]:
        """Queue ID of the marker event that opens this pass.

        Markers carry no Global ID, so this is the only id that addresses the
        pass row itself rather than one of the draws inside it.
        """
        if not path:
            return None
        cache = getattr(self, "_marker_queue_cache", None)
        if cache is None:
            cache = {}
            for event in self.events:
                if event.is_draw:
                    continue
                key = tuple(event.marker_path) + (event.name,)
                if key not in cache and getattr(event, "queue_id", None) is not None:
                    cache[key] = event.queue_id
            self._marker_queue_cache = cache
        return cache.get(tuple(path))

    @cached_property
    def _pass_by_name(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in self.passes:
            grouped[entry["name"]].append(entry)
        return grouped

    @cached_property
    def resource_usage(self) -> dict[int, dict[str, Any]]:
        """resource id -> how each draw touches it."""
        usage: dict[int, dict[str, Any]] = {}

        def slot(rid: int) -> dict[str, Any]:
            return usage.setdefault(
                rid,
                {
                    "resource_id": rid,
                    "read_draws": [],
                    "write_draws": [],
                    "render_target_draws": [],
                    "depth_draws": [],
                    "vertex_draws": [],
                    "index_draws": [],
                    "constant_draws": [],
                    "passes": [],
                },
            )

        for draw in self.draw_calls:
            for view in draw.views():
                rid = view.resource_id if view.resource_id is not None else view.va_resource_id
                if rid is None:
                    continue
                entry = slot(rid)
                if view.kind is ViewKind.UAV:
                    entry["write_draws"].append(draw.index)
                elif view.kind is ViewKind.SRV:
                    entry["read_draws"].append(draw.index)
                elif view.kind is ViewKind.CBV:
                    entry["constant_draws"].append(draw.index)
                if draw.pass_name and draw.pass_name not in entry["passes"]:
                    entry["passes"].append(draw.pass_name)
            for rid in draw.render_target_resource_ids:
                entry = slot(rid)
                entry["render_target_draws"].append(draw.index)
                entry["write_draws"].append(draw.index)
                if draw.pass_name and draw.pass_name not in entry["passes"]:
                    entry["passes"].append(draw.pass_name)
            if draw.depth_stencil_resource_id is not None:
                entry = slot(draw.depth_stencil_resource_id)
                entry["depth_draws"].append(draw.index)
                entry["write_draws"].append(draw.index)
                if draw.pass_name and draw.pass_name not in entry["passes"]:
                    entry["passes"].append(draw.pass_name)
            for vertex in draw.vertex_buffers:
                if vertex.resource_id is not None:
                    entry = slot(vertex.resource_id)
                    entry["vertex_draws"].append(draw.index)
                    entry["read_draws"].append(draw.index)
            if draw.index_buffer and draw.index_buffer.resource_id is not None:
                entry = slot(draw.index_buffer.resource_id)
                entry["index_draws"].append(draw.index)
                entry["read_draws"].append(draw.index)
            for binding in draw.bindings:
                if binding.resource_id is not None:
                    entry = slot(binding.resource_id)
                    entry["constant_draws"].append(draw.index)

        for entry in usage.values():
            for key in (
                "read_draws",
                "write_draws",
                "render_target_draws",
                "depth_draws",
                "vertex_draws",
                "index_draws",
                "constant_draws",
            ):
                entry[key] = sorted(set(entry[key]))
        return usage

    @cached_property
    def descriptor_coverage(self) -> dict[str, Any]:
        """How much of the frame's descriptor data actually came back.

        ``resource_usage`` is built entirely from the views a draw resolves, so a capture
        whose descriptor writes were not recovered yields empty read/write lists that look
        exactly like "nothing in the frame touches this resource". That ambiguity cost real
        debugging time once: a UAV was reported as consumed by nobody when in truth the
        export's ``ModifyDescriptors_*.cpp`` had simply never been parsed. Publishing the
        coverage lets a caller say "no data" instead of asserting a negative.
        """
        tables = 0
        tables_with_views = 0
        for draw in self.draw_calls:
            for binding in draw.bindings:
                if binding.kind is not RootParameterKind.DESCRIPTOR_TABLE:
                    continue
                tables += 1
                if binding.resolved_views:
                    tables_with_views += 1
        resolved = sum(1 for view in self.views.values() if view.resource_id is not None)
        return {
            "descriptors_parsed": len(self.views),
            "descriptors_with_resource": resolved,
            "descriptor_tables_bound": tables,
            "descriptor_tables_with_views": tables_with_views,
            "tables_empty": tables - tables_with_views,
            "coverage_percent": (
                round(100.0 * tables_with_views / tables, 2) if tables else 0.0
            ),
            "usage_is_complete": tables > 0 and tables_with_views == tables,
            "caveat": (
                "resource_usage is derived from resolved descriptors. Where a table "
                "resolved to no views, an empty read/write list means the data is missing, "
                "not that the frame leaves the resource untouched."
            ),
        }

    # ==================================================================
    # lookups
    # ==================================================================
    def event_by_global_id(self, global_id: int) -> Optional[Event]:
        return self._events_by_global_id.get(global_id)

    def event_by_queue_id(self, queue_id: int) -> Optional[Event]:
        """Look up an event by the Queue ID column of the exported event list.

        Queue ID is present on every row (Global ID only appears on actions), so
        it is the one identifier that can address markers as well as draws.
        """
        cache = getattr(self, "_events_by_queue_id", None)
        if cache is None:
            cache = {
                event.queue_id: event
                for event in self.events
                if getattr(event, "queue_id", None) is not None
            }
            self._events_by_queue_id = cache
        return cache.get(queue_id)

    def resolve_event(
        self, *, global_id: int | None = None, queue_id: int | None = None
    ) -> Optional[Event]:
        if global_id is not None:
            found = self.event_by_global_id(global_id)
            if found is not None:
                return found
        if queue_id is not None:
            return self.event_by_queue_id(queue_id)
        return None

    def find_pass_by_event(
        self, *, global_id: int | None = None, queue_id: int | None = None
    ) -> Optional[dict[str, Any]]:
        """Map any event id onto the pass that contains it.

        Works for a draw/dispatch id and for the enclosing marker id alike,
        because both resolve to the same marker_path.
        """
        event = self.resolve_event(global_id=global_id, queue_id=queue_id)
        if event is None:
            return None
        path = tuple(event.marker_path)
        if not path:
            return None
        for entry in self.passes:
            if tuple(entry["marker_path"]) == path:
                return entry
        # The id may name the marker itself rather than a child action, in which
        # case the pass path is the event path plus the event's own name.
        extended = path + (event.name,)
        for entry in self.passes:
            if tuple(entry["marker_path"]) == extended:
                return entry
        return None

    def draw_call_by_global_id(self, global_id: int) -> Optional[DrawCall]:
        return self._draws_by_global_id.get(global_id)

    def draw_call(self, index: int) -> Optional[DrawCall]:
        if 0 <= index < len(self.draw_calls):
            return self.draw_calls[index]
        return None

    def resolve_draw(
        self,
        *,
        draw_index: int | None = None,
        global_id: int | None = None,
        queue_id: int | None = None,
    ) -> Optional[DrawCall]:
        if global_id is not None:
            found = self.draw_call_by_global_id(global_id)
            if found is not None:
                return found
        if queue_id is not None:
            event = self.event_by_queue_id(queue_id)
            if event is not None and event.global_id is not None:
                found = self.draw_call_by_global_id(event.global_id)
                if found is not None:
                    return found
        if draw_index is not None:
            return self.draw_call(draw_index)
        return None

    def resource(self, resource_id: int) -> Optional[Resource]:
        return self.resources.get(resource_id)

    def pipeline_state(self, pso_id: int) -> Optional[PipelineState]:
        return self.pipeline_states.get(pso_id)

    def view(self, heap_id: int, heap_index: int) -> Optional[View]:
        return self.views.get((heap_id, heap_index))

    def find_pass(self, name_or_index: str | int) -> Optional[dict[str, Any]]:
        if isinstance(name_or_index, int) or str(name_or_index).isdigit():
            index = int(name_or_index)
            for entry in self.passes:
                if entry["pass_index"] == index:
                    return entry
            return None
        exact = self._pass_by_name.get(str(name_or_index))
        if exact:
            return exact[0]
        needle = str(name_or_index).lower()
        for entry in self.passes:
            if needle in entry["name"].lower():
                return entry
        return None

    # ==================================================================
    # queries
    # ==================================================================
    def find_events(
        self,
        pattern: str | None = None,
        *,
        kind: EventKind | str | None = None,
        regex: bool = False,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[list[Event], int]:
        want_kind = EventKind(kind) if isinstance(kind, str) else kind
        compiled = re.compile(pattern, re.I) if (pattern and regex) else None
        needle = pattern.lower() if (pattern and not regex) else None
        matched: list[Event] = []
        for event in self.events:
            if want_kind is not None and event.kind is not want_kind:
                continue
            if compiled is not None and not compiled.search(event.name):
                continue
            if needle is not None and needle not in event.name.lower():
                continue
            matched.append(event)
        total = len(matched)
        window = matched[offset : offset + limit] if limit else matched[offset:]
        return window, total

    def find_draw_calls(
        self,
        *,
        pass_name: str | None = None,
        marker: str | None = None,
        kind: EventKind | str | None = None,
        pso_id: int | None = None,
        uses_resource: int | None = None,
        shader_hash: str | None = None,
        min_instances: int | None = None,
        min_triangles: int | None = None,
        predicate: Callable[[DrawCall], bool] | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[list[DrawCall], int]:
        want_kind = EventKind(kind) if isinstance(kind, str) else kind
        pass_needle = pass_name.lower() if pass_name else None
        marker_needle = marker.lower() if marker else None
        matched: list[DrawCall] = []
        for draw in self.draw_calls:
            if want_kind is not None and draw.kind is not want_kind:
                continue
            if pso_id is not None and draw.pso_id != pso_id:
                continue
            if pass_needle is not None and pass_needle not in draw.pass_name.lower():
                continue
            if marker_needle is not None and marker_needle not in draw.marker.lower():
                continue
            if min_instances is not None and draw.instance_count < min_instances:
                continue
            if min_triangles is not None and draw.triangle_count < min_triangles:
                continue
            if shader_hash is not None:
                needle = shader_hash.lower()
                if not any(
                    needle in (s.shader_hash or "").lower()
                    or needle in (s.debug_name or "").lower()
                    for s in draw.shaders
                ):
                    continue
            if uses_resource is not None:
                if not any(r.api_id == uses_resource for r in draw.resources()):
                    continue
            if predicate is not None and not predicate(draw):
                continue
            matched.append(draw)
        total = len(matched)
        window = matched[offset : offset + limit] if limit else matched[offset:]
        return window, total

    def find_resources(
        self,
        *,
        kind: str | None = None,
        min_width: int = 0,
        min_size_bytes: int = 0,
        format_filter: str | None = None,
        render_target: bool | None = None,
        depth_stencil: bool | None = None,
        uav: bool | None = None,
        used_only: bool = False,
        predicate: Callable[[Resource], bool] | None = None,
        offset: int = 0,
        limit: int | None = None,
        sort_by: str = "size",
    ) -> tuple[list[Resource], int]:
        usage = self.resource_usage if used_only else {}
        matched: list[Resource] = []
        for resource in self.resources.values():
            if kind is not None and resource.kind.value != kind:
                continue
            if resource.width < min_width:
                continue
            if min_size_bytes and resource.size_bytes < min_size_bytes:
                continue
            if format_filter and format_filter.upper() not in resource.format.upper():
                continue
            if render_target is not None and resource.is_render_target != render_target:
                continue
            if depth_stencil is not None and resource.is_depth_stencil != depth_stencil:
                continue
            if uav is not None and resource.is_uav != uav:
                continue
            if used_only and resource.api_id not in usage:
                continue
            if predicate is not None and not predicate(resource):
                continue
            matched.append(resource)

        keys: dict[str, Callable[[Resource], Any]] = {
            "size": lambda r: -r.size_bytes,
            "pixels": lambda r: -r.pixel_count,
            "id": lambda r: r.api_id,
            "width": lambda r: -r.width,
        }
        matched.sort(key=keys.get(sort_by, keys["size"]))
        total = len(matched)
        window = matched[offset : offset + limit] if limit else matched[offset:]
        return window, total

    def find_shaders(
        self,
        *,
        stage: ShaderStage | str | None = None,
        name: str | None = None,
        used_only: bool = False,
        unique: bool = False,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[list[Shader], int]:
        want = ShaderStage(stage) if isinstance(stage, str) else stage
        pool: Iterable[Shader]
        if used_only:
            used = {d.pso_id for d in self.draw_calls}
            pool = [s for s in self.shaders if s.pso_id in used]
        else:
            pool = self.shaders

        matched: list[Shader] = []
        seen: set[str] = set()
        for shader in pool:
            if want is not None and shader.stage is not want:
                continue
            if name:
                needle = name.lower()
                haystack = f"{shader.debug_name} {shader.shader_hash}".lower()
                if needle not in haystack:
                    continue
            if unique:
                if shader.key in seen:
                    continue
                seen.add(shader.key)
            matched.append(shader)
        total = len(matched)
        window = matched[offset : offset + limit] if limit else matched[offset:]
        return window, total

    def find_shader(
        self,
        *,
        pso_id: int | None = None,
        stage: str | None = None,
        shader_hash: str | None = None,
        draw_index: int | None = None,
        global_id: int | None = None,
        queue_id: int | None = None,
    ) -> Optional[Shader]:
        if draw_index is not None or global_id is not None or queue_id is not None:
            draw = self.resolve_draw(
                draw_index=draw_index, global_id=global_id, queue_id=queue_id
            )
            if draw is None:
                return None
            if stage:
                return draw.shader(stage)
            return draw.shaders[0] if draw.shaders else None
        candidates, _ = self.find_shaders(stage=stage, name=shader_hash)
        if pso_id is not None:
            candidates = [s for s in candidates if s.pso_id == pso_id]
        return candidates[0] if candidates else None

    # ==================================================================
    # shader data access (called from model properties)
    # ==================================================================
    def _load_blob(self, index: int) -> bytes:
        return self._resource_stream.read_index(index)

    def _load_shader_bytecode(self, shader: Shader) -> bytes:
        if shader.blob_index is None:
            return b""
        try:
            packed = self._load_blob(shader.blob_index)
        except PixToolError:
            return b""
        parts = split_packed_shaders(packed)
        for part in parts:
            if len(part) == shader.byte_size:
                try:
                    container = DxbcContainer.parse(part)
                except ValueError:
                    continue
                if not shader._chunks:
                    self._fill_shader_meta(shader, container)
                if len(parts) == 1:
                    return part
        chunk = packed[shader.blob_stage_offset : shader.blob_stage_offset + shader.byte_size]
        if chunk[:4] == b"DXBC":
            try:
                self._fill_shader_meta(shader, DxbcContainer.parse(chunk))
            except ValueError:
                pass
            return chunk
        pso = self.pipeline_states.get(shader.pso_id)
        order = [s.stage for s in pso.shaders] if pso else []
        try:
            position = order.index(shader.stage)
        except ValueError:
            position = 0
        if position < len(parts):
            candidate = parts[position]
            try:
                self._fill_shader_meta(shader, DxbcContainer.parse(candidate))
            except ValueError:
                pass
            return candidate
        return b""

    def _fill_shader_meta(self, shader: Shader, container: DxbcContainer) -> None:
        shader._chunks = tuple(container.tags)
        shader.hash_md5 = container.hash_md5
        shader._shader_hash = container.shader_hash or ""
        shader._debug_name = container.debug_name or ""
        shader._meta_loaded = True

    def _ensure_shader_meta(self, shader: Shader) -> None:
        if shader.blob_index is None:
            return
        blob = shader.bytecode
        if not blob:
            return
        try:
            self._fill_shader_meta(shader, DxbcContainer.parse(blob))
        except ValueError:
            pass

    def _container(self, shader: Shader) -> Optional[DxbcContainer]:
        blob = shader.bytecode
        if not blob:
            return None
        try:
            return DxbcContainer.parse(blob)
        except ValueError:
            return None

    def _disassemble(self, shader: Shader) -> str:
        blob = shader.bytecode
        if not blob:
            return ""
        try:
            return self._disassembler.disassemble(blob)
        except PixToolError:
            return ""

    def _signature(self, shader: Shader, tag: str) -> list[Any]:
        container = self._container(shader)
        if container is None:
            return []
        return parse_signature(container.chunk(tag))

    def _shader_bindings(self, shader: Shader) -> list[dict[str, Any]]:
        return parse_resource_bindings(shader.disassembly)

    def _shader_metadata(self, shader: Shader) -> dict[str, Any]:
        return parse_shader_metadata(shader.disassembly)

    def _shader_constant_buffers(self, shader: Shader) -> list[dict[str, Any]]:
        return parse_constant_buffers(shader.disassembly)

    def _embedded_source(self, shader: Shader) -> str:
        container = self._container(shader)
        if container is None:
            return ""
        for tag in ("SPDB", "ILDB"):
            raw = container.chunk(tag)
            text = scrape_embedded_hlsl(raw or b"")
            if text:
                return text
        return ""

    @property
    def disassembly_available(self) -> bool:
        return self._disassembler.available

    @property
    def disassembly_unavailable_reason(self) -> str | None:
        return self._disassembler.unavailable_reason

    # ==================================================================
    # statistics
    # ==================================================================
    def frame_statistics(self) -> dict[str, Any]:
        draws = [d for d in self.draw_calls if d.kind is EventKind.DRAW]
        dispatches = [d for d in self.draw_calls if d.kind is EventKind.DISPATCH]
        indirect = [d for d in self.draw_calls if d.kind is EventKind.EXECUTE_INDIRECT]
        textures = [r for r in self.resources.values() if r.is_texture]
        buffers = [r for r in self.resources.values() if r.is_buffer]
        render_targets = [r for r in textures if r.is_render_target]

        return {
            "events": {
                "total": len(self.events),
                "by_kind": dict(Counter(e.kind.value for e in self.events)),
            },
            "draw_calls": {
                "total": len(self.draw_calls),
                "draw": len(draws),
                "dispatch": len(dispatches),
                "execute_indirect": len(indirect),
                "by_kind": dict(Counter(d.kind.value for d in self.draw_calls)),
            },
            "geometry": {
                "total_triangles": sum(d.triangle_count for d in draws),
                "total_instances": sum(d.instance_count for d in draws),
                "indexed_draws": sum(1 for d in draws if d.index_buffer is not None),
                "max_triangles_in_draw": max((d.triangle_count for d in draws), default=0),
            },
            "compute": {
                "total_threads": sum(d.thread_count for d in dispatches),
                "max_threads_in_dispatch": max(
                    (d.thread_count for d in dispatches), default=0
                ),
            },
            "passes": {
                "total": len(self.passes),
                "max_depth": max((p["depth"] for p in self.passes), default=0),
            },
            "resources": {
                "total": len(self.resources),
                "textures": len(textures),
                "buffers": len(buffers),
                "render_targets": len(render_targets),
                "by_kind": dict(Counter(r.kind.value for r in self.resources.values())),
                "estimated_texture_bytes": sum(r.size_bytes for r in textures),
                "estimated_buffer_bytes": sum(r.size_bytes for r in buffers),
            },
            "descriptors": {
                "total": len(self.views),
                "by_kind": dict(Counter(v.kind.value for v in self.views.values())),
            },
            "pipeline": {
                "pipeline_states": len(self.pipeline_states),
                "root_signatures": len(self.root_signatures),
                "unique_psos_used": len(
                    {d.pso_id for d in self.draw_calls if d.pso_id is not None}
                ),
            },
            "shaders": {
                "total": len(self.shaders),
                "by_stage": dict(Counter(s.stage.value for s in self.shaders)),
                "unique": len({s.key for s in self.shaders}),
            },
            "capabilities": {
                "disassembly_available": self.disassembly_available,
                "event_list_available": bool(self.events),
            },
        }
