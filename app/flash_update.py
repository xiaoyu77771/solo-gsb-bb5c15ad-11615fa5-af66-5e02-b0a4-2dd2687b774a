"""Incremental flash update planning.

Given the *current* flash contents and the *target* firmware image (both
assembled from Intel HEX), compute the erase/write operation sequence
that turns one into the other inside the flash window
``[flash_start, flash_end)``:

* addresses not present in an image read as ``0xFF`` (erased state);
* the window must hold a whole number of erase blocks: ``flash_start``
  and ``flash_end`` aligned to ``erase_block_size``;
* per erase block: when any bit must flip 0 -> 1 (flash cells program
  1 -> 0 only), the block is erased first and every non-``0xFF`` target
  byte in it is rewritten; otherwise just the differing bytes are
  written;
* pending writes merge into contiguous runs capped at
  ``max_write_size`` and never crossing an erase-block boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hex_parser import Segment

ERASED_BYTE = 0xFF


@dataclass
class FlashParams:
    """Flash window and operation limits."""

    flash_start: int  # inclusive window start
    flash_end: int  # exclusive window end
    erase_block_size: int
    max_write_size: int


@dataclass
class FlashOperation:
    """One erase or write operation; writes carry their payload."""

    op: str  # "erase" | "write"
    address: int
    size: int
    data: bytes | None = None


@dataclass
class FlashBlock:
    """An erase block touched by the plan."""

    index: int
    start: int
    end: int
    erased: bool
    write_bytes: int


@dataclass
class FlashUpdatePlan:
    params_valid: bool
    param_errors: list[str] = field(default_factory=list)
    operations: list[FlashOperation] = field(default_factory=list)
    blocks: list[FlashBlock] = field(default_factory=list)
    block_count: int = 0
    erase_count: int = 0
    erased_bytes: int = 0
    write_count: int = 0
    written_bytes: int = 0
    changed_bytes: int = 0


def _fmt(addr: int) -> str:
    return f"0x{addr:04X}"


def validate_flash_params(params: FlashParams) -> list[str]:
    """Return every parameter problem; an empty list means usable."""
    errors: list[str] = []
    if params.erase_block_size <= 0:
        errors.append(f"erase_block_size must be positive, got {params.erase_block_size}")
    if params.max_write_size <= 0:
        errors.append(f"max_write_size must be positive, got {params.max_write_size}")
    if params.flash_start < 0:
        errors.append(f"flash_start must be non-negative, got {params.flash_start}")
    if params.flash_end <= params.flash_start:
        errors.append(
            f"flash_end {_fmt(params.flash_end)} must be greater than "
            f"flash_start {_fmt(params.flash_start)} (half-open interval)"
        )
    if params.erase_block_size > 0:
        if params.flash_start % params.erase_block_size:
            errors.append(
                f"flash_start {_fmt(params.flash_start)} is not aligned to "
                f"erase_block_size {params.erase_block_size}"
            )
        if params.flash_end % params.erase_block_size:
            errors.append(
                f"flash_end {_fmt(params.flash_end)} is not aligned to "
                f"erase_block_size {params.erase_block_size}"
            )
    return errors


def plan_flash_update(
    current_segments: list[Segment],
    target_segments: list[Segment],
    params: FlashParams,
) -> FlashUpdatePlan:
    """Diff both images inside the flash window and emit the op sequence.

    Parameter problems abort the plan: no operations are generated.
    """
    errors = validate_flash_params(params)
    if errors:
        return FlashUpdatePlan(params_valid=False, param_errors=errors)

    window = params.flash_end - params.flash_start
    current = _paint_window(current_segments, params.flash_start, window)
    target = _paint_window(target_segments, params.flash_start, window)

    block_size = params.erase_block_size
    plan = FlashUpdatePlan(params_valid=True, block_count=window // block_size)
    for index in range(plan.block_count):
        offset = index * block_size
        cur = current[offset : offset + block_size]
        tgt = target[offset : offset + block_size]
        if cur == tgt:
            continue
        plan.changed_bytes += sum(1 for c, t in zip(cur, tgt) if c != t)
        # Flash cells program 1 -> 0 only; a bit set in the target but
        # clear in the current contents forces an erase of the block.
        needs_erase = any((c & t) != t for c, t in zip(cur, tgt))
        block_start = params.flash_start + offset
        if needs_erase:
            plan.operations.append(FlashOperation("erase", block_start, block_size))
            plan.erase_count += 1
            plan.erased_bytes += block_size
            write_offsets = [i for i, t in enumerate(tgt) if t != ERASED_BYTE]
        else:
            write_offsets = [i for i, (c, t) in enumerate(zip(cur, tgt)) if c != t]
        written = 0
        for run_start, run_end in _runs(write_offsets):
            pos = run_start
            while pos < run_end:
                size = min(params.max_write_size, run_end - pos)
                plan.operations.append(
                    FlashOperation("write", block_start + pos, size, bytes(tgt[pos : pos + size]))
                )
                plan.write_count += 1
                plan.written_bytes += size
                written += size
                pos += size
        plan.blocks.append(
            FlashBlock(
                index=index,
                start=block_start,
                end=block_start + block_size,
                erased=needs_erase,
                write_bytes=written,
            )
        )
    return plan


def count_outside(segments: list[Segment], start: int, end: int) -> int:
    """Number of image bytes falling outside the half-open ``[start, end)``."""
    total = 0
    for seg in segments:
        lo = max(seg.start, start)
        hi = min(seg.end, end)
        total += len(seg.data) - max(0, hi - lo)
    return total


def _paint_window(segments: list[Segment], start: int, size: int) -> bytearray:
    """Dense image of the window; addresses without data stay ``0xFF``."""
    window = bytearray(b"\xff" * size)
    for seg in segments:
        lo = max(seg.start, start)
        hi = min(seg.end, start + size)
        if lo < hi:
            window[lo - start : hi - start] = seg.data[lo - seg.start : hi - seg.start]
    return window


def _runs(offsets: list[int]) -> list[tuple[int, int]]:
    """Merge sorted offsets into contiguous half-open ``(start, end)`` runs."""
    runs: list[tuple[int, int]] = []
    for off in offsets:
        if runs and off == runs[-1][1]:
            runs[-1] = (runs[-1][0], off + 1)
        else:
            runs.append((off, off + 1))
    return runs
