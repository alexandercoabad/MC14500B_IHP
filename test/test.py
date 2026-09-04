"""
Comprehensive verification suite for tt_um_mc14500b_soc_extended.

Organized in three groups:
  1. Core / peripheral functional tests   -> must PASS before tapeout.
  2. Regression tests for issues found    -> currently FAIL; they encode the
     *intended* behaviour (per the project's own README/info.md) so that a
     fix can be verified by re-running this file and watching them go green.
  3. Documentation / spec-compliance checks.

Observability note: RR (the MC14500B result register) is never brought out
to a pin directly. Every test that needs to see RR routes it out through the
"STO 0xC" (opcode 0x8, operand 0xC) instruction, which shifts RR into
uo_out[0] via the output-latch peripheral. This is the only externally
observable proxy for RR available on this design.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

CLK_PERIOD_NS = 20  # 50 MHz, matches info.yaml


# ---------------------------------------------------------------------------
# Instruction encoding helpers (opcode:operand nibble split, per project.v)
# ---------------------------------------------------------------------------
NOP0, LD, LDC, AND, ANDC, OR, ORC, XNOR = range(8)
STO, STOC, IEN, OEN, JMP, RTN_SKIP, SKZ, NOPF = range(8, 16)


def ins(opcode, operand):
    return ((opcode & 0xF) << 4) | (operand & 0xF)


# ---------------------------------------------------------------------------
# Low level drive helpers
# ---------------------------------------------------------------------------
async def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())


async def reset(dut):
    """Assert and release rst_n. Note: pc, ram_bank, etc. are valid
    immediately on the edge that releases rst_n (no extra edge needed) -
    the *next* clock edge after this function returns will already begin
    executing instruction 0 and advance pc to 1, since NOP0 is the default
    (zeroed) program-memory content."""
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await Timer(1, unit="ns")


async def tick(dut, n=1):
    """Advance n clock edges and settle combinational logic."""
    for _ in range(n):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")


async def load_program(dut, program, exit_ui_in=0x00):
    """Write `program` (list of bytes) into the 64-byte instruction RAM via
    programming mode, then drop back into run mode with a clean pc=0 start.

    IMPORTANT TIMING NOTE: the instruction at address 0 always executes on
    the very same clock edge that programming mode is exited (prog_mode is
    a combinational tap on uio_in[7], so the edge that clears it is already
    treated as a run-mode edge). At that instant `ram_bank[15:8]` still
    holds its post-reset value (0), because `ram_bank[15:8] <= ui_in` only
    happens on run-mode edges, none of which have occurred yet. That means
    any LD/LDC placed at address 0 will read stale (always-zero) external
    input data, one full cycle out of sync with `exit_ui_in`. Tests that
    need a specific external input value must therefore place a throwaway
    instruction (e.g. NOP0) at address 0, with the real input-dependent
    instruction starting at address 1, and pass the desired value as
    `exit_ui_in` (which is what will actually be sampled).
    """
    for addr, byte in enumerate(program):
        dut.uio_in.value = 0b1100_0000 | (addr & 0x3F)  # prog_mode=1, prog_we=1
        dut.ui_in.value = byte & 0xFF
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
    # Leave programming mode; pc re-starts at 0 on the next edge, which is
    # also the edge that executes the instruction at address 0.
    dut.uio_in.value = 0x00
    dut.ui_in.value = exit_ui_in & 0xFF
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


def get_pc(dut):
    return dut.uio_out.value.to_unsigned() & 0x3F


def get_write_pulse(dut):
    return (dut.uio_out.value.to_unsigned() >> 7) & 0x1


def get_uo(dut):
    return dut.uo_out.value.to_unsigned()


# ===========================================================================
# GROUP 1 - Core functional behaviour
# ===========================================================================

@cocotb.test()
async def test_reset_state(dut):
    """After reset (and with prog_mode=0), pc=0, outputs are all zero, and
    uio_oe indicates run-mode direction (all uio pins driven as outputs)."""
    await start_clock(dut)
    await reset(dut)

    assert get_pc(dut) == 0, f"pc should be 0 after reset, got {get_pc(dut)}"
    assert get_uo(dut) == 0x00, f"uo_out should be 0 after reset, got {get_uo(dut):#x}"
    assert dut.uio_oe.value.to_unsigned() == 0xFF, "uio_oe should be all-output in run mode"


@cocotb.test()
async def test_program_counter_increments_and_wraps(dut):
    """pc must free-run through all 64 words and wrap 63 -> 0 (6-bit counter)."""
    await start_clock(dut)
    await reset(dut)
    await load_program(dut, [ins(NOP0, 0)] * 64)  # explicit NOP program

    last_pc = get_pc(dut)
    seen_wrap = False
    for _ in range(140):  # > 2 full loops of 64
        await tick(dut)
        pc = get_pc(dut)
        if last_pc == 63 and pc == 0:
            seen_wrap = True
        else:
            assert pc == (last_pc + 1) & 0x3F, f"pc jumped from {last_pc} to {pc}"
        last_pc = pc
    assert seen_wrap, "pc never wrapped from 63 back to 0 in 140 cycles"


@cocotb.test()
async def test_program_mode_holds_pc_at_zero(dut):
    """While prog_mode=1, pc must be held at 0 regardless of clock edges,
    and normal execution must not occur (no output-latch writes)."""
    await start_clock(dut)
    await reset(dut)
    # Program that would shift a bit into uo_out[0] if it ever executed.
    await load_program(dut, [ins(LDC, 0), ins(STO, 0xC)] + [ins(NOP0, 0)] * 62)

    # Re-enter programming mode without writing (prog_we=0) and hold it.
    dut.uio_in.value = 0b1000_0000
    for _ in range(5):
        await tick(dut)
        assert get_pc(dut) == 0, "pc must stay at 0 while prog_mode=1"
    assert get_uo(dut) == 0x00, "core must not execute while prog_mode=1"
    dut.uio_in.value = 0x00


@cocotb.test()
async def test_ld_reads_external_input_with_one_cycle_latency(dut):
    """LD from address 0xF must reflect ui_in[7], latched one clock late
    (ram_bank[15:8] <= ui_in happens in the same edge as pc advances)."""
    await start_clock(dut)
    await reset(dut)
    prog = [ins(NOP0, 0), ins(LD, 0xF), ins(STO, 0xC)] + [ins(NOP0, 0)] * 61
    await load_program(dut, prog, exit_ui_in=0x80)  # bit 7 = 1
    await tick(dut, 2)  # NOP already executed on the load_program exit edge
    assert get_uo(dut) & 0x1 == 1, "expected RR (from ui_in[7]) to reach uo_out[0] as 1"

    # Reset and repeat with bit7=0 to confirm it's not stuck.
    await reset(dut)
    await load_program(dut, prog, exit_ui_in=0x00)
    await tick(dut, 2)
    assert get_uo(dut) & 0x1 == 0, "expected RR (from ui_in[7]=0) to reach uo_out[0] as 0"


@cocotb.test()
async def test_ldc_inverts_input(dut):
    """LDC must load the complement of the addressed bit into RR."""
    await start_clock(dut)
    await reset(dut)
    prog = [ins(NOP0, 0), ins(LDC, 0xF), ins(STO, 0xC)] + [ins(NOP0, 0)] * 61

    await load_program(dut, prog, exit_ui_in=0x80)  # bit7=1 -> LDC should give RR=0
    await tick(dut, 2)
    assert get_uo(dut) & 0x1 == 0, "LDC of a 1 should produce RR=0"

    await reset(dut)
    await load_program(dut, prog, exit_ui_in=0x00)  # bit7=0 -> LDC should give RR=1
    await tick(dut, 2)
    assert get_uo(dut) & 0x1 == 1, "LDC of a 0 should produce RR=1"


@cocotb.test()
async def test_scratch_ram_write_and_readback(dut):
    """Registers 0x0-0x7 must be independently read/write-able general
    purpose scratch bits, per the README's address map."""
    await start_clock(dut)
    await reset(dut)
    # NOP; LD ui_in[7] -> STO reg3 -> LD reg3 -> STO 0xC  (round-trips through scratch reg 3)
    prog = [ins(NOP0, 0), ins(LD, 0xF), ins(STO, 0x3), ins(LD, 0x3), ins(STO, 0xC)] + [ins(NOP0, 0)] * 59
    await load_program(dut, prog, exit_ui_in=0x80)

    await tick(dut, 4)
    assert get_uo(dut) & 0x1 == 1, "value written to scratch reg 3 should read back as 1"


