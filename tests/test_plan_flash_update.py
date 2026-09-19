"""Tests for the /api/v1/plan-flash-update endpoint and flash update logic."""

import base64

from fastapi.testclient import TestClient

from app.flash_update import (
    FlashParams,
    count_outside,
    plan_flash_update,
    validate_flash_params,
)
from app.hex_parser import parse_intel_hex
from app.main import app

client = TestClient(app)


def rec(rectype: int, addr: int, data: bytes = b"") -> str:
    """Build a valid Intel HEX record line."""
    body = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, rectype]) + bytes(data)
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


EOF = rec(0x01, 0)


def data_rec(addr: int, data: bytes) -> str:
    return rec(0x00, addr, data)


def hex_text(*records: str) -> str:
    return "\n".join([*records, EOF])


def segments_of(text: str):
    result = parse_intel_hex(text)
    assert result.ok
    return result.segments


# flash window [0x1000, 0x1040): two 32-byte erase blocks, 16-byte writes
PARAMS = FlashParams(flash_start=0x1000, flash_end=0x1040, erase_block_size=0x20, max_write_size=0x10)


def plan(current_text: str, target_text: str, params: FlashParams = PARAMS):
    return plan_flash_update(segments_of(current_text), segments_of(target_text), params)


# ---------------------------------------------------------------------------
# parameter validation
# ---------------------------------------------------------------------------

def test_valid_params_pass():
    assert validate_flash_params(PARAMS) == []


def test_misaligned_window_edges_rejected():
    errors = validate_flash_params(FlashParams(0x1004, 0x1040, 0x20, 0x10))
    assert any("flash_start" in e and "aligned" in e for e in errors)
    errors = validate_flash_params(FlashParams(0x1000, 0x1030, 0x20, 0x10))
    assert any("flash_end" in e and "aligned" in e for e in errors)


def test_non_positive_sizes_rejected():
    errors = validate_flash_params(FlashParams(0x1000, 0x1040, 0, 0x10))
    assert any("erase_block_size" in e for e in errors)
    errors = validate_flash_params(FlashParams(0x1000, 0x1040, 0x20, 0))
    assert any("max_write_size" in e for e in errors)
    errors = validate_flash_params(FlashParams(0x1000, 0x1040, -0x20, -1))
    assert any("erase_block_size" in e for e in errors)
    assert any("max_write_size" in e for e in errors)


def test_empty_or_inverted_window_rejected():
    assert validate_flash_params(FlashParams(0x1000, 0x1000, 0x20, 0x10))
    assert validate_flash_params(FlashParams(0x1040, 0x1000, 0x20, 0x10))
    assert validate_flash_params(FlashParams(-0x20, 0x1000, 0x20, 0x10))


def test_invalid_params_abort_planning():
    result = plan_flash_update([], [], FlashParams(0x1004, 0x1040, 0x20, 0x10))
    assert not result.params_valid
    assert result.param_errors
    assert result.operations == []
    assert result.blocks == []


# ---------------------------------------------------------------------------
# pure planning logic
# ---------------------------------------------------------------------------

def test_identical_images_need_no_operations():
    text = hex_text(data_rec(0x1000, b"\x01\x02\x03"))
    result = plan(text, text)
    assert result.params_valid
    assert result.operations == []
    assert result.blocks == []
    assert result.block_count == 2
    assert result.changed_bytes == 0
    assert result.erased_bytes == 0
    assert result.written_bytes == 0


def test_one_to_zero_change_writes_only_changed_bytes():
    current = hex_text(data_rec(0x1000, b"\xff\xff"))
    target = hex_text(data_rec(0x1000, b"\x0f\xff"))
    result = plan(current, target)
    assert [(o.op, o.address, o.size) for o in result.operations] == [("write", 0x1000, 1)]
    assert result.operations[0].data == b"\x0f"
    assert result.erase_count == 0
    assert result.erased_bytes == 0
    assert result.written_bytes == 1
    assert result.changed_bytes == 1
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert (block.index, block.start, block.end) == (0, 0x1000, 0x1020)
    assert block.erased is False
    assert block.write_bytes == 1


def test_zero_to_one_change_erases_block_without_writes():
    # target has no data in the block: it must read as all-0xFF after erase
    current = hex_text(data_rec(0x1000, b"\x0f"))
    result = plan(current, EOF)
    assert [(o.op, o.address, o.size) for o in result.operations] == [("erase", 0x1000, 0x20)]
    assert result.erase_count == 1
    assert result.erased_bytes == 0x20
    assert result.write_count == 0
    assert result.changed_bytes == 1
    assert result.blocks[0].erased is True
    assert result.blocks[0].write_bytes == 0