@cocotb.test()
async def test_alu_truth_tables(dut):
    """Directed truth-table coverage for AND / ANDC / OR / ORC / XNOR across
    all 4 combinations of (previous RR, addressed data bit)."""
    await start_clock(dut)

    ops_and_truth = {
        AND:  lambda rr, d: rr & d,
        ANDC: lambda rr, d: rr & (not d),
        OR:   lambda rr, d: rr | d,
        ORC:  lambda rr, d: rr | (not d),
        XNOR: lambda rr, d: not (rr ^ d),
    }

    for opcode, fn in ops_and_truth.items():
        for rr_init, data_bit in [(0, 0), (0, 1), (1, 0), (1, 1)]:
            await reset(dut)
            # reg0 <- rr_init (via LDC-trick: LDC of address 0 (which is 0
            # after reset) gives 1; LD of 0 gives 0), reg1 <- data_bit,
            # then LD reg0 (sets RR=rr_init), <opcode> reg1, STO 0xC.
            seed_rr_op = LDC if rr_init else LD
            prog = [
                ins(seed_rr_op, 0x0),   # RR = rr_init (reg0 is 0 post-reset)
                ins(STO, 0x0),          # stash rr_init into reg0 (harmless, keeps addressing simple)
                ins(LD, 0xF),           # RR = ui_in[7]
                ins(STO, 0x1),          # reg1 = data_bit
                ins(LD, 0x0),           # reload RR = rr_init
                ins(opcode, 0x1),       # RR = op(rr_init, reg1)
                ins(STO, 0xC),          # observe RR
            ] + [ins(NOP0, 0)] * 57
            await load_program(dut, prog)
            dut.ui_in.value = 0x80 if data_bit else 0x00
            await tick(dut, len(prog) + 4)  # let it loop through at least once fully
            expected = 1 if fn(rr_init, data_bit) else 0
            got = get_uo(dut) & 0x1
            assert got == expected, (
                f"opcode={opcode:#x} rr_init={rr_init} data={data_bit}: "
                f"expected RR={expected}, got {got}"
            )


@cocotb.test()
async def test_ien_gates_data_input(dut):
    """When IEN (via opcode 0xA) clears the input-enable flag, subsequently
    addressed data bits must read back as 0 regardless of the true value
    (actual_data = core_data_in & r_ien)."""
    await start_clock(dut)
    await reset(dut)
    prog = [
        ins(IEN, 0x0),   # r_ien <= actual_data; reg0 is 0 post-reset -> r_ien becomes 0
        ins(LD, 0xF),    # RR should now read as 0 no matter what ui_in[7] is
        ins(STO, 0xC),
    ] + [ins(NOP0, 0)] * 61
    await load_program(dut, prog)
    dut.ui_in.value = 0x80  # ui_in[7]=1, but IEN should mask it out
    await tick(dut, 6)
    assert get_uo(dut) & 0x1 == 0, "IEN=0 should force addressed data to read as 0"


@cocotb.test()
async def test_oen_blocks_store(dut):
    """When OEN (opcode 0xB) clears the output-enable flag, STO/STOC must
    not modify any addressed register (core_write_en requires r_oen)."""
    await start_clock(dut)
    await reset(dut)
    prog = [
        ins(OEN, 0x0),    # r_oen <= actual_data = reg0(0) -> r_oen becomes 0
        ins(LDC, 0x0),    # RR = 1
        ins(STO, 0x2),    # should be BLOCKED: reg2 must remain 0
        ins(LD, 0x2),     # read back reg2
        ins(STO, 0xC),    # observe it
    ] + [ins(NOP0, 0)] * 59
    await load_program(dut, prog)
    await tick(dut, 8)
    assert get_uo(dut) & 0x1 == 0, "OEN=0 should have blocked the STO from writing reg2"


@cocotb.test()
async def test_skip_opcode_skips_exactly_one_instruction(dut):
    """This design implements the skip-if-zero behaviour on opcode 0xD
    (labelled RTN/SKZ in project.v) rather than the standard MC14500B's
    0xE. Confirm the *implemented* behaviour: when RR=0 the skip fires and
    the immediately following instruction has no effect."""
    await start_clock(dut)
    await reset(dut)
    prog = [
        ins(LD, 0x0),      # RR = reg0 = 0
        ins(RTN_SKIP, 0),  # skip-if-RR==0 -> should skip next instruction
        ins(LDC, 0x0),     # SKIPPED: would otherwise set RR=1
        ins(STO, 0xC),     # observe RR: should still be 0 (skip worked)
    ] + [ins(NOP0, 0)] * 60
    await load_program(dut, prog)
    await tick(dut, 6)
    assert get_uo(dut) & 0x1 == 0, (
        "opcode 0xD should have skipped the following LDC, leaving RR=0"
    )