def test_erase_rewrites_all_non_ff_target_bytes_in_block():
    # 0xF0 -> 0xFF at 0x1004 forces an erase; the unchanged 0x0F at 0x1000
    # must be rewritten because the erase wipes it too
    current = hex_text(data_rec(0x1000, b"\x0f"), data_rec(0x1004, b"\xf0"))
    target = hex_text(data_rec(0x1000, b"\x0f"))
    result = plan(current, target)
    assert [(o.op, o.address, o.size) for o in result.operations] == [
        ("erase", 0x1000, 0x20),
        ("write", 0x1000, 1),
    ]
    assert result.operations[1].data == b"\x0f"
    assert result.blocks[0].erased is True
    assert result.blocks[0].write_bytes == 1


def test_missing_addresses_default_to_ff():
    # explicit 0xFF equals the erased state: no operation at all
    current = hex_text(data_rec(0x1000, b"\xff" * 4))
    result = plan(current, EOF)
    assert result.operations == []
    assert result.blocks == []
    # and an empty current image against an empty target is equally quiet
    assert plan(EOF, EOF).operations == []


def test_runs_merge_and_split_at_max_write_size():
    payload = bytes(range(0x20))  # one full block, 1 -> 0 flips only
    result = plan(EOF, hex_text(data_rec(0x1000, payload)))
    assert [(o.op, o.address, o.size) for o in result.operations] == [
        ("write", 0x1000, 0x10),
        ("write", 0x1010, 0x10),
    ]
    assert result.operations[0].data == payload[:0x10]
    assert result.operations[1].data == payload[0x10:]
    assert result.written_bytes == 0x20


def test_sparse_writes_form_separate_runs():
    current = hex_text(data_rec(0x1000, b"\xff" * 8))
    target = hex_text(data_rec(0x1000, b"\x00"), data_rec(0x1004, b"\x00" * 4))
    result = plan(current, target)
    assert [(o.op, o.address, o.size) for o in result.operations] == [
        ("write", 0x1000, 1),
        ("write", 0x1004, 4),
    ]


def test_writes_never_cross_erase_block_boundary():
    # both blocks fully rewritten; max_write_size (0x30) exceeds the block
    # size (0x20), yet every write must stay inside its own block
    params = FlashParams(0x1000, 0x1040, 0x20, 0x30)
    result = plan(EOF, hex_text(data_rec(0x1000, b"\x00" * 0x40)), params)
    assert [(o.op, o.address, o.size) for o in result.operations] == [
        ("write", 0x1000, 0x20),
        ("write", 0x1020, 0x20),
    ]
    for op in result.operations:
        block_start = params.flash_start + (op.address - params.flash_start) // params.erase_block_size * params.erase_block_size
        assert block_start <= op.address
        assert op.address + op.size <= block_start + params.erase_block_size


def test_erased_and_plain_blocks_reported_in_address_order():
    # block 0: 0xF0 -> 0xFF forces erase; block 1: 0xFF -> 0x0F plain write
    current = hex_text(data_rec(0x1004, b"\xf0"), data_rec(0x1020, b"\xff"))
    target = hex_text(data_rec(0x1020, b"\x0f"))
    result = plan(current, target)
    assert [(o.op, o.address) for o in result.operations] == [
        ("erase", 0x1000),
        ("write", 0x1020),
    ]
    assert [b.index for b in result.blocks] == [0, 1]
    assert [b.erased for b in result.blocks] == [True, False]
    assert result.changed_bytes == 2
    assert result.erased_bytes == 0x20
    assert result.written_bytes == 1


def test_data_outside_window_is_ignored_by_plan():
    current = hex_text(data_rec(0x2000, b"\x00" * 4))
    target = hex_text(data_rec(0x2000, b"\x01" * 4))
    result = plan(current, target)
    assert result.operations == []
    assert result.changed_bytes == 0


def test_count_outside():
    segments = segments_of(hex_text(data_rec(0x0FF0, b"\x01" * 0x20)))
    # 0x0FF0..0x1010: 16 bytes below, 16 bytes inside [0x1000, 0x1040)
    assert count_outside(segments, 0x1000, 0x1040) == 0x10
    assert count_outside(segments, 0x0000, 0x2000) == 0


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

CURRENT_HEX = hex_text(data_rec(0x1000, b"\x0f"), data_rec(0x1004, b"\xf0"))
TARGET_HEX = hex_text(data_rec(0x1000, b"\x0f"), data_rec(0x1020, b"\x0f"))
PARAMS_JSON = {
    "flash_start": 0x1000,
    "flash_end": 0x1040,
    "erase_block_size": 0x20,
    "max_write_size": 0x10,
}


def post(payload: dict):
    resp = client.post("/api/v1/plan-flash-update", json=payload)
    assert resp.status_code == 200
    return resp.json()