@cocotb.test()
async def test_edge_detector_addr8(dut):
    """A rising edge on ui_in[0] should set the edge flag (readable at
    address 8); writing a 1 back to address 8 via STO should clear it.

    Note: `LD 0x8`/`STO 0xC` are placed at the *start* of a 64-word program
    that otherwise free-runs through NOPs, so re-sampling edge_flag after
    changing the stimulus requires waiting for pc to wrap back around to
    address 0 (~64 cycles) rather than just a couple of ticks.
    """
    await start_clock(dut)
    await reset(dut)
    prog = [
        ins(LD, 0x8),   # RR = edge_flag (safe at addr 0: edge_flag is a
                         # plain register, not routed through ram_bank, so
                         # it doesn't suffer the stale-ram_bank issue that
                         # ui_in-derived reads have at address 0)
        ins(STO, 0xC),  # observe it
    ] + [ins(NOP0, 0)] * 62
    await load_program(dut, prog)

    dut.ui_in.value = 0x00
    await tick(dut, 2)
    assert get_uo(dut) & 0x1 == 0, "edge flag should be clear before any edge"

    dut.ui_in.value = 0x01  # rising edge on ui_in[0]
    await tick(dut, 64)  # let pc wrap back around to re-execute LD 0x8/STO 0xC
    assert get_uo(dut) & 0x1 == 1, "edge flag should be set after a rising edge on ui_in[0]"

    # Clear it: STOC/STO of 1 -> address 8. Keep ui_in[0] held (no new edge).
    clear_prog = [ins(LDC, 0x0), ins(STO, 0x8), ins(LD, 0x8), ins(STO, 0xC)] + [ins(NOP0, 0)] * 60
    await load_program(dut, clear_prog, exit_ui_in=0x01)
    await tick(dut, 6)
    assert get_uo(dut) & 0x1 == 0, "writing 1 to address 8 should clear the edge flag"


@cocotb.test()
async def test_output_latch_shifts_msb_first_history(dut):
    """Repeated STO 0xC calls should build up an 8-bit shift history in
    uo_out, newest bit in position 0."""
    await start_clock(dut)
    await reset(dut)
    # Alternate RR 1,0,1,0,... via LD/LDC of reg0 (=0), STO 0xC, four times.
    prog = []
    for i in range(4):
        prog.append(ins(LDC if i % 2 == 0 else LD, 0x0))
        prog.append(ins(STO, 0xC))
    prog += [ins(NOP0, 0)] * (64 - len(prog))
    await load_program(dut, prog)

    await tick(dut, 8)
    # Expect the last 4 shifted-in bits (newest first) = 1,0,1,0 -> binary ...1010
    assert get_uo(dut) & 0xF == 0b1010, f"unexpected shift history, got {get_uo(dut):#04x}"


@cocotb.test()
async def test_clock_divider_freezes_pc(dut):
    """Enabling the clock divider (STO 1 to address 9) must hold pc static
    for many cycles instead of stepping every clock."""
    await start_clock(dut)
    await reset(dut)
    prog = [ins(LDC, 0x0), ins(STO, 0x9)] + [ins(NOP0, 0)] * 62
    await load_program(dut, prog)

    await tick(dut, 3)  # let pc reach the frozen instruction
    frozen_pc = get_pc(dut)
    for _ in range(50):
        await tick(dut)
        assert get_pc(dut) == frozen_pc, "pc must not advance while the clock divider is active"


# ===========================================================================
# GROUP 2 - Regression tests for issues found during review.
# These encode the behaviour implied by the project's own documentation.
# If they FAIL, it means the RTL currently diverges from spec (see the
# accompanying design-review notes).
# ===========================================================================

@cocotb.test()
async def test_bug_stoc_should_invert_for_peripheral_addresses(dut):
    """BUG CANDIDATE: for scratch registers 0-7, STOC correctly stores the
    complement of RR (see project.v's `(opcode==9) ? !core_data_out :
    core_data_out`). The same inversion is NOT applied for the peripheral
    addresses 0x8/0x9/0xC, which all key off the raw `core_data_out` (=RR)
    regardless of whether the instruction was STO or STOC. This test
    encodes the *intended* symmetric behaviour and is expected to FAIL
    against the current RTL, which instead behaves like STO in both cases
    for peripheral writes."""
    await start_clock(dut)
    await reset(dut)
    # RR = 0 (reg0 is 0 post-reset), STOC to addr 0xC should store !RR = 1.
    prog = [ins(LD, 0x0), ins(STOC, 0xC)] + [ins(NOP0, 0)] * 62
    await load_program(dut, prog)
    await tick(dut, 4)
    assert get_uo(dut) & 0x1 == 1, (
        "STOC to address 0xC with RR=0 should store the complement (1); "
        "the current RTL stores the raw RR value (0) instead"
    )


@cocotb.test()
async def test_bug_peripheral_writes_alias_every_fastclock_while_parked(dut):
    """BUG CANDIDATE: core_write_en (which drives the edge-flag-clear,
    clock-divider, and output-latch peripherals) is a *combinational*
    function of the currently fetched instruction. It is not gated by
    cpu_clk_step. When the clock divider parks the CPU on an STO/STOC
    instruction for ~4096 real clock cycles, the peripheral logic (which
    lives in its own always-blocks, clocked every real cycle) re-executes
    the same store on every single physical clock edge instead of once per
    CPU step.

    This test parks pc on `STO 0xC` with RR=1 (after enabling the slow
    clock) and samples uo_out for the first several physical clock edges.
    Correct (intended) behaviour: uo_out changes ONCE (0x00 -> 0x01) and
    then holds steady for the remainder of the ~4096-cycle wait.
    Actual/expected-failing behaviour if the bug is present: uo_out keeps
    ramping (0x01, 0x03, 0x07, 0x0F, ...) as the same bit is repeatedly
    shifted in on every physical clock edge.
    """
    await start_clock(dut)
    await reset(dut)
    prog = [
        ins(LDC, 0x0),   # RR = 1 (reg0 is 0 post-reset)
        ins(STO, 0x9),   # enable slow clock divider
        ins(STO, 0xC),   # this is where pc will be parked for ~4096 cycles
    ] + [ins(NOP0, 0)] * 61
    await load_program(dut, prog)

    # Advance one edge at a time so we can capture uo_out on the very first
    # cycle pc parks at 2 (avoids masking early aliasing inside a batch tick).
    samples = []
    for _ in range(11):
        await tick(dut)
        if get_pc(dut) == 2:
            samples.append(get_uo(dut))
    assert len(samples) >= 8, f"pc never parked at 2 long enough, got {samples}"
    samples = samples[:8]

    # Intended behaviour: the STO fires exactly once as pc arrives at 2 (the
    # write lands one cycle later), then uo_out should hold at 0x01 for the
    # remaining ~4094 parked cycles.
    expected = [0] + [1] * 7
    assert samples == expected, (
        f"uo_out should settle to 0x01 and hold steady; instead observed the "
        f"ramping sequence {[hex(s) for s in samples]} (expected {[hex(s) for s in expected]}), "
        "indicating the STO instruction is being re-executed every physical "
        "clock cycle while the CPU is parked by the slow-clock divider"
    )


# ===========================================================================
# GROUP 3 - Documentation / spec-compliance checks
# ===========================================================================

@cocotb.test()
async def test_doc_addr8to15_are_not_a_uniform_input_port(dut):
    """The README's address map table describes 0x8-0xF as a single
    contiguous range, and info.md separately claims the range 0x8-0xF
    exposes ui_in[7:0]. In the actual RTL only addresses 0xD/0xE/0xF
    expose (delayed) ui_in bits 5/6/7; addresses 0x8/0x9/0xC are
    peripheral registers, and 0xA/0xB are hard-wired to 0. This test
    documents the *actual* mapping so a reader can see the discrepancy;
    it is expected to PASS against the current RTL and is here to protect
    against silent re-interpretation of the address map during any future
    edit."""
    await start_clock(dut)
    await reset(dut)
    prog = [ins(LD, 0xA), ins(STO, 0xC)] + [ins(NOP0, 0)] * 62  # LD reserved addr 0xA
    await load_program(dut, prog)
    dut.ui_in.value = 0xFF  # if 0xA were a live input tap, this would read as 1
    await tick(dut, 4)
    assert get_uo(dut) & 0x1 == 0, (
        "address 0xA is documented ambiguously but is hard-wired to 0 in the "
        "RTL (mapped_ram_bank[11:10] = 2'b00), not a live ui_in tap"
    )