def test_api_happy_path():
    body = post({"current_hex": CURRENT_HEX, "target_hex": TARGET_HEX, **PARAMS_JSON})
    assert body["ok"] is True
    assert body["current_hex_ok"] is True
    assert body["target_hex_ok"] is True
    assert body["params_valid"] is True
    assert body["error_count"] == 0
    assert [(o["op"], o["address"], o["size"]) for o in body["operations"]] == [
        ("erase", 0x1000, 0x20),
        ("write", 0x1000, 1),
        ("write", 0x1020, 1),
    ]
    assert body["operations"][0]["data_base64"] is None
    assert base64.b64decode(body["operations"][1]["data_base64"]) == b"\x0f"
    assert base64.b64decode(body["operations"][2]["data_base64"]) == b"\x0f"
    assert body["blocks"] == [
        {"index": 0, "start": 0x1000, "end": 0x1020, "erased": True, "write_bytes": 1},
        {"index": 1, "start": 0x1020, "end": 0x1040, "erased": False, "write_bytes": 1},
    ]
    assert body["stats"] == {
        "block_count": 2,
        "affected_blocks": 2,
        "erase_count": 1,
        "erased_bytes": 0x20,
        "write_count": 2,
        "written_bytes": 2,
        "changed_bytes": 2,
    }
    assert body["current_total_bytes"] == 2
    assert body["target_total_bytes"] == 2


def test_api_no_changes_returns_empty_plan():
    body = post({"current_hex": CURRENT_HEX, "target_hex": CURRENT_HEX, **PARAMS_JSON})
    assert body["ok"] is True
    assert body["operations"] == []
    assert body["blocks"] == []
    assert body["stats"]["affected_blocks"] == 0
    assert body["stats"]["changed_bytes"] == 0


def test_api_invalid_current_hex_blocks_plan():
    bad = ":00000001FE\n"  # bad checksum
    body = post({"current_hex": bad, "target_hex": TARGET_HEX, **PARAMS_JSON})
    assert body["ok"] is False
    assert body["current_hex_ok"] is False
    assert body["target_hex_ok"] is True
    assert body["params_valid"] is True
    assert body["operations"] == []
    assert body["blocks"] == []
    assert body["stats"] is None
    current_diags = [d for d in body["diagnostics"] if d["source"] == "current"]
    assert any(d["status"] == "error" and "checksum" in d["message"] for d in current_diags)


def test_api_invalid_target_hex_blocks_plan():
    bad = ":011000000FE0\n"  # missing EOF
    body = post({"current_hex": CURRENT_HEX, "target_hex": bad, **PARAMS_JSON})
    assert body["ok"] is False
    assert body["current_hex_ok"] is True
    assert body["target_hex_ok"] is False
    assert body["stats"] is None
    target_diags = [d for d in body["diagnostics"] if d["source"] == "target"]
    assert any("end-of-file" in d["message"] for d in target_diags)


def test_api_invalid_params_block_plan():
    body = post(
        {"current_hex": CURRENT_HEX, "target_hex": TARGET_HEX, **PARAMS_JSON, "flash_start": 0x1004}
    )
    assert body["ok"] is False
    assert body["current_hex_ok"] is True
    assert body["target_hex_ok"] is True
    assert body["params_valid"] is False
    assert body["operations"] == []
    assert body["stats"] is None
    param_diags = [d for d in body["diagnostics"] if d["source"] == "params"]
    assert any("aligned" in d["message"] for d in param_diags)
    assert all(d["line"] == 0 for d in param_diags)


def test_api_multiple_param_errors_reported_together():
    body = post(
        {
            "current_hex": CURRENT_HEX,
            "target_hex": TARGET_HEX,
            "flash_start": 0x1040,
            "flash_end": 0x1000,
            "erase_block_size": 0,
            "max_write_size": -1,
        }
    )
    assert body["ok"] is False
    messages = [d["message"] for d in body["diagnostics"] if d["source"] == "params"]
    assert any("erase_block_size" in m for m in messages)
    assert any("max_write_size" in m for m in messages)
    assert any("flash_end" in m for m in messages)


def test_api_bytes_outside_window_warn_but_still_plan():
    target = hex_text(data_rec(0x1000, b"\x0f"), data_rec(0x9000, b"\x01"))
    body = post({"current_hex": EOF, "target_hex": target, **PARAMS_JSON})
    assert body["ok"] is True
    warnings = [d for d in body["diagnostics"] if d["status"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["source"] == "target"
    assert "outside flash window" in warnings[0]["message"]
    assert body["warning_count"] == 1
    # only the in-window byte is planned
    assert [(o["op"], o["address"]) for o in body["operations"]] == [("write", 0x1000)]


def test_api_missing_fields_return_422():
    resp = client.post("/api/v1/plan-flash-update", json={"current_hex": CURRENT_HEX})
    assert resp.status_code == 422
